#!/usr/bin/env python3
"""Self-optimizer chay moi sang: phan tich lich su lenh paper va tu dieu
chinh tham so TRONG BIEN AN TOAN. Khong bao gio cham vao: don bay,
size lenh, tong so lenh toi da, dung lo ngay, phi.

Can toi thieu 20 lenh dong trong 7 ngay qua, neu khong se bo qua.
"""
import json
import os
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_P = os.path.join(BASE, "config.json")
TRADES_P = os.path.join(BASE, "trades.jsonl")
LOG_P = os.path.join(BASE, "optimizer.log")
STATE_P = os.path.join(BASE, "optimizer_state.json")

MIN_TRADES = 20
LOOKBACK_DAYS = 7

# param -> (min, max, step)
BOUNDS = {
    "scalp.tp_pct": (0.004, 0.012, 0.001),
    "scalp.sl_pct": (0.002, 0.006, 0.0005),
    "scalp.rsi_long_max": (60, 80, 5),
    "scalp.rsi_short_min": (20, 40, 5),
    "scalp.max_positions": (2, 5, 1),
    "adx_threshold": (15, 30, 2),
    "grid.step_mult": (0.8, 2.0, 0.2),
    "grid.step_min": (0.003, 0.007, 0.001),
    "grid.levels_each_side": (3, 7, 1),
}

PROTECTED = ("leverage", "order_margin_usdt", "max_total_positions",
             "risk", "fee_rate", "slippage", "start_equity",
             "universe_file", "poll_seconds", "fast_poll_seconds",
             "candle_refresh_seconds")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_P, "a") as f:
        f.write(f"[{ts}Z] {msg}\n")
    print(msg)


def get_path(cfg, dotted):
    cur = cfg
    for k in dotted.split("."):
        cur = cur[k]
    return cur


def set_path(cfg, dotted, val):
    cur = cfg
    ks = dotted.split(".")
    for k in ks[:-1]:
        cur = cur[k]
    cur[ks[-1]] = val


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def nudge(cfg, dotted, direction, actions, reason):
    """direction: +1 / -1. Tra ve True neu co thay doi that."""
    lo, hi, step = BOUNDS[dotted]
    old = get_path(cfg, dotted)
    new = clamp(round(old + direction * step, 6), lo, hi)
    if new == old:
        return False
    set_path(cfg, dotted, new)
    actions.append(f"{dotted}: {old} -> {new} ({reason})")
    return True


def stats(trades, fee_rate=0.0):
    """PF va pnl theo NET tron vong (tru phi vao lenh)."""
    n = len(trades)
    net = [(t["pnl"] - t.get("notional", 0) * fee_rate) for t in trades]
    wins = [p for p in net if p > 0]
    gross_w = sum(wins)
    gross_l = -sum(p for p in net if p <= 0)
    pf = (gross_w / gross_l) if gross_l > 0 else (99.0 if gross_w > 0 else 0.0)
    return {"n": n, "wr": len(wins) / n if n else 0, "pf": pf,
            "pnl": sum(net)}


def main():
    cfg = json.load(open(CFG_P))
    fee_rate = cfg.get("fee_rate", 0.0005)
    for p in PROTECTED:
        pass  # tai lieu: cac khoa nay optimizer khong bao gio sua
    if not os.path.exists(TRADES_P):
        log("SKIP: chua co lich su lenh")
        return
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    trades = []
    for line in open(TRADES_P):
        line = line.strip()
        if not line:
            continue
        t = json.loads(line)
        if t.get("closed_at", 0) >= cutoff:
            trades.append(t)
    if len(trades) < MIN_TRADES:
        log(f"SKIP: chi co {len(trades)} lenh/7 ngay (can >= {MIN_TRADES})")
        return

    actions = []
    by_tag = {}
    for t in trades:
        by_tag.setdefault(t["tag"], []).append(t)
    sc = stats(by_tag.get("scalp", []), fee_rate)
    gr = stats(by_tag.get("grid", []), fee_rate)

    # --- scalp ---
    if sc["n"] >= 15:
        if sc["pf"] < 0.8:
            # thua: an TP xa hon de bu phi
            nudge(cfg, "scalp.tp_pct", +1, actions,
                  f"scalp PF={sc['pf']:.2f} (<0.8), n={sc['n']}")
        elif sc["pf"] > 1.5 and sc["wr"] > 0.55:
            nudge(cfg, "scalp.tp_pct", -1, actions,
                  f"scalp PF={sc['pf']:.2f} tot, chot som hon")
        if sc["wr"] < 0.35:
            # winrate thap: loc tin hieu chat hon
            nudge(cfg, "scalp.rsi_long_max", -1, actions,
                  f"scalp winrate={sc['wr']:.0%} (<35%)")
            nudge(cfg, "scalp.rsi_short_min", +1, actions,
                  f"scalp winrate={sc['wr']:.0%} (<35%)")
        if sc["pf"] < 0.6:
            nudge(cfg, "scalp.max_positions", -1, actions,
                  f"scalp PF={sc['pf']:.2f} (<0.6) -> giam so lenh")
        elif sc["pf"] > 1.8:
            nudge(cfg, "scalp.max_positions", +1, actions,
                  f"scalp PF={sc['pf']:.2f} (>1.8) -> tang so lenh")

    # --- grid ---
    if gr["n"] >= 15:
        if gr["pf"] < 0.8:
            nudge(cfg, "grid.step_mult", +1, actions,
                  f"grid PF={gr['pf']:.2f} (<0.8) -> luoi thua hon")
            nudge(cfg, "grid.step_min", +1, actions,
                  f"grid PF={gr['pf']:.2f} (<0.8) -> nang bien loi nhuan toi thieu")
        elif gr["pf"] > 2.0:
            nudge(cfg, "grid.step_mult", -1, actions,
                  f"grid PF={gr['pf']:.2f} (>2) -> luoi day hon")

    # --- ADX threshold: keo ve phia chien luoc dang thang ---
    if sc["n"] >= 10 and gr["n"] >= 10:
        if gr["pf"] > sc["pf"] + 0.5:
            nudge(cfg, "adx_threshold", +1, actions,
                  f"grid PF={gr['pf']:.2f} > scalp PF={sc['pf']:.2f} -> uu tien ranging")
        elif sc["pf"] > gr["pf"] + 0.5:
            nudge(cfg, "adx_threshold", -1, actions,
                  f"scalp PF={sc['pf']:.2f} > grid PF={gr['pf']:.2f} -> uu tien trending")

    # --- disable coin thua lien tuc ---
    by_inst = {}
    for t in trades:
        by_inst.setdefault(t.get("inst", "?"), []).append(t)
    disabled = cfg.get("disabled_insts", {})
    now = time.time()
    # go bo coin het han disable
    for inst in [i for i, until in disabled.items() if until <= now]:
        del disabled[inst]
        actions.append(f"{inst}: mo lai sau thoi gian tam nghi")
    for inst, ts_ in by_inst.items():
        s = stats(ts_, fee_rate)
        if s["n"] >= 8 and s["pnl"] < -25 and inst not in disabled:
            disabled[inst] = now + 48 * 3600
            actions.append(f"{inst}: TAM DUNG 48h (pnl {s['pnl']:+.1f}/{s['n']} lenh)")
    cfg["disabled_insts"] = disabled

    if actions:
        tmp = CFG_P + ".tmp"
        json.dump(cfg, open(tmp, "w"), indent=1)
        os.replace(tmp, CFG_P)
        for a in actions:
            log("ADJUST: " + a)
    else:
        log(f"OK: khong can dieu chinh (scalp PF={sc['pf']:.2f} n={sc['n']}, "
            f"grid PF={gr['pf']:.2f} n={gr['n']})")

    st = {"ts": int(time.time()),
          "trades_7d": len(trades),
          "scalp": sc, "grid": gr,
          "actions": actions[-10:]}
    json.dump(st, open(STATE_P, "w"), indent=1)
    print("ACTIONS:", len(actions))


if __name__ == "__main__":
    main()
