"""Giam sat vi Solana qua Helius free RPC: phat hien lenh BUY moi.

Moi vong: voi moi vi theo doi, lay signatures moi (getSignaturesForAddress),
doc transaction (getTransaction jsonParsed), tinh chenh lech so du:
SOL giam + token tang => BUY.
"""
import time

import re

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def redact_key(s):
    """Xoa api-key khoi moi chuoi log."""
    return re.sub(r"api-key=[^&\s'\"]+", "api-key=***", str(s))


class Helius:
    def __init__(self, api_key, timeout=20):
        self.url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        self.timeout = timeout

    def rpc(self, method, params):
        try:
            r = requests.post(
                self.url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=self.timeout,
            )
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(redact_key(e))
        j = r.json()
        if j.get("error"):
            raise RuntimeError(str(j["error"])[:200])
        return j.get("result")


def parse_buy(tx, wallet, min_sol):
    """Tra ve dict(mint, sol_spent, tokens) neu la BUY, else None."""
    try:
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return None
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        widx = None
        for i, k in enumerate(keys):
            pk = k.get("pubkey") if isinstance(k, dict) else k
            if pk == wallet:
                widx = i
                break
        if widx is None:
            return None
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if widx >= len(pre) or widx >= len(post):
            return None
        sol_spent = (pre[widx] - post[widx]) / 1e9
        if sol_spent < min_sol:
            return None
        pre_t = {(t.get("accountIndex"), t.get("mint")): t
                 for t in (meta.get("preTokenBalances") or [])}
        for t in (meta.get("postTokenBalances") or []):
            if t.get("owner") != wallet:
                continue
            mint = t.get("mint") or ""
            if mint in STABLES or mint == SOL_MINT or not mint:
                continue
            p = pre_t.get((t.get("accountIndex"), mint))
            pre_amt = float((p.get("uiTokenAmount") or {}).get("uiAmount") or 0) if p else 0
            post_amt = float((t.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if post_amt - pre_amt > 0:
                return {"mint": mint, "sol_spent": round(sol_spent, 4),
                        "tokens": post_amt - pre_amt}
        return None
    except Exception:
        return None


def parse_sell(tx, wallet, min_sol):
    """Tra ve dict(mint, sol_received, tokens) neu la SELL, else None."""
    try:
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return None
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        widx = None
        for i, k in enumerate(keys):
            pk = k.get("pubkey") if isinstance(k, dict) else k
            if pk == wallet:
                widx = i
                break
        if widx is None:
            return None
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if widx >= len(pre) or widx >= len(post):
            return None
        sol_received = (post[widx] - pre[widx]) / 1e9
        if sol_received < min_sol:
            return None
        pre_t = {(t.get("accountIndex"), t.get("mint")): t
                 for t in (meta.get("preTokenBalances") or [])}
        for t in (meta.get("postTokenBalances") or []):
            if t.get("owner") != wallet:
                continue
            mint = t.get("mint") or ""
            if mint in STABLES or mint == SOL_MINT or not mint:
                continue
            p = pre_t.get((t.get("accountIndex"), mint))
            pre_amt = float((p.get("uiTokenAmount") or {}).get("uiAmount") or 0) if p else 0
            post_amt = float((t.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if pre_amt - post_amt > 0:
                # sold_frac: phan luong token vi dang giu bi ban trong tx nay
                # (copy exit cong don qua nhieu lenh ban).
                return {"mint": mint, "sol_received": round(sol_received, 4),
                        "tokens": pre_amt - post_amt,
                        "sold_frac": round((pre_amt - post_amt) / pre_amt, 6)}
        # Ban SACH + dong token account trong cung tx: account bien mat khoi
        # postTokenBalances -> vong tren khong thay. Day lai la ca quan trong
        # nhat cho copy exit (vi xa het).
        post_keys = {(t.get("accountIndex"), t.get("mint"))
                     for t in (meta.get("postTokenBalances") or [])}
        for key, p in pre_t.items():
            if p.get("owner") != wallet or key in post_keys:
                continue
            mint = key[1] or ""
            if mint in STABLES or mint == SOL_MINT or not mint:
                continue
            pre_amt = float((p.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if pre_amt > 0:
                return {"mint": mint, "sol_received": round(sol_received, 4),
                        "tokens": pre_amt, "sold_frac": 1.0}
        return None
    except Exception:
        return None


def wallet_price_usd(buy, sol_usd):
    """Gia vi nguon khop (USD/token) = SOL vi chi / token nhan * gia SOL.
    SOL chi gom ca phi/tip/rent cua vi -> gia hoi CAO hon thuc (guard chong
    mua duoi vi vay hoi long hon, khong chat hon). None neu khong tinh duoc."""
    try:
        tokens = float(buy.get("tokens") or 0)
        sol = float(buy.get("sol_spent") or 0)
        px = sol * float(sol_usd or 0) / tokens if tokens > 0 else 0
    except (TypeError, ValueError):
        return None
    return px if px > 0 and px == px and px != float("inf") else None


def load_wallets(cfg, base):
    import json as _json
    import os as _os
    out = []
    wf = cfg.get("wallets_file", "wallets.json")
    p = wf if _os.path.isabs(wf) else _os.path.join(base, wf)
    if _os.path.exists(p):
        try:
            for w in _json.load(open(p)):
                out.append(w["address"] if isinstance(w, dict) else w)
        except Exception:
            pass
    for w in cfg.get("watch_wallets", []) or []:
        out.append(w["address"] if isinstance(w, dict) else w)
    # dedupe, giu thu tu
    seen, res = set(), []
    for a in out:
        if a and a not in seen:
            seen.add(a)
            res.append(a)
    return res


def poll_wallet_txs(cfg, st, log, ds_token_fn, sol_usd, base):
    """Quet moi vi, tra ve (buys_enriched, sells_raw). Luu last sig vao st."""
    h = Helius(cfg["helius_api_key"])
    wallets = load_wallets(cfg, base)
    if not wallets:
        return [], []
    last = st.setdefault("helius_last", {})
    sigs = []
    sells = []
    min_sol = cfg.get("min_sol_spent", 0.3)
    # Lenh ban nho hon van can cho copy exit (vi xa het sau khi coin sap chi
    # con vai chuc $). sell_cluster/holder van loc theo min_sol_spent.
    min_sell = cfg.get("min_sell_sol", min(0.05, min_sol))
    for addr in wallets:
        try:
            res = h.rpc("getSignaturesForAddress",
                        [addr, {"limit": 25, "commitment": "confirmed"}])
        except Exception as e:
            log(f"helius sig {addr[:6]}..: {e}")
            continue
        fresh = []
        for s in res or []:
            sig = s.get("signature")
            if not sig:
                continue
            if sig == last.get(addr):
                break
            fresh.append(s)
        fresh.reverse()
        for s in fresh:
            try:
                tx = h.rpc("getTransaction",
                           [s["signature"],
                            {"encoding": "jsonParsed",
                             "maxSupportedTransactionVersion": 1,
                             "commitment": "confirmed"}])
            except Exception:
                continue
            if not tx:
                continue
            b = parse_buy(tx, addr, min_sol)
            if b:
                price_usd, symbol, mcap = ds_token_fn(b["mint"])
                wpx = wallet_price_usd(b, sol_usd)
                sigs.append({
                    "wallet_price_usd": wpx,
                    "wallet_tokens": b.get("tokens"),
                    "sol_spent": b["sol_spent"],
                    "tid": s["signature"],
                    "wallet": addr,
                    "token": b["mint"],
                    "symbol": (symbol or "?").upper(),
                    "amount_usd": round(b["sol_spent"] * sol_usd, 1),
                    "price_usd": price_usd or 0,
                    "price_now": price_usd or 0,
                    "ts": s.get("blockTime") or int(time.time()),
                    "tx": s["signature"],
                    "mcap_usd": mcap or 0,
                    "src": "poll",
                })
                log(f"BUY phat hien: {symbol} ${b['sol_spent']*sol_usd:.0f} "
                    f"vi {addr[:6]}..")
                continue
            sl = parse_sell(tx, addr, min_sell)
            if sl:
                sells.append({
                    "tid": s["signature"], "wallet": addr, "token": sl["mint"],
                    "sol_amount": sl["sol_received"],
                    "sold_frac": sl.get("sold_frac"),
                    "amount_usd": round(sl["sol_received"] * sol_usd, 1),
                    "ts": s.get("blockTime") or int(time.time()),
                    "src": "poll", "side": "sell",
                })
                log(f"SELL phat hien: {sl['mint'][:10]}.. "
                    f"${sl['sol_received']*sol_usd:.0f} vi {addr[:6]}..")
        if res and res[0].get("signature"):
            last[addr] = res[0]["signature"]
        time.sleep(0.2)
    return sigs, sells
