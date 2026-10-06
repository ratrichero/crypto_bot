"""Strategy signals: regime detection, scalp entries."""
from indicators import ema, rsi, adx


def detect_regime(candles15, threshold):
    """Returns ('trending'|'ranging', adx_value)."""
    v = adx(candles15)
    if v is None:
        return "ranging", None
    return ("trending" if v >= threshold else "ranging"), round(v, 1)


def scalp_signal(candles5, candles15, cfg):
    """Returns 'long' | 'short' | None with info dict.

    Rules:
    - Trend filter on 15m: close vs EMA(20)
    - Entry on 5m: breakout of last N candle high/low + RSI guard
    - Uses last CLOSED candles only (drops the forming candle).
    """
    s = cfg["scalp"]
    if len(candles5) < 40 or len(candles15) < 30:
        return None, {"reason": "warmup"}
    closed5 = candles5[:-1]
    closes15 = [c["c"] for c in candles15]
    e20 = ema(closes15, s["ema_trend"])
    if e20 is None:
        return None, {"reason": "warmup"}
    trend = "long" if closes15[-1] > e20 else "short"

    sig = closed5[-1]
    look = closed5[-(s["breakout_lookback"] + 1):-1]
    closes5 = [c["c"] for c in closed5]
    r = rsi(closes5, s["rsi_period"])

    if trend == "long":
        if sig["c"] > max(c["h"] for c in look) and (r is None or r < s["rsi_long_max"]):
            return "long", {"rsi": round(r, 1) if r else None,
                             "ema20_15m": round(e20, 1)}
    else:
        if sig["c"] < min(c["l"] for c in look) and (r is None or r > s["rsi_short_min"]):
            return "short", {"rsi": round(r, 1) if r else None,
                              "ema20_15m": round(e20, 1)}
    return None, {"reason": "no_signal", "trend": trend}
