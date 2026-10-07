"""Tach so lieu cua 1 vi tu ket qua RPC getTransaction (thuan, khong mang).

Dung chung: live_trader (ghi P&L/phi tu tx that thay vi getBalance co the
lag) va reconcile_wallet (doi chieu dashboard voi chain).
"""
SOL_MINT = "So11111111111111111111111111111111111111112"


def _key(k):
    return k.get("pubkey") if isinstance(k, dict) else k


def parse_tx(tx, wallet, mint=None):
    """Tach tx -> so lieu cua vi. Thuan, test duoc.

    sol_delta: lamports vi thay doi (DA gom phi neu vi tra phi).
    fee: meta.fee (vi la fee payer -> phi do vi tra).
    token_delta: base units cua `mint` (moi account token chu so huu = vi).
    rent_open: lamports nap vao token account MOI cua vi (mint do).
    rent_close: lamports thu ve tu token account cua vi bi dong.
    """
    if not tx:
        return None
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [_key(k) for k in msg.get("accountKeys") or []]
    # encoding json (khong parsed) cho v0: loadedAddresses tach rieng
    la = meta.get("loadedAddresses") or {}
    if len(keys) < len(meta.get("preBalances") or []):
        keys = keys + list(la.get("writable") or []) + list(
            la.get("readonly") or [])
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    out = {"ok": meta.get("err") is None, "fee": int(meta.get("fee") or 0),
           "block_time": tx.get("blockTime"), "sol_delta": 0,
           "token_delta": 0, "rent_open": 0, "rent_close": 0,
           "fee_payer": keys[0] == wallet if keys else False,
           "mints": {}}
    if wallet in keys:
        i = keys.index(wallet)
        if i < len(pre) and i < len(post):
            out["sol_delta"] = int(post[i]) - int(pre[i])

    def _tb(lst):
        d = {}
        for b in lst or []:
            if b.get("owner") != wallet:
                continue
            amt = int(((b.get("uiTokenAmount") or {}).get("amount")) or 0)
            d[(b.get("accountIndex"), b.get("mint"))] = amt
        return d
    tpre, tpost = _tb(meta.get("preTokenBalances")), _tb(
        meta.get("postTokenBalances"))
    for k in set(tpre) | set(tpost):
        idx, m = k
        delta = tpost.get(k, 0) - tpre.get(k, 0)
        if m == SOL_MINT:
            continue  # wSOL tam cua Jupiter: da phan anh trong sol_delta
        out["mints"][m] = out["mints"].get(m, 0) + delta
        if mint and m != mint:
            continue
        if idx is None or idx >= len(pre) or idx >= len(post):
            continue
        if k not in tpre and int(pre[idx]) == 0 and int(post[idx]) > 0:
            out["rent_open"] += int(post[idx])
        if k not in tpost and int(pre[idx]) > 0 and int(post[idx]) == 0:
            out["rent_close"] += int(pre[idx])
    if mint:
        out["token_delta"] = out["mints"].get(mint, 0)
    return out
