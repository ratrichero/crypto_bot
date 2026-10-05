#!/usr/bin/env python3
"""Bao cao radar meme: paper copy-trade + wallet leaderboard (tieng Viet)."""
import json
import os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))


def load_jsonl(p):
    out = []
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    # dedupe: bo ban ghi trung do 2 process chong nhau (truoc 05/10/2026)
    seen, uniq = {}, []
    for r in out:
        k = (r.get("token"), r.get("opened_at"), r.get("wallet"),
             r.get("closed_at"), round(r.get("final_ret", 0) or 0, 6))
        if k not in seen:
            seen[k] = True
            uniq.append(r)
    return uniq


def main():
    trades = load_jsonl(os.path.join(BASE, "paper_trades.jsonl"))
    st = {}
    sp = os.path.join(BASE, "radar_state.json")
    if os.path.exists(sp):
        st = json.load(open(sp))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    L = [f"RADAR MEME (Solana smart money, paper) — {ts}"]
    L.append(f"Vi the paper dang mo: {len(st.get('paper', []))}")
    if not trades:
        L.append("Chua co lenh paper nao dong (can ~4h moi dong 1 lenh).")
        print("\n".join(L))
        return

    def avg_rets(ts_):
        out = {}
        for h in ("5", "15", "60", "240"):
            vs = [t["snaps"][h] for t in ts_ if h in t.get("snaps", {})]
            out[h] = sum(vs) / len(vs) if vs else None
        return out

    n = len(trades)
    wins = sum(1 for t in trades if (t.get("final_ret") or 0) > 0)
    ar = avg_rets(trades)
    L.append(f"Lenh da dong: {n} | Winrate: {wins/n:.0%}")
    L.append("Loi nhuan TB theo chan troi: " + ", ".join(
        f"+{h}p: {v:+.1%}" if v is not None else f"+{h}p: ?"
        for h, v in ar.items()))
    by_reason = {}
    for t in trades:
        r = t.get("reason", "?")
        d = by_reason.setdefault(r, {"n": 0, "pnl": 0.0})
        d["n"] += 1
        if t.get("final_ret") is not None:
            d["pnl"] += t["final_ret"] * t["size_usd"]
    if by_reason:
        L.append("Thoat lenh theo ly do: " + "; ".join(
            f"{r}: {d['n']} lenh ({d['pnl']:+.1f}U)"
            for r, d in sorted(by_reason.items(),
                               key=lambda x: -x[1]["pnl"])))

    by_w = {}
    for t in trades:
        d = by_w.setdefault(t["wallet"], {"n": 0, "pnl": 0.0})
        d["n"] += 1
        if t.get("final_ret") is not None:
            d["pnl"] += t["final_ret"] * t["size_usd"]
    L.append("Top vi (tinh theo P&L paper cac call):")
    for w, d in sorted(by_w.items(), key=lambda x: x[1]["pnl"],
                       reverse=True)[:5]:
        L.append(f"  - {w[:10]}...: {d['n']} lenh, {d['pnl']:+.1f}U")
    L.append("Paper thuan tuy mo phong copy theo vi smart money; "
             "ket qua ao khong dam bao ket qua that.")
    # plan holder (vi holder: SL rong, om dai, scale-in, chot theo vi)
    ht = load_jsonl(os.path.join(BASE, "paper_trades_holder.jsonl"))
    if ht:
        hn = len(ht)
        hw = sum(1 for t in ht if (t.get("final_ret") or 0) > 0)
        hpnl = sum((t.get("final_ret") or 0) * t.get("size_usd", 0) for t in ht)
        hr = {}
        for t in ht:
            d = hr.setdefault(t.get("reason", "?"), {"n": 0, "pnl": 0.0})
            d["n"] += 1
            d["pnl"] += (t.get("final_ret") or 0) * t.get("size_usd", 0)
        L.append(f"HOLDER plan: {hn} lenh dong | winrate {hw/hn:.0%} | "
                 f"net {hpnl:+.1f}U | dang mo: {len(st.get('paper_holder', []))}")
        L.append("  Thoat: " + "; ".join(
            f"{r}: {d['n']} ({d['pnl']:+.1f}U)"
            for r, d in sorted(hr.items(), key=lambda x: -x[1]["pnl"])))
    print("\n".join(L))


if __name__ == "__main__":
    main()
