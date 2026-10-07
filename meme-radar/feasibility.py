"""Kiem tra kha thi cho paper trading voi volume/thanh khoan that.

De xuat cua Musev (paper radar dang gia dinh khop 100% tai gia signal):
  - Loc thanh khoan:  chi mo paper khi liquidity_usd >= size * min_liquidity_mult
  - Slippage dong:    slippage_pct = min(size / liquidity_usd * slippage_coef,
                                         max_slippage_pct)   (don vi %)
                      vd size $100, liquidity $2,000 -> 100/2000*50 = 2.5%
                      tru vao gia vao (mua cao hon) va gia ra (ban thap hon)
  - Kha nang thoat:   volume 5 phut < gia tri leg * min_exit_volume_mult ->
                      exit_constrained, hoan 1 vong; qua max_exit_waits vong
                      -> thoat voi slippage toi da, ghi exit_failed_liquidity

Ham thuan (khong I/O) -> dung chung cho radar.py (paper), report.py (replay /
do nhay) va test. Config doc tu cap cao nhat cua config.json radar (dung ten
key Musev de xuat).
"""

DEFAULTS = {
    "feasibility_enabled": True,
    "min_liquidity_mult": 20.0,
    "slippage_coef": 50.0,
    "max_slippage_pct": 15.0,
    "min_exit_volume_mult": 5.0,
    "max_exit_waits": 3,
}


def params(cfg=None):
    """Tham so kha thi: DEFAULTS ghi de boi key cung ten trong cfg."""
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if cfg and cfg.get(k) is not None:
            out[k] = cfg[k]
    return out


def _pos(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and f == f and f != float("inf") else None


def liquidity_ok(size_usd, liquidity_usd, f):
    """(ok, ly_do). Khong co so lieu thanh khoan -> khong mo (fail closed:
    paper khong duoc gia dinh khop khi khong biet pool)."""
    liq = _pos(liquidity_usd)
    if liq is None:
        return False, "skipped_no_liquidity_data"
    need = float(size_usd) * float(f["min_liquidity_mult"])
    if liq < need:
        return False, "skipped_low_liquidity"
    return True, None


def slippage_pct(size_usd, liquidity_usd, f):
    """Slippage uoc tinh (PHAN TRAM, 2.5 = 2.5%). Thieu thanh khoan -> toi
    da (bi quan)."""
    cap = float(f["max_slippage_pct"])
    liq = _pos(liquidity_usd)
    size = max(0.0, float(size_usd or 0))
    if liq is None:
        return cap
    return min(size / liq * float(f["slippage_coef"]), cap)


def exit_constrained(leg_usd, volume_m5_usd, f):
    """True neu volume 5 phut gan nhat qua nho so voi leg ban. Khong co so
    lieu volume -> False (khong biet thi khong tu che them do tre)."""
    vol = volume_m5_usd
    try:
        vol = float(vol)
    except (TypeError, ValueError):
        return False
    if vol != vol:
        return False
    return vol < max(0.0, float(leg_usd)) * float(f["min_exit_volume_mult"])


def apply_buy(price, slip_pct):
    """Gia khop mua sau slippage (cao hon)."""
    return float(price) * (1.0 + float(slip_pct) / 100.0)


def apply_sell(price, slip_pct):
    """Gia khop ban sau slippage (thap hon)."""
    return float(price) * (1.0 - float(slip_pct) / 100.0)


def replay_trade(trade, liquidity_usd, f):
    """Tinh lai 1 trade paper da dong (legs: frac, ret) gia dinh thanh khoan
    `liquidity_usd` co dinh. Tra ve dict:
      skipped  : True neu bi loc thanh khoan (khong mo)
      pnl_raw  : P&L goc (khop tai gia signal)
      pnl_adj  : P&L sau slippage vao + ra
      slip_in  : slippage vao (%)
    Dung cho do nhay du lieu cu (truoc day khong ghi thanh khoan)."""
    size = float(trade.get("size_usd") or 0)
    legs = trade.get("legs") or []
    pnl_raw = 0.0
    for leg in legs:
        pnl_raw += float(leg.get("frac") or 0) * size * float(leg.get("ret")
                                                             or 0)
    ok, _ = liquidity_ok(size, liquidity_usd, f)
    if not ok:
        return {"skipped": True, "pnl_raw": pnl_raw, "pnl_adj": 0.0,
                "slip_in": None}
    s_in = slippage_pct(size, liquidity_usd, f)
    pnl_adj = 0.0
    for leg in legs:
        frac = float(leg.get("frac") or 0)
        ret = float(leg.get("ret") or 0)
        leg_usd = frac * size * (1.0 + ret)
        s_out = slippage_pct(leg_usd, liquidity_usd, f)
        # Mua o gia entry*(1+s_in), ban o px*(1-s_out): moi $ von mua duoc
        # 1/(1+s_in) don vi so voi khop tai gia signal.
        factor = (1.0 + ret) * (1.0 - s_out / 100.0) / (1.0 + s_in / 100.0)
        pnl_adj += frac * size * (factor - 1.0)
    return {"skipped": False, "pnl_raw": pnl_raw, "pnl_adj": pnl_adj,
            "slip_in": s_in}
