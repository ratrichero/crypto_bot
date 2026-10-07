#!/usr/bin/env python3
"""Multi-symbol Binance USDT-M futures bot: scalp 5m/15m + two-way grid.

Port of trading-bot/bot.py (OKX) to Binance. Strategy rules are identical;
only the venue layer differs (see live_binance.py for Binance specifics).

Price ticks arrive real-time via websocket (miniTicker stream). SL/TP +
grid triggers are checked on a fast loop (~0.5s); candle/strategy work runs
on a slow loop. Kill switch: create file STOP in this dir.

Modes (config "mode"): "data_only" | "dry_run" | "live"
(default "dry_run").
  data_only: public market data/candles and signal calculations only; no
             simulated positions and no authenticated calls.
  dry_run: signals flow through BinanceEngine but ONLY LOGGED, zero
           authenticated calls, no API key needed.
  live:    places real orders on Binance, needs env BINANCE_API_KEY /
           BINANCE_API_SECRET. DOI MODE CAN RESTART BOT.
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone

import binance_client
import binance_safety
import strategy
from indicators import atr
from binance_ws import BinanceWS

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json")))
UNI = json.load(open(os.path.join(BASE, CFG.get("universe_file", "universe.json"))))
SYMBOLS = [u["symbol"] for u in UNI]
STATE_P = os.path.join(BASE, "state.json")
TRADES_P = os.path.join(BASE, "trades.jsonl")
LOG_P = os.path.join(BASE, "bot.log")
STOP_P = os.path.join(BASE, "STOP")

FAST_POLL = CFG.get("fast_poll_seconds", 0.5)
SLOW_EVERY = 10          # slow tasks every N fast loops (~5s)
CANDLE_PER_SLOW = 2      # symbols refreshed per slow tick
WARMUP_DELAY = float(CFG.get("warmup_delay_seconds", 0.75))
MODE = CFG.get("mode", "dry_run")
DATA_ONLY = MODE == "data_only"
LOCK_P = os.path.join(BASE, "bot.lock")
CIRCUIT_P = os.path.join(BASE, "binance_circuit.json")


def make_engine(st):
    if MODE == "data_only":
        from live_binance import DataOnlyEngine
        return DataOnlyEngine(log=log)
    if MODE in ("dry_run", "live"):
        from live_binance import BinanceEngine
        eng = BinanceEngine(CFG, st, dry_run=(MODE == "dry_run"),
                            log=log, symbols=SYMBOLS)
        # Khoi tao ket noi Postgres de ghi trade truc tiep
        # (khong fail neu DB khong dung duoc; JSONL van la backup)
        try:
            eng.init_db()
        except Exception as e:
            log(f"DB init warning: {e}")
        return eng
    raise SystemExit(
        "config 'mode' khong hop le: %r (chon data_only|dry_run|live)" %
        (MODE,))


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
        "intraday_peak_equity": CFG["start_equity"],
        "mark_equity": CFG["start_equity"],
        "unrealized_pnl": 0.0,
        "daily_drawdown_pct": 0.0,
        "_risk_initialized": False,
        "signal_bars": {},
        "halted": False,
        "halt_reason": "",
        "cooldown_until": 0,
        "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0},
        "_pid": 0,
        "_client_nonce": 0,
    }


def load_state():
    if os.path.exists(STATE_P):
        try:
            st = json.load(open(STATE_P))
            d = default_state()
            d.update(st)
            d.setdefault("grids", {})
            d.setdefault("regimes", {})
            d.setdefault("signal_bars", {})
            d.setdefault("intraday_peak_equity", d.get("day_start_equity", d["equity"]))
            d.setdefault("mark_equity", d.get("equity", 0.0))
            d.setdefault("unrealized_pnl", 0.0)
            d.setdefault("daily_drawdown_pct", 0.0)
            d.setdefault("_risk_initialized", False)
            return d
        except Exception:
            pass
    return default_state()


def save_state(st):
    """Atomically persist state; stop opening risk if persistence fails."""
    tmp = STATE_P + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(st, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_P)
    except Exception:
        st["halted"] = True
        st["halt_reason"] = "state persistence failure"
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def record_trade(rec):
    with open(TRADES_P, "a") as f:
        f.write(json.dumps(rec) + "\n")


def rest_tickers_fallback():
    """Low-weight REST fallback; never hide a rate-limit/circuit signal."""
    try:
        rows = binance_client.get_symbol_prices()
        want = set(SYMBOLS)
        return {r["symbol"]: float(r["price"]) for r in rows
                if r["symbol"] in want and r.get("price")}
    except binance_safety.BinanceSafetyStop:
        raise
    except Exception as e:
        log(f"fallback prices failed: {e}")
        return {}


def is_disabled(symbol):
    return CFG.get("disabled_symbols", {}).get(symbol, 0) > time.time()


def _record_close(st, rec):
    """Persist a close and update grid/scalp bookkeeping."""
    try:
        record_trade(rec)
    except Exception:
        st["halted"] = True
        st["halt_reason"] = "state persistence failure"
        raise
    symbol = rec["symbol"]
    g = st["grids"].get(symbol)
    if g:
        for key, pid in list(g.get("taken", {}).items()):
            if pid == rec["id"]:
                g["taken"].pop(key, None)
                break
    log(f"CLOSE #{rec['id']} {symbol} {rec['side']} {rec['tag']} "
        f"{rec['reason']} pnl={rec['pnl']:+.2f} eq={st['equity']:.2f}")
    if rec["tag"] == "scalp" and rec["reason"] == "SL":
        st["cooldown_until"] = (time.time()
                               + CFG["scalp"]["cooldown_after_sl_min"] * 60)
        log("Scalp cooldown 30p sau SL")


def update_positions(engine, st, symbol, price):
    """Tick-driven SL/TP check using the mark price.

    With an armed exchange guard Binance executes the exit itself; the bot
    only falls back to its own market close after a grace period
    (engine.defer_local_exit), so both sides never race the same trigger.
    """
    closed = []
    defer = getattr(engine, "defer_local_exit", None)
    clear = getattr(engine, "clear_local_trigger", None)
    for pos in [p for p in st["positions"] if p["symbol"] == symbol]:
        label = None
        if pos["side"] == "long":
            if pos["sl"] and price <= pos["sl"]:
                label = "sl"
            elif pos["tp"] and price >= pos["tp"]:
                label = "tp"
        else:
            if pos["sl"] and price >= pos["sl"]:
                label = "sl"
            elif pos["tp"] and price <= pos["tp"]:
                label = "tp"
        if label is None:
            if clear:
                clear(pos)
            continue
        if defer and defer(pos, label):
            continue
        rec = engine.close(pos, pos[label], label.upper())
        if rec:
            if clear:
                clear(pos)
            closed.append(rec)
    for rec in closed:
        _record_close(st, rec)
    return bool(closed)


def manage_scalp(engine, st, symbol, price, c5, c15):
    s = CFG["scalp"]
    n_scalp = sum(1 for p in st["positions"] if p["tag"] == "scalp")
    if n_scalp >= s["max_positions"]:
        return False
    if time.time() < st["cooldown_until"]:
        return False
    closed5 = c5[:-1] if len(c5) > 1 else c5
    bar_ts = closed5[-1].get("ts") if closed5 else None
    signal_bars = st.setdefault("signal_bars", {})
    if bar_ts is not None and signal_bars.get(symbol) == bar_ts:
        return False
    if bar_ts is not None:
        # Consume a closed candle once, even if it has no signal or the order
        # is rejected. This prevents re-opening the same breakout after TP.
        signal_bars[symbol] = bar_ts
    sig, info = strategy.scalp_signal(c5, c15, CFG)
    if not sig:
        return False
    if any(p["tag"] == "scalp" and p["symbol"] == symbol and p["side"] == sig
           for p in st["positions"]):
        return False
    if any(p["tag"] == "scalp" and p["symbol"] == symbol and p["side"] != sig
           for p in st["positions"]):
        return False
    notional = CFG["order_margin_usdt"] * CFG["leverage"]
    pos, why = engine.open(symbol, sig, notional, price,
                           s["sl_pct"], s["tp_pct"], "scalp")
    if pos:
        log(f"OPEN #{pos['id']} {symbol} scalp {sig} entry={pos['entry']:.4f} "
            f"sl={pos['sl']:.4f} tp={pos['tp']:.4f} info={info}")
        return True
    if str(why).startswith(("cooldown", "symbol cooldown")):
        return False
    log(f"Scalp {symbol} {sig} rejected: {why}")
    return False


def acquire_instance_lock():
    """Prevent two bot copies from sharing the same IP/request budget."""
    try:
        import fcntl
    except ImportError:
        log("WARNING fcntl unavailable; cannot enforce single-instance lock")
        return None
    fh = open(LOCK_P, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit(
            "binance-bot dang chay (lock=%s); khong khoi dong them instance" %
            LOCK_P
        )
    fh.seek(0)
    fh.truncate()
    fh.write("pid=%d\\n" % os.getpid())
    fh.flush()
    return fh


def manage_grid_risk(engine, st, mark_prices):
    """Stop a grid basket before an unbounded one-way move consumes equity."""
    limit_pct = float(CFG["risk"].get("grid_basket_max_loss_pct", 0.0))
    if limit_pct <= 0:
        return False
    base_equity = float(st.get("mark_equity", st.get("equity", 0.0)) or 0.0)
    if base_equity <= 0:
        return False
    changed = False
    grid_symbols = set(st.get("grids", {})) | {
        p.get("symbol") for p in st["positions"] if p.get("tag") == "grid"
    }
    for symbol in sorted(s for s in grid_symbols if s):
        grid = st["grids"].setdefault(
            symbol, {"anchor": None, "taken": {}}
        )
        if grid.get("risk_halted"):
            continue
        positions = [p for p in st["positions"]
                     if p.get("tag") == "grid" and p.get("symbol") == symbol]
        if not positions:
            continue
        price = mark_prices.get(symbol)
        if price is None:
            continue
        pnl = 0.0
        for pos in positions:
            if pos["side"] == "long":
                pnl += (price - pos["entry"]) * pos["qty"]
            else:
                pnl += (pos["entry"] - price) * pos["qty"]
        loss_limit = base_equity * limit_pct
        if pnl > -loss_limit:
            continue
        grid["risk_halted"] = True
        grid["rebuild_pending"] = True
        log("GRID BASKET STOP %s pnl=%+.2f limit=-%.2f positions=%d" %
            (symbol, pnl, loss_limit, len(positions)))
        for pos in list(positions):
            rec = engine.close(pos, price, "GRID_BASKET_STOP")
            if rec:
                _record_close(st, rec)
                changed = True
        grid["taken"] = {}
    return changed


def manage_grid(engine, st, symbol, price):
    g = CFG["grid"]
    grid = st["grids"].setdefault(symbol, {"anchor": None, "taken": {}})
    grid.setdefault("anchor", None)
    grid.setdefault("taken", {})
    if grid.get("risk_halted"):
        return False
    step = grid.get("step") or g["step_pct"]
    rng = g["range_steps"] * step
    changed = False
    active = [p for p in st["positions"]
              if p.get("tag") == "grid" and p.get("symbol") == symbol]
    # Preserve level ownership across an anchor cycle. Never forget live lots.
    for pos in active:
        if pos.get("level") is not None:
            grid.setdefault("taken", {}).setdefault(pos["level"], pos["id"])
    if grid.get("rebuild_pending") and not active:
        grid["anchor"] = price
        grid["taken"] = {}
        grid["rebuild_pending"] = False
        log(f"Grid {symbol} rebuild anchor={price:.4f}")
        changed = True
    elif grid["anchor"] is None:
        grid["anchor"] = price
        grid["rebuild_pending"] = False
        log(f"Grid {symbol} rebuild anchor={price:.4f}")
        changed = True
    elif abs(price / grid["anchor"] - 1) > rng:
        if active:
            # Existing lots must be flattened or reach their exits before a
            # new anchor is allowed; otherwise old and new grids overlap.
            if not grid.get("rebuild_pending"):
                log("Grid %s trend break; freeze new levels until flat" % symbol)
            grid["rebuild_pending"] = True
            return changed
        grid["anchor"] = price
        grid["taken"] = {}
        grid["rebuild_pending"] = False
        log(f"Grid {symbol} rebuild anchor={price:.4f}")
        changed = True
    if grid.get("rebuild_pending"):
        return changed
    anchor = grid["anchor"]
    n_grid = sum(1 for p in st["positions"] if p["tag"] == "grid")
    notional = CFG["order_margin_usdt"] * CFG["leverage"]
    entries = 0
    max_entries = int(g.get("max_entries_per_cycle", 1))
    for k in range(1, g["levels_each_side"] + 1):
        if entries >= max_entries or n_grid >= g["max_positions"]:
            break
        if len(st["positions"]) >= CFG["max_total_positions"]:
            break
        bk = f"b{k}"
        if price <= anchor * (1 - k * step) and bk not in grid["taken"]:
            pos, _ = engine.open(symbol, "long", notional,
                                 anchor * (1 - k * step),
                                 0.03, step, "grid", level=bk)
            if pos:
                grid["taken"][bk] = pos["id"]
                n_grid += 1
                entries += 1
                changed = True
                log(f"OPEN #{pos['id']} {symbol} grid BUY k={k} @ {pos['entry']:.4f}")
        if entries >= max_entries:
            break
        sk = f"s{k}"
        if price >= anchor * (1 + k * step) and sk not in grid["taken"]:
            pos, _ = engine.open(symbol, "short", notional,
                                 anchor * (1 + k * step),
                                 0.03, step, "grid", level=sk)
            if pos:
                grid["taken"][sk] = pos["id"]
                n_grid += 1
                entries += 1
                changed = True
                log(f"OPEN #{pos['id']} {symbol} grid SELL k={k} @ {pos['entry']:.4f}")
    return changed


def main():
    # One process per host/IP.  This lock is held for the lifetime of main.
    lock_handle = acquire_instance_lock()
    binance_client.configure(log, state_path=CIRCUIT_P)
    try:
        binance_safety.ensure_allowed()
    except binance_safety.BinanceSafetyStop as e:
        log("START BLOCKED by Binance safety circuit: %s" % e)
        return

    st = load_state()
    try:
        engine = make_engine(st)
    except binance_safety.BinanceSafetyStop as e:
        log("START STOPPED by Binance safety circuit: %s" % e)
        save_state(st)
        return
    except Exception:
        log("ENGINE START ERROR:\n" + traceback.format_exc())
        save_state(st)
        return
    log(f"Bot start MULTI-TICK (Binance). {len(SYMBOLS)} symbols, "
        f"fast={FAST_POLL}s, equity={st['equity']:.2f} MODE={MODE} "
        f"single_instance={bool(lock_handle)}")

    if MODE == "live":
        try:
            engine.start_user_stream()
        except binance_safety.BinanceSafetyStop as e:
            log("USER WS START STOPPED by Binance safety circuit: %s" % e)
            save_state(st)
            return
        except Exception:
            log("USER WS START ERROR:\n" + traceback.format_exc())
            save_state(st)
            return

    ws = BinanceWS(SYMBOLS, log)
    ws.start()

    candles = {}
    log("Warmup: fetching candles with shared session/rate limiter...")
    for symbol in SYMBOLS:
        try:
            candles[symbol] = {
                "5m": binance_client.get_klines(symbol, "5m", 100),
                "15m": binance_client.get_klines(symbol, "15m", 100),
                "ts": time.time(),
            }
        except binance_safety.BinanceSafetyStop as e:
            log("WARMUP STOPPED by Binance safety circuit: %s" % e)
            ws.stop()
            getattr(engine, "stop_user_stream", lambda: None)()
            save_state(st)
            return
        except Exception as e:
            log(f"warmup {symbol} failed: {e}")
        # Stagger symbols so startup does not create a request burst.
        time.sleep(WARMUP_DELAY)
    log(f"Warmup done: {len(candles)}/{len(SYMBOLS)}")

    prices = {}
    mark_prices = {}
    last_rest_px = 0
    last_equity_refresh = 0
    last_heartbeat = 0
    last_cfg_reload = 0
    loop = 0
    while True:
        try:
            if os.path.exists(STOP_P):
                log("STOP file -> shutdown")
                ws.stop()
                getattr(engine, "stop_user_stream", lambda: None)()
                save_state(st)
                binance_client.close()
                return
            if ws.fatal_error:
                raise binance_safety.BinanceSafetyStop(ws.fatal_error)
            user_error = getattr(engine, "user_stream_error", lambda: None)()
            if user_error:
                raise binance_safety.BinanceSafetyStop(user_error)
            binance_safety.ensure_allowed()

            # ---- prices: routed WS live, low-weight REST fallback ----
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
                prices = ws.snapshot()
                marks = ws.mark_snapshot()
                mark_prices = {
                    symbol: marks.get(symbol, price)
                    for symbol, price in prices.items()
                }
            elif now - last_rest_px > 30:
                # /fapi/v2/ticker/price without symbol has weight 2, versus
                # weight 40 for the all-symbol 24h statistics endpoint.
                prices = rest_tickers_fallback()
                mark_prices = dict(prices)
                last_rest_px = now
            if not prices:
                time.sleep(2)
                continue

            dirty = False

            # Mark-to-market equity is the risk source of truth. In live mode
            # this reads Binance account margin balance; dry-run estimates it
            # from local positions and the mark price feed.
            risk_equity_ready = False
            if (DATA_ONLY or
                    now - last_equity_refresh >= float(
                        CFG.get("equity_refresh_seconds", 5))):
                try:
                    if not DATA_ONLY:
                        mtm = engine.mark_to_market_equity(mark_prices)
                        st["mark_equity"] = float(mtm)
                        st["unrealized_pnl"] = float(
                            engine.unrealized(mark_prices)
                        )
                        risk_equity_ready = True
                    last_equity_refresh = now
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    last_equity_refresh = now
                    st["halted"] = True
                    st["halt_reason"] = "equity unavailable"
                    log("CRITICAL mark-to-market equity unavailable: %s" % e)
                    dirty = True
            if DATA_ONLY:
                st["mark_equity"] = st.get("equity", 0.0)
                st["unrealized_pnl"] = 0.0
                risk_equity_ready = True

            today = utc_day()
            if risk_equity_ready and (
                    st.get("day") != today or not st.get("_risk_initialized")):
                persistent_halt = str(st.get("halt_reason", "")).startswith(
                    ("exchange ", "unmanaged ", "position ", "open order ",
                     "ambiguous ", "order fill ", "partial ", "close ",
                     "state persistence ")
                )
                st["day"] = today
                st["day_start_equity"] = st["mark_equity"]
                st["intraday_peak_equity"] = st["mark_equity"]
                st["_risk_initialized"] = True
                st["halted"] = persistent_halt
                if not persistent_halt:
                    st["halt_reason"] = ""
                    for symbol, grid in st.get("grids", {}).items():
                        if not any(p.get("tag") == "grid"
                                   and p.get("symbol") == symbol
                                   for p in st["positions"]):
                            grid["risk_halted"] = False
                            grid["rebuild_pending"] = False
                log(f"New risk day {today} baseline={st['day_start_equity']:.2f}")

            if st.get("_risk_initialized"):
                if st["mark_equity"] > st.get("intraday_peak_equity", 0):
                    st["intraday_peak_equity"] = st["mark_equity"]
                if st["day_start_equity"] > 0:
                    st["daily_drawdown_pct"] = (
                        st["mark_equity"] - st["day_start_equity"]
                    ) / st["day_start_equity"]


            if MODE == "live":
                # Vong doi SL/TP tren san (ALGO_UPDATE + query theo algoId):
                # guard khop -> ghi PnL that tu fill + huy guard con lai;
                # guard bi huy/het han/tu choi -> danh dau dat lai.
                try:
                    for rec in engine.sync_exchange_protection():
                        _record_close(st, rec)
                        dirty = True
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    log(f"sync_exchange_protection loi: {e}")
                # Phat hien vi the bi TP/SL tren san dong ma bot chua biet
                # (VD khi dang halt hoac miss WS): ghi trade uoc tinh truoc
                # khi reconcile, de mismatch tu het thay vi treo halt.
                try:
                    for rec in engine.detect_exchange_closed(mark_prices):
                        _record_close(st, rec)
                        dirty = True
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    log(f"detect_exchange_closed loi: {e}")
            if MODE == "live" and not engine.reconcile_positions():
                if not st["halted"]:
                    st["halted"] = True
                    st["halt_reason"] = "position reconciliation unavailable"
                dirty = True

            # ---- daily stop before every entry path
            # The mark-to-market loss guard must run before FAST and SLOW
            # strategy evaluation. Otherwise an iteration can open a new
            # position and only then discover that the account crossed the
            # daily loss limit.
            if (not DATA_ONLY and st.get("_risk_initialized")
                    and st["day_start_equity"] > 0):
                dp = st.get("daily_drawdown_pct", 0.0)
                if (not st["halted"]
                        and dp <= -float(CFG["risk"]["daily_max_loss_pct"])):
                    st["halted"] = True
                    st["halt_reason"] = f"daily stop {dp*100:.2f}%"
                    for pos in list(st["positions"]):
                        rec = engine.close(
                            pos,
                            mark_prices.get(pos["symbol"], pos["entry"]),
                            "DAILY_STOP",
                        )
                        if rec:
                            _record_close(st, rec)
                    for grid in st.get("grids", {}).values():
                        grid["risk_halted"] = True
                        grid["rebuild_pending"] = True
                    dirty = True
                    log("HALTED %s mark_equity=%.2f day_start=%.2f" %
                        (st["halt_reason"], st["mark_equity"],
                         st["day_start_equity"]))

            # ---- FAST PATH: only trading modes may mutate positions/orders
            reconcile_hold = str(st.get("halt_reason", "")).startswith(
                ("exchange position", "exchange protection reconciliation",
                 "unmanaged ", "position ")
            )
            if not DATA_ONLY:
                if not reconcile_hold:
                    if manage_grid_risk(engine, st, mark_prices):
                        dirty = True
                    for symbol in SYMBOLS:
                        mark = mark_prices.get(symbol, prices.get(symbol))
                        if mark is None:
                            continue
                        if update_positions(engine, st, symbol, mark):
                            dirty = True
                    # Retry dat protection cho vi the chua co SL/TP tren san
                    try:
                        if engine.retry_protection():
                            dirty = True
                    except Exception as e:
                        log(f"retry_protection loi: {e}")
                    # Quet don lenh mo coi moi 30 giay
                    try:
                        now_ts = time.time()
                        last_cleanup = st.get("_last_orphan_cleanup", 0)
                        if now_ts - last_cleanup >= 30:
                            cleaned = engine.cleanup_orphan_orders()
                            st["_last_orphan_cleanup"] = now_ts
                            if cleaned:
                                log(f"CLEANUP: da xoa {cleaned} lenh mo coi")
                                dirty = True
                    except Exception as e:
                        log(f"orphan cleanup loi: {e}")
                # Kiem tra PAUSE file - neu co thi khong mo lenh moi
                paused = os.path.exists(os.path.join(BASE, "PAUSE"))
                if not st["halted"] and not paused:
                    for symbol in SYMBOLS:
                        px = prices.get(symbol)
                        cc = candles.get(symbol)
                        if px is None or not cc:
                            continue
                        regime = st["regimes"].get(symbol, {}).get("regime", "ranging")
                        if regime == "ranging" and not is_disabled(symbol):
                            if manage_grid(engine, st, symbol, px):
                                dirty = True

            # ---- SLOW PATH: candles + regime + optional scalp entries ----
            if loop % SLOW_EVERY == 0:
                due = [s for s in SYMBOLS
                       if now - candles.get(s, {}).get("ts", 0)
                       > CFG["candle_refresh_seconds"]]
                for symbol in due[:CANDLE_PER_SLOW]:
                    try:
                        candles[symbol] = {
                            "5m": binance_client.get_klines(symbol, "5m", 100),
                            "15m": binance_client.get_klines(symbol, "15m", 100),
                            "ts": time.time(),
                        }
                        time.sleep(0.5)  # them gian request giua cac symbol
                    except binance_safety.BinanceSafetyStop:
                        raise
                    except Exception as e:
                        log(f"candle refresh {symbol} failed: {e}")
                if not st["halted"]:
                    for symbol in SYMBOLS:
                        px = prices.get(symbol)
                        cc = candles.get(symbol)
                        if px is None or not cc or not cc.get("15m"):
                            continue
                        rec = st["regimes"].get(symbol, {})
                        regime = rec.get("regime", "ranging")
                        closed15 = cc["15m"][:-1] if len(cc["15m"]) > 1 else cc["15m"]
                        regime_bar_ts = closed15[-1].get("ts") if closed15 else None
                        if regime_bar_ts != rec.get("bar_ts"):
                            previous = rec.get("regime")
                            candidate, av = strategy.detect_regime(
                                cc["15m"],
                                CFG["adx_threshold"],
                                previous=previous,
                                range_threshold=CFG.get("adx_range_threshold"),
                            )
                            candidate_name = rec.get("candidate")
                            candidate_count = int(rec.get("candidate_count", 0))
                            if previous is None:
                                regime = candidate
                                candidate_name = None
                                candidate_count = 0
                            elif candidate == previous:
                                regime = previous
                                candidate_name = None
                                candidate_count = 0
                            elif candidate == candidate_name:
                                candidate_count += 1
                                if candidate_count >= int(CFG.get(
                                        "regime_confirm_bars", 2)):
                                    regime = candidate
                                    candidate_name = None
                                    candidate_count = 0
                            else:
                                candidate_name = candidate
                                candidate_count = 1
                            rec = {
                                "regime": regime,
                                "adx": av,
                                "bar_ts": regime_bar_ts,
                                "candidate": candidate_name,
                                "candidate_count": candidate_count,
                            }
                            st["regimes"][symbol] = rec
                            if regime != previous and previous is not None:
                                log(f"{symbol} regime -> {regime} (adx={av})")
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
                            symbol, {"anchor": None, "taken": {}})["step"] = astep
                        if (not DATA_ONLY and regime == "trending"
                                and not is_disabled(symbol)):
                            if manage_scalp(engine, st, symbol,
                                            px, cc["5m"], cc["15m"]):
                                dirty = True

            if dirty or loop % 20 == 0:
                save_state(st)
            loop += 1
            if now - last_heartbeat > 600:
                log(f"heartbeat wallet={st['equity']:.2f} "
                    f"mark_equity={st.get('mark_equity', st['equity']):.2f} "
                    f"unreal={st.get('unrealized_pnl', 0.0):+.2f} "
                    f"daily_dd={st.get('daily_drawdown_pct', 0.0)*100:+.2f}% "
                    f"pos={len(st['positions'])} ws={ws.healthy()} mode={MODE}")
                last_heartbeat = now
        except binance_safety.BinanceSafetyStop as e:
            # Critical rule: 429/418/-1003 and a persisted circuit stop all
            # REST/private trading; do not let the 0.5s loop retry it.
            log("SAFETY STOP: %s" % e)
            ws.stop()
            getattr(engine, "stop_user_stream", lambda: None)()
            save_state(st)
            binance_client.close()
            return
        except Exception:
            log("LOOP ERROR:\n" + traceback.format_exc())
        time.sleep(FAST_POLL)


if __name__ == "__main__":
    main()
