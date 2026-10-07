"""Chon pair DexScreener dung cho mot mint (dung chung radar.py + live_trader.py).

API `GET /tokens/v1/solana/{mint}` tra ve MOI pair co chua mint, khong dam
bao thu tu. rows[0] co the la:
  - pair ma mint la QUOTE (vd XYZ/SOL khi hoi gia SOL) -> priceUsd la gia XYZ;
  - pool rac thanh khoan thap -> gia lech;
  - pair chain khac (phong ho).
Quy tac: chi xet chainId solana (neu co truong nay); uu tien tuyet doi pair co
mint la BASE, thanh khoan USD cao nhat; khong co thi suy gia tu pair mint la
QUOTE (priceUsd / priceNative). Khong suy duoc -> None (fail closed).
"""


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and f == f and f != float("inf") else None


def _liq(row):
    try:
        return float((row.get("liquidity") or {}).get("usd") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _vol(v):
    """Volume USD (>= 0). Khong co / sai -> None (khong biet)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 and f == f and f != float("inf") else None


def pick_pair(rows, mint):
    """Tra ve dict {price, symbol, mcap, liquidity_usd, volume_m5, volume_h24,
    as_base, pair} hoac
    None. mcap chi co khi mint la base (pair quote khong cho mcap cua mint ->
    0 = khong biet)."""
    if not isinstance(rows, list) or not mint:
        return None
    best, best_rank = None, -1.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        chain = row.get("chainId")
        if chain is not None and chain != "solana":
            continue
        base = row.get("baseToken") or {}
        quote = row.get("quoteToken") or {}
        if not isinstance(base, dict) or not isinstance(quote, dict):
            continue
        liq = _liq(row)
        if base.get("address") == mint:
            px = _num(row.get("priceUsd"))
            if px is None:
                continue
            cand = {
                "price": px, "symbol": base.get("symbol") or "?",
                "mcap": _num(row.get("marketCap")) or _num(row.get("fdv")) or 0.0,
                "as_base": True,
            }
            rank = liq + 1e18  # uu tien tuyet doi pair mint la base
        elif quote.get("address") == mint:
            pu, pn = _num(row.get("priceUsd")), _num(row.get("priceNative"))
            if not (pu and pn):
                continue
            cand = {"price": pu / pn, "symbol": quote.get("symbol") or "?",
                    "mcap": 0.0, "as_base": False}
            rank = liq
        else:
            continue
        if rank > best_rank:
            vol = row.get("volume") if isinstance(row.get("volume"),
                                                  dict) else {}
            cand.update(liquidity_usd=liq, pair=row.get("pairAddress"),
                        volume_m5=_vol(vol.get("m5")),
                        volume_h24=_vol(vol.get("h24")))
            best, best_rank = cand, rank
    return best
