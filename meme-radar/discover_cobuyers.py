#!/usr/bin/env python3
"""Tim vi mua cung (co-buyers) voi 5 vi gioi tren cac token thang dam.
- Voi moi token: xac dinh gio mua cua vi minh (opened_at som nhat)
- Quet tx trong [buy-60p, buy], trich nguoi mua >= 0.3 SOL (fee payer)
- Xuat candidates.json (dia chi duy nhat, so lan mua cung)
"""
import json
import os
import sys
import time
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from sources.helius import Helius, SOL_MINT, STABLES

MIN_SOL = 0.3
WINDOW_SEC = 3600
MAX_TX_PER_TOKEN = 400

KNOWN = set()
for f in ["wallets.json", "wallets.json.bak-20261005", "wallets.json.bak-20261005-7w"]:
    p = os.path.join(BASE, f)
    if os.path.exists(p):
        for w in json.load(open(p)):
            KNOWN.add(w["address"] if isinstance(w, dict) else w)


def log(m):
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] {m}", flush=True)


def buyer_of(tx, target_mint, min_sol):
    """Tra ve dia chi buyer neu fee-payer mua target_mint >= min_sol."""
    try:
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return None
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        if not keys:
            return None
        k0 = keys[0]
        buyer = k0.get("pubkey") if isinstance(k0, dict) else k0
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if not pre or not post:
            return None
        spent = (pre[0] - post[0]) / 1e9
        if spent < min_sol:
            return None
        for t in meta.get("postTokenBalances") or []:
            if t.get("mint") != target_mint:
                continue
            if t.get("owner") != buyer:
                continue
            amt = float((t.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if amt > 0:
                return buyer
        return None
    except Exception:
        return None


def main():
    toks = json.load(open(os.path.join(BASE, "discover_tokens.json")))
    # gio mua cua vi minh theo tung token (tu paper dedupe)
    recs = [json.loads(l) for l in open(os.path.join(BASE, "paper_trades.jsonl"))
            if l.strip()]
    buy_time = {}
    for r in recs:
        t = r["token"]
        if t not in buy_time or r["opened_at"] < buy_time[t]:
            buy_time[t] = r["opened_at"]
    key = open(os.path.join(BASE, ".helius_key")).read().strip()
    h = Helius(key, timeout=30)
    all_buyers = Counter()
    for entry in toks:
        tok = entry["token"]
        bt = buy_time.get(tok)
        if not bt:
            continue
        lo = bt - WINDOW_SEC
        log(f"token {tok[:10]}.. buy_time={time.strftime('%H:%M', time.gmtime(bt))}")
        sigs, before, pages = [], None, 0
        while pages < 30:
            params = {"limit": 1000, "commitment": "confirmed"}
            if before:
                params["before"] = before
            res = h.rpc("getSignaturesForAddress", [tok, params])
            if not res:
                break
            pages += 1
            stop = False
            for s in res:
                bts = s.get("blockTime", 0)
                if bts < lo:
                    stop = True
                    break
                if bts <= bt and not s.get("err"):
                    sigs.append(s)
            if stop or len(res) < 1000:
                break
            before = res[-1]["signature"]
            time.sleep(0.15)
        log(f"  {len(sigs)} tx trong window, parse {min(len(sigs), MAX_TX_PER_TOKEN)}...")
        n_buy = 0
        for s in sigs[-MAX_TX_PER_TOKEN:]:
            try:
                tx = h.rpc("getTransaction", [s["signature"],
                           {"encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 1,
                            "commitment": "confirmed"}])
            except Exception:
                continue
            if not tx:
                continue
            b = buyer_of(tx, tok, MIN_SOL)
            if b and b not in KNOWN:
                all_buyers[b] += 1
                n_buy += 1
            time.sleep(0.04)
        log(f"  -> {n_buy} luot mua, tong {len(all_buyers)} vi la")
        time.sleep(0.5)
    cands = [{"address": a, "label": "", "cobuy_count": n,
              "note": "co-buyer"}
             for a, n in all_buyers.most_common(60)]
    json.dump(cands, open(os.path.join(BASE, "candidates.json"), "w"), indent=1)
    print(f"\nXong: {len(cands)} ung vien -> candidates.json")
    for c in cands[:15]:
        print(f"  {c['address'][:12]}.. mua cung {c['cobuy_count']} token")


if __name__ == "__main__":
    main()
