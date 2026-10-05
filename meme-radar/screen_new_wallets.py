#!/usr/bin/env python3
"""Sang loc ung vien vi MOI cho dan qua.
Usage: python3 screen_new_wallets.py candidates.json
- Buoc 1: quet nhanh tx/7d + ty le thanh cong -> loai bot spam
- Buoc 2: FIFO realized P&L (toi da 400 tx thanh cong, swap >=0.1 SOL)
- Tieu chi nhan: realized > +2 SOL, winrate >= 50%, >= 10 roundtrips
"""
import json
import os
import sys
import time
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from sources.helius import Helius, parse_buy, parse_sell

LOOKBACK_DAYS = 7
MAX_OK_TXS = 400
MIN_SOL = 0.1


def log(m):
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] {m}", flush=True)


def scan_activity(h, addr, cutoff):
    """Tra ve (total, ok) trong window. Gioi han 10 page."""
    before, total, ok, pages = None, 0, 0, 0
    while pages < 10:
        params = {"limit": 1000, "commitment": "confirmed"}
        if before:
            params["before"] = before
        res = h.rpc("getSignaturesForAddress", [addr, params])
        if not res:
            break
        pages += 1
        done = False
        for s in res:
            if s.get("blockTime", 0) < cutoff:
                done = True
                break
            total += 1
            if not s.get("err"):
                ok += 1
        if done or len(res) < 1000:
            break
        before = res[-1]["signature"]
        time.sleep(0.15)
    return total, ok


def collect_ok_sigs(h, addr, cutoff):
    out, before, pages = [], None, 0
    while len(out) < MAX_OK_TXS and pages < 15:
        params = {"limit": 1000, "commitment": "confirmed"}
        if before:
            params["before"] = before
        res = h.rpc("getSignaturesForAddress", [addr, params])
        if not res:
            break
        pages += 1
        done = False
        for s in res:
            if s.get("blockTime", 0) < cutoff:
                done = True
                break
            if not s.get("err"):
                out.append(s)
                if len(out) >= MAX_OK_TXS:
                    break
        if done or len(res) < 1000:
            break
        before = res[-1]["signature"]
        time.sleep(0.15)
    return out


def fifo(buys, sells):
    bb = defaultdict(list)
    for b in buys:
        bb[b["mint"]].append([b["tokens"], b["sol"] / b["tokens"] if b["tokens"] else 0])
    realized, nw, nl = 0.0, 0, 0
    for s in sorted(sells, key=lambda x: x["ts"]):
        lots = bb.get(s["mint"])
        if not lots:
            continue
        qty, spx = s["tokens"], s["sol"] / s["tokens"] if s["tokens"] else 0
        while qty > 1e-9 and lots:
            tk, px = lots[0]
            take = min(qty, tk)
            pnl = take * (spx - px)
            realized += pnl
            nw, nl = (nw + 1, nl) if pnl > 0 else (nw, nl + 1)
            lots[0][0] -= take
            qty -= take
            if lots[0][0] <= 1e-9:
                lots.pop(0)
    return realized, nw, nl


def main():
    cands = json.load(open(sys.argv[1]))
    key = open(os.path.join(BASE, ".helius_key")).read().strip()
    h = Helius(key, timeout=30)
    cutoff = int(time.time()) - LOOKBACK_DAYS * 86400
    results = []
    # Checkpoint: tiep tuc tu ket qua da luu, khong lam lai vi da xong
    done_addrs = set()
    try:
        prev = json.load(open(os.path.join(BASE, "screen_new.json")))
        if prev.get("done") == prev.get("total") == len(cands) and len(cands):
            log("screening da hoan tat, khong can chay lai")
            return
        for r in prev.get("results", []):
            if r.get("wallet"):
                results.append(r)
                done_addrs.add(r["wallet"])
        if done_addrs:
            log(f"tiep tuc tu checkpoint: {len(done_addrs)} vi da xong")
    except Exception:
        pass
    for c in cands:
        addr = c["address"]
        if addr in done_addrs:
            continue
        label = c.get("label", "?")
        try:
            total, ok = scan_activity(h, addr, cutoff)
        except Exception as e:
            log(f"{label[:14]:14} scan LOI: {str(e)[:60]}")
            continue
        rate = ok / total if total else 0
        log(f"{label[:14]:14} tx={total} ok={ok} ({rate:.0%})")
        if total > 2000 and rate < 0.10:
            results.append({"wallet": addr, "label": label, "tier": "BOT_SPAM",
                            "tx": total, "ok_rate": round(rate, 3)})
            continue
        if ok < 10:
            results.append({"wallet": addr, "label": label, "tier": "NO_SIGNAL",
                            "tx": total, "ok_rate": round(rate, 3)})
            continue
        try:
            sigs = collect_ok_sigs(h, addr, cutoff)
            buys, sells = [], []
            for s in sigs:
                try:
                    tx = h.rpc("getTransaction", [s["signature"],
                               {"encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 1,
                                "commitment": "confirmed"}])
                except Exception:
                    continue
                if not tx:
                    continue
                b = parse_buy(tx, addr, MIN_SOL)
                if b:
                    buys.append({"ts": s["blockTime"], "mint": b["mint"],
                                 "sol": b["sol_spent"], "tokens": b["tokens"]})
                    continue
                sl = parse_sell(tx, addr, MIN_SOL)
                if sl:
                    sells.append({"ts": s["blockTime"], "mint": sl["mint"],
                                  "sol": sl["sol_received"], "tokens": sl["tokens"]})
                time.sleep(0.04)
            realized, nw, nl = fifo(buys, sells)
            rt = nw + nl
            wr = nw / rt if rt else 0
            tier = ("ACCEPT" if (realized > 2 and wr >= 0.5 and rt >= 10)
                    else "WEAK")
            log(f"  -> {tier}: {realized:+.1f} SOL, RT={rt}, wr={wr:.0%}")
            results.append({"wallet": addr, "label": label, "tier": tier,
                            "tx": total, "ok_rate": round(rate, 3),
                            "realized_sol": round(realized, 2), "rt": rt,
                            "wr": round(wr, 2), "n_buys": len(buys)})
        except Exception as e:
            log(f"  -> SKIP loi: {str(e)[:60]}")
            results.append({"wallet": addr, "label": label, "tier": "ERROR",
                            "tx": total, "ok_rate": round(rate, 3)})
        json.dump({"ts": int(time.time()), "results": results,
                   "done": len(results), "total": len(cands)},
                  open(os.path.join(BASE, "screen_new.json"), "w"), indent=1)
        time.sleep(0.4)
    results.sort(key=lambda x: x.get("realized_sol", -999), reverse=True)
    print("\n===== KET QUA SANG LOC =====")
    for r in results:
        extra = (f" {r['realized_sol']:+.1f} SOL RT={r['rt']} wr={r['wr']:.0%}"
                 if "realized_sol" in r else "")
        print(f"{r['tier']:10} {r['label'][:16]:16}{extra}")
    json.dump({"ts": int(time.time()), "results": results,
               "done": len(results), "total": len(cands)},
              open(os.path.join(BASE, "screen_new.json"), "w"), indent=1)
    print("Da luu screen_new.json")


if __name__ == "__main__":
    main()
