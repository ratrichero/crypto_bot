#!/usr/bin/env python3
"""Multi-instrument OKX paper bot: scalp 5m/15m + grid, USDT-SWAP futures.

Price ticks arrive real-time via websocket. SL/TP + grid triggers are
checked on a fast loop (~0.5s); candle/strategy work runs on a slow loop.
Kill switch: create file STOP in this dir.
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone

import okx_client
import strategy
from indicators import atr
from paper_engine import PaperEngine
from okx_ws import TickerWS

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json")))
UNI = json.load(open(os.path.join(BASE, CFG.get("universe_file", "universe.json"))))
INSTS = [u["instId"] for u in UNI]
STATE_P = os.path.join(BASE, "state.json")
TRADES_P = os.path.join(BASE, "trades.jsonl")
LOG_P = os.path.join(BASE, "bot.log")
STOP_P = os.path.join(BASE, "STOP")

FAST_POLL = CFG.get("fast_poll_seconds", 0.5)
SLOW_EVERY = 10          # slow tasks every N fast loops (~5s)
CANDLE_PER_SLOW = 2      # instruments refreshed per slow tick

# Chon engine theo config "mode": paper | dry_run | live (mac dinh paper).
# dry_run: tin hieu di qua live_okx nhung CHI LOG, khong goi API ghi, khong can key.
# live: dat lenh that tren OKX, can file .okx_key. DOI MODE CAN RESTART BOT
# (vong reload config 60s khong doi engine giua chung).
MODE = CFG.get("mode", "paper")


def make_engine(st):
    if MODE == "paper":
        return PaperEngine(CFG, st)
    if MODE in ("dry_run", "live"):
        from live_okx import LiveEngine
        return LiveEngine(CFG, st, dry_run=(MODE == "dry_run"), log=log)
    raise SystemExit(
        "config 'mode' khong hop le: %r (chon paper|dry_run|live)" % (MODE,))


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}Z] {msg}"
    try:
        if os.path.exists(LOG_P) and os.path.getsize(LOG_P) > 5 * 1024 * 1024:
            with open(LOG_P, "rb") as f:
                f.seek(-1024 * 1024, os.SEEK_END)
                tail = f.read()
            with open(LOG_P, "wb") as f:
                f.write(tail)
    except Exception:
        pass
    with open(LOG_P, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def default_state():
    return {
        "equity": CFG["start_equity"],
        "positions": [],
        "grids": {},
        "regimes": {},
        "day": utc_day(),
        "day_start_equity": CFG["start_equity"],
        "halted": False,
        "halt_reason": "",
        "cooldown_until": 0,
        "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0},
        "_pid": 0,
    }


def load_state():
    if os.path.exists(STATE_P):
        try:
            st = json.load(open(STATE_P))
            d = default_state()
            d.update(st)
            d.setdefault("grids", {})
            d.setdefault("regimes", {})
            return d
        except Exception:
            pass
    return default_state()


def save_state(st):
    tmp = STATE_P + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_P)


def record_trade(rec):
    with open(TRADES_P, "a") as f:
        f.write(json.dumps(rec) + "\n")


def rest_tickers_fallback():
    try:
        rows = okx_client._get("/api/v5/market/tickers", {"instType": "SWAP"})
        want = set(INSTS)
        return {r["instId"]: float(r["last"]) for r in rows
                if r["instId"] in want and r.get("last")}
    except Exception as e:
        log(f"fallback tickers failed: {e}")
        return {}


def is_disabled(inst):
    return CFG.get("disabled_insts", {}).get(inst, 0) > time.time()


def update_positions(engine, st, inst, price):
    """Tick-driven SL/TP check. Returns True if anything closed."""
    closed = []
    for pos in [p for p in st["positions"] if p["inst"] == inst]:
        if pos["side"] == "long":
            if pos["sl"] and price <= pos["sl"]:
                closed.append(engine.close(pos, pos["sl"], "SL"))
            elif pos["tp"] and price >= pos["tp"]:
                closed.append(engine.close(pos, pos["tp"], "TP"))
        else:
            if pos["sl"] and price >= pos["sl"]:
                closed.append(engine.close(pos, pos["sl"], "SL"))
            elif pos["tp"] and price <= pos["tp"]:
                closed.append(engine.close(pos, pos["tp"], "TP"))
    for rec in closed:
        record_trade(rec)
        g = st["grids"].get(inst)
        if g:
            for k, pid in list(g["taken"].items()):
                if pid == rec["id"]:
                    g["taken"].pop(k, None)
                    break
        log(f"CLOSE #{rec['id']} {inst} {rec['side']} {rec['tag']} "
            f"{rec['reason']} pnl={rec['pnl']:+.2f} eq={st['equity']:.2f}")
        if rec["tag"] == "scalp" and rec["reason"] == "SL":
            st["cooldown_until"] = time.time() + CFG["scalp"]["cooldown_after_sl_min"] * 60
            log("Scalp cooldown 30p sau SL")
    return bool(closed)


def manage_scalp(engine, st, inst, price, c5, c15):
    s = CFG["scalp"]
    n_scalp = sum(1 for p in st["positions"] if p["tag"] == "scalp")
    if n_scalp >= s["max_positions"]:
        return False
    if time.time() < st["cooldown_until"]:
        return False
    sig, info = strategy.scalp_signal(c5, c15, CFG)
    if not sig:
        return False
    if any(p["tag"] == "scalp" and p["inst"] == inst and p["side"] == sig
           for p in st["positions"]):
        return False
    notional = CFG["order_margin_usdt"] * CFG["leverage"]
    pos, why = engine.open(inst, sig, notional, price,
                           s["sl_pct"], s["tp_pct"], "scalp")
    if pos:
        log(f"OPEN #{pos['id']} {inst} scalp {sig} entry={pos['entry']:.4f} "
            f"sl={pos['sl']:.4f} tp={pos['tp']:.4f} info={info}")
        return True
    log(f"Scalp {inst} {sig} rejected: {why}")
    return False


def manage_grid(engine, st, inst, price):
    g = CFG["grid"]
    grid = st["grids"].setdefault(inst, {"anchor": None, "taken": {}})
    step = grid.get("step") or g["step_pct"]
    rng = g["range_steps"] * step
    changed = False
    if grid["anchor"] is None or abs(price / grid["anchor"] - 1) > rng:
        grid["anchor"] = price
        grid["taken"] = {}
        log(f"Grid {inst} rebuild anchor={price:.4f}")
        changed = True
    anchor = grid["anchor"]
    n_grid = sum(1 for p in st["positions"] if p["tag"] == "grid")
    notional = CFG["order_margin_usdt"] * CFG["leverage"]
    for k in range(1, g["levels_each_side"] + 1):
        if n_grid >= g["max_positions"]:
            break
        if len(st["positions"]) >= CFG["max_total_positions"]:
            break
        bk = f"b{k}"
        if price <= anchor * (1 - k * step) and bk not in grid["taken"]:
            pos, _ = engine.open(inst, "long", notional, anchor * (1 - k * step),
                                 0, step, "grid", level=bk)
            if pos:
                grid["taken"][bk] = pos["id"]
                n_grid += 1
                changed = True
                log(f"OPEN #{pos['id']} {inst} grid BUY k={k} @ {pos['entry']:.4f}")
        sk = f"s{k}"
        if price >= anchor * (1 + k * step) and sk not in grid["taken"]:
            pos, _ = engine.open(inst, "short", notional, anchor * (1 + k * step),
                                 0, step, "grid", level=sk)
            if pos:
                grid["taken"][sk] = pos["id"]
                n_grid += 1
                changed = True
                log(f"OPEN #{pos['id']} {inst} grid SELL k={k} @ {pos['entry']:.4f}")
    return changed


def main():
    st = load_state()
    engine = make_engine(st)
    log(f"Bot start MULTI-TICK. {len(INSTS)} insts, fast={FAST_POLL}s, "
        f"equity={st['equity']:.2f} MODE={MODE}")

    ws = TickerWS(INSTS, log)
    ws.start()

    candles = {}
    log("Warmup: fetching candles...")
    for inst in INSTS:
        try:
            candles[inst] = {
                "5m": okx_client.get_candles(inst, "5m", 100),
                "15m": okx_client.get_candles(inst, "15m", 100),
                "ts": time.time(),
            }
        except Exception as e:
            log(f"warmup {inst} failed: {e}")
        time.sleep(0.2)
    log(f"Warmup done: {len(candles)}/{len(INSTS)}")

    prices = {}
    last_rest_px = 0
    last_heartbeat = 0
    last_cfg_reload = 0
    loop = 0
    while True:
        try:
            if os.path.exists(STOP_P):
                log("STOP file -> shutdown")
                ws.stop()
                save_state(st)
                return

            if st["day"] != utc_day():
                st["day"] = utc_day()
                st["day_start_equity"] = st["equity"]
                st["halted"] = False
                st["halt_reason"] = ""
                log(f"New day {st['day']}")

            # ---- prices: WS live, REST fallback at most every 10s ----
            now = time.time()
            if now - last_cfg_reload > 60:
                try:
                    new = json.load(open(os.path.join(BASE, "config.json")))
                    CFG.clear()
                    CFG.update(new)
                except Exception as e:
                    log(f"cfg reload failed: {e}")
                last_cfg_reload = now
            if ws.healthy():
                prices = dict(ws.prices)
            elif now - last_rest_px > 10:
                prices.update(rest_tickers_fallback())
                last_rest_px = now
            if not prices:
                time.sleep(2)
                continue

            dirty = False

            # ---- FAST PATH: SL/TP + grid triggers every tick-loop ----
            for inst in INSTS:
                px = prices.get(inst)
                if px is None:
                    continue
                if update_positions(engine, st, inst, px):
                    dirty = True
            if not st["halted"]:
                for inst in INSTS:
                    px = prices.get(inst)
                    cc = candles.get(inst)
                    if px is None or not cc:
                        continue
                    regime = st["regimes"].get(inst, {}).get("regime", "ranging")
                    if regime == "ranging" and not is_disabled(inst):
                        if manage_grid(engine, st, inst, px):
                            dirty = True

            # ---- SLOW PATH: candles + regime + scalp entries ----
            if loop % SLOW_EVERY == 0:
                due = [i for i in INSTS
                       if now - candles.get(i, {}).get("ts", 0) > CFG["candle_refresh_seconds"]]
                for inst in due[:CANDLE_PER_SLOW]:
                    try:
                        candles[inst] = {
                            "5m": okx_client.get_candles(inst, "5m", 100),
                            "15m": okx_client.get_candles(inst, "15m", 100),
                            "ts": time.time(),
                        }
                    except Exception as e:
                        log(f"candle refresh {inst} failed: {e}")
                if not st["halted"]:
                    for inst in INSTS:
                        px = prices.get(inst)
                        cc = candles.get(inst)
                        if px is None or not cc or not cc.get("15m"):
                            continue
                        regime, av = strategy.detect_regime(cc["15m"], CFG["adx_threshold"])
                        prev = st["regimes"].get(inst, {}).get("regime")
                        st["regimes"][inst] = {"regime": regime, "adx": av}
                        if regime != prev and prev is not None:
                            log(f"{inst} regime -> {regime} (adx={av})")
                        # ATR-adaptive grid step cho lan rebuild ke tiep
                        try:
                            a = atr(cc["15m"], 14)
                            apct = a / px if a and px else None
                        except Exception:
                            apct = None
                        g = CFG["grid"]
                        if apct:
                            astep = round(min(g["step_max"],
                                              max(g["step_min"],
                                                  g["step_mult"] * apct)), 6)
                        else:
                            astep = g["step_pct"]
                        st["grids"].setdefault(
                            inst, {"anchor": None, "taken": {}})["step"] = astep
                        if regime == "trending" and not is_disabled(inst):
                            if manage_scalp(engine, st, inst, px, cc["5m"], cc["15m"]):
                                dirty = True

            # ---- daily stop ----
            if st["day_start_equity"] > 0:
                dp = (st["equity"] - st["day_start_equity"]) / st["day_start_equity"]
                if not st["halted"] and dp <= -CFG["risk"]["daily_max_loss_pct"]:
                    st["halted"] = True
                    st["halt_reason"] = f"daily stop {dp*100:.2f}%"
                    for pos in list(st["positions"]):
                        rec = engine.close(pos, prices.get(pos["inst"], pos["entry"]),
                                           "DAILY_STOP")
                        record_trade(rec)
                    st["grids"] = {}
                    dirty = True
                    log(f"HALTED {st['halt_reason']} eq={st['equity']:.2f}")

            if dirty or loop % 20 == 0:
                save_state(st)
            loop += 1
            if now - last_heartbeat > 600:
                u = engine.unrealized(prices)
                log(f"heartbeat eq={st['equity']:.2f} unreal={u:+.2f} "
                    f"pos={len(st['positions'])} ws={ws.healthy()}")
                last_heartbeat = now
        except Exception:
            log("LOOP ERROR:\n" + traceback.format_exc())
        time.sleep(FAST_POLL)


if __name__ == "__main__":
    main()
