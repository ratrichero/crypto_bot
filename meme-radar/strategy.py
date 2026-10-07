"""Shared exit strategy logic for paper (radar.py) and live (live_trader.py).

Both must use the SAME exit ladder to ensure paper results are predictive
of live performance. Any change to exit rules goes here, not duplicated.
"""


def decide_exits(pos, price, now, P):
    """Pure exit decision — used by both paper and live.

    Mutates pos flags (tp1/tp2/ts_keep/ts_done/peak).
    Returns (actions, reason): actions = [(frac, why)], reason set when
    position should close fully (trailing/stop_loss/smart_exit/time_stop).
    """
    actions = []
    entry = pos.get("entry") or 0
    if entry <= 0 or not price or price <= 0:
        return actions, None
    ret = price / entry - 1
    reason = None
    if price > pos.get("peak", entry):
        pos["peak"] = price
    if pos.get("remaining", 1.0) <= 0:
        return actions, None

    def take(frac, why):
        frac = min(frac, pos.get("remaining", 1.0))
        if frac <= 0:
            return
        actions.append((round(frac, 4), why))
        pos["remaining"] = pos.get("remaining", 1.0) - frac

    # TP ladder
    if not pos.get("tp1") and ret >= P["tp1_pct"]:
        pos["tp1"] = True
        take(P["tp1_frac"], "TP1")
    if pos.get("tp1") and not pos.get("tp2") and ret >= P["tp2_pct"]:
        pos["tp2"] = True
        take(P["tp2_frac"], "TP2")
    # trailing: armed after TP2 or ts_keep
    rem = pos.get("remaining", 1.0)
    trail_arm = pos.get("tp2") or pos.get("ts_keep")
    if trail_arm and rem > 0 and price <= pos["peak"] * (1 - P["trailing_pct"]):
        take(rem, "TRAIL")
        reason = "trailing"
    # SL
    rem = pos.get("remaining", 1.0)
    if not reason and rem > 0 and ret <= -P["sl_pct"]:
        take(rem, "SL")
        reason = "stop_loss"
    # smart exit
    rem = pos.get("remaining", 1.0)
    if not reason and rem > 0 and pos.get("smart_exit"):
        take(rem, "SMART_EXIT")
        reason = "smart_exit"
    # copy exit: vi nguon (vi minh copy) da ban >= nguong luong dang giu
    rem = pos.get("remaining", 1.0)
    if not reason and rem > 0 and pos.get("copy_exit"):
        take(rem, "COPY_EXIT")
        reason = "copy_exit"
    # time stop
    rem = pos.get("remaining", 1.0)
    el_min = (now - pos["opened_at"]) / 60
    if (not reason and rem > 0 and not pos.get("ts_done")
            and el_min >= P["time_stop_min"]):
        pos["ts_done"] = True
        if ret >= P["ts_keep_pct"]:
            take(rem * P["ts_keep_frac"], "TIME_KEEP")
            pos["ts_keep"] = True
        else:
            take(rem, "TIME")
            reason = "time_stop"
    return actions, reason
