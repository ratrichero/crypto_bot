"""Range grid 2 chieu (G4) - logic THUAN, dung chung cho bot live va
backtest_v2 (G3) de backtest phan anh dung dieu bot se lam.

Hinh hoc:
- Bien [low, high] lay tu scanner (high/low cua `range_hours` nen 1h da
  dong), mid = (low + high) / 2.
- Nua DUOI bien chi dat LONG: mid*(1 - k*step), k = 1..levels_each_side,
  phai nam tren low. Nua TREN chi dat SHORT: mid*(1 + k*step) < high.
  Khong bao gio mo long + short cung mot gia giua bien.
- TP moi lot = grid.tp_pct (0 -> 1 step). TP > step nen khi gia quet tu day
  len dinh, long chua chot thi short da mo: hai chieu cung ton tai (hedge).
- SL bien tren san: long = low*(1 - boundary_sl_buffer), short =
  high*(1 + boundary_sl_buffer); khong bao gio xa hon grid.sl_pct tu gia vao.
- Bien vo (gia ra ngoai bien qua break_buffer, hoac ADX 1h > trend_exit_adx)
  -> khong mo moi, huy lenh cho; tuy chon cat lot dang lo (derisk).
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULTS = {
    "levels_each_side": 5, "step_pct": 0.005, "step_min": 0.004,
    "step_max": 0.008, "step_mult": 0.8, "tp_pct": 0.01, "sl_pct": 0.03,
    "boundary_sl_buffer": 0.005, "break_buffer": 0.003,
    "trend_exit_adx": 25.0, "derisk_on_trend": False,
    "derisk_loss_pct": 0.01, "max_symbols": 0, "max_lots_per_symbol": 4,
    "max_positions": 7, "limit_min_gap_pct": 0.0005,
}


def gcfg(grid_cfg: Optional[dict]) -> dict:
    out = dict(DEFAULTS)
    out.update(grid_cfg or {})
    return out


def grid_step(atr15_pct: Optional[float], grid_cfg: dict) -> float:
    """Do gian tang = clamp(step_mult x ATR(15m)/gia, step_min, step_max) -
    cung cong thuc grid classic."""
    g = gcfg(grid_cfg)
    if atr15_pct:
        return round(min(g["step_max"], max(g["step_min"],
                                             g["step_mult"] * atr15_pct)), 6)
    return float(g["step_pct"])


def build_range(metrics: dict, grid_cfg: dict, ts: float,
                step: Optional[float] = None) -> Optional[dict]:
    """Dung bien + tang tu metrics scanner (range_low/range_high/atr15_pct).
    None neu bien khong hop le hoac khong co tang nao."""
    g = gcfg(grid_cfg)
    try:
        low = float(metrics["range_low"])
        high = float(metrics["range_high"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < low < high):
        return None
    step = float(step if step is not None
                 else grid_step(metrics.get("atr15_pct"), g))
    if step <= 0:
        return None
    mid = (low + high) / 2.0
    levels = []
    for k in range(1, int(g["levels_each_side"]) + 1):
        lp = mid * (1 - k * step)
        if lp > low:
            levels.append({"key": "rb%d" % k, "side": "long", "k": k,
                           "price": lp})
        sp = mid * (1 + k * step)
        if sp < high:
            levels.append({"key": "rs%d" % k, "side": "short", "k": k,
                           "price": sp})
    if not levels:
        return None
    buf = float(g["boundary_sl_buffer"])
    return {"low": low, "high": high, "mid": mid, "step": step,
            "ts": float(ts), "levels": levels,
            "sl_long": low * (1 - buf), "sl_short": high * (1 + buf)}


def lot_exits(rng: dict, side: str, entry: float,
              grid_cfg: dict) -> Tuple[float, float]:
    """(sl_price, tp_price) cho 1 lot. SL bien, khong xa hon grid.sl_pct."""
    g = gcfg(grid_cfg)
    tp_pct = float(g.get("tp_pct") or 0) or float(rng["step"])
    sl_cap = float(g.get("sl_pct") or 0.03)
    if side == "long":
        return (max(rng["sl_long"], entry * (1 - sl_cap)),
                entry * (1 + tp_pct))
    return (min(rng["sl_short"], entry * (1 + sl_cap)),
            entry * (1 - tp_pct))


def check_break(rng: Optional[dict], price: float,
                metrics: Optional[dict], grid_cfg: dict) -> Optional[str]:
    """Ly do bien vo (str) hoac None."""
    if not rng:
        return None
    g = gcfg(grid_cfg)
    bb = float(g["break_buffer"])
    if price < rng["low"] * (1 - bb):
        return "giá thủng đáy biên %.6g" % rng["low"]
    if price > rng["high"] * (1 + bb):
        return "giá vượt đỉnh biên %.6g" % rng["high"]
    adx1 = (metrics or {}).get("adx_1h")
    if adx1 is not None and float(adx1) > float(g["trend_exit_adx"]):
        return "ADX 1h %.1f > %s (chuyển trend)" % (float(adx1),
                                                   g["trend_exit_adx"])
    return None


def market_triggers(rng: dict, price: float,
                    occupied: Iterable[str]) -> List[dict]:
    """Tang da bi gia cat qua (vao lenh market), gan gia nhat truoc.
    Long: gia <= tang va con tren SL bien; short tuong tu."""
    occ = set(occupied)
    out = []
    for lv in rng["levels"]:
        if lv["key"] in occ:
            continue
        if ((lv["side"] == "long" and rng["sl_long"] < price <= lv["price"])
                or (lv["side"] == "short"
                    and lv["price"] <= price < rng["sl_short"])):
            out.append(dict(lv, dist=abs(lv["price"] / price - 1)))
    out.sort(key=lambda lv: lv["dist"])
    return out


def limit_candidates(rng: dict, price: float, occupied: Iterable[str],
                     min_gap_pct: float = 0.0005) -> List[dict]:
    """Tang dat duoc lenh LIMIT post-only: long DUOI gia, short TREN gia
    (cach it nhat min_gap de khong bi GTX tu choi). Gan gia nhat truoc."""
    occ = set(occupied)
    out = []
    for lv in rng["levels"]:
        if lv["key"] in occ:
            continue
        if ((lv["side"] == "long" and lv["price"] < price * (1 - min_gap_pct))
                or (lv["side"] == "short"
                    and lv["price"] > price * (1 + min_gap_pct))):
            out.append(dict(lv, dist=abs(lv["price"] / price - 1)))
    out.sort(key=lambda lv: lv["dist"])
    return out


def plan_slots(symbols: Sequence[dict], grid_cfg: dict,
               total_lots: int) -> List[Tuple[str, str]]:
    """Phan bo slot cho lenh vao moi (dung cho ca limit va market).

    symbols: [{symbol, score, lots, candidates:[level], pending:set(keys)}]
      - lots: so lot grid dang mo cua symbol
      - candidates: tang co the vao (da loc occupied), gan gia truoc
      - pending: key lenh cho dang co (duoc uu tien giu, tranh huy/dat lai)
    Gioi han: grid.max_positions (tong lot + lenh cho), max_lots_per_symbol,
    max_symbols (symbol co lot hoac lenh cho). Tra ve [(symbol, key)] duoc
    phep (gom pending duoc giu + tang moi).

    Lenh cho TINH vao slot: neu tat ca cung khop thi van khong vuot tran.
    Dat tran -> danh sach rong -> moi lenh cho con lai bi huy.
    """
    g = gcfg(grid_cfg)
    budget = max(0, int(g["max_positions"]) - int(total_lots))
    per_sym = int(g.get("max_lots_per_symbol") or 0) or 10 ** 6
    max_syms = int(g.get("max_symbols") or 0) or 10 ** 6
    busy = {s["symbol"] for s in symbols if s.get("lots")}
    order = sorted(symbols, key=lambda s: (not s.get("lots"),
                                           -float(s.get("score") or 0)))
    rows = []
    for s in order:
        for lv in s.get("candidates", []):
            rows.append((lv["key"] not in s.get("pending", set()),
                         abs(float(lv.get("dist", 0.0))), s["symbol"],
                         lv["key"], s))
    # uu tien: giu lenh cho cu, roi tang gan gia, roi symbol diem cao
    rank_sym = {s["symbol"]: i for i, s in enumerate(order)}
    rows.sort(key=lambda r: (r[0], r[1], rank_sym[r[2]]))
    used_sym: Dict[str, int] = {}
    chosen = []
    active_syms = set(busy)
    for _new, _dist, sym, key, s in rows:
        if budget <= 0:
            break
        if used_sym.get(sym, 0) + int(s.get("lots") or 0) >= per_sym:
            continue
        if sym not in active_syms and len(active_syms) >= max_syms:
            continue
        chosen.append((sym, key))
        used_sym[sym] = used_sym.get(sym, 0) + 1
        active_syms.add(sym)
        budget -= 1
    return chosen


def derisk_targets(lots: Sequence[dict], price: float,
                   grid_cfg: dict) -> List[dict]:
    """Lot dang lo > derisk_loss_pct (khi bien vo + derisk_on_trend)."""
    g = gcfg(grid_cfg)
    if not g.get("derisk_on_trend"):
        return []
    lim = float(g["derisk_loss_pct"])
    out = []
    for p in lots:
        e = float(p["entry"])
        loss = (e - price) / e if p["side"] == "long" else (price - e) / e
        if loss > lim:
            out.append(p)
    return out
