#!/usr/bin/env python3
"""Daily paper-trade report, multi-instrument (Vietnamese)."""
import json
import os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    sp, tp = os.path.join(BASE, "state.json"), os.path.join(BASE, "trades.jsonl")
    if not os.path.exists(sp):
        print("Bot chua co du lieu (chua chay lan nao).")
        return
    st = json.load(open(sp))
    trades = []
    if os.path.exists(tp):
        for line in open(tp):
            line = line.strip()
            if line:
                trades.append(json.loads(line))

    eq = st["equity"]
    total_pnl = eq - 1000.0
    day_pnl = eq - st.get("day_start_equity", eq)
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = (wins / n * 100) if n else 0
    # pnl trong file da tru phi ra lenh; tru them phi vao lenh de ra net tron vong
    cfg = json.load(open(os.path.join(BASE, "config.json")))
    fee_rate = cfg.get("fee_rate", 0.0005)
    net_pnls = [t["pnl"] - t["notional"] * fee_rate for t in trades]
    exp = sum(net_pnls) / n if n else 0
    gw = sum(p for p in net_pnls if p > 0)
    gl = sum(p for p in net_pnls if p < 0)
    pf = gw / abs(gl) if gl else float("inf")
    by_tag, by_inst = {}, {}
    for t in trades:
        d = by_tag.setdefault(t["tag"], {"n": 0, "pnl": 0.0})
        d["n"] += 1
        d["pnl"] += t["pnl"]
        e = by_inst.setdefault(t.get("inst", "?"), {"n": 0, "pnl": 0.0})
        e["n"] += 1
        e["pnl"] += t["pnl"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_coins = len(json.load(open(os.path.join(BASE, "universe.json"))))

    L = [
        f"BAO CAO PAPER TRADE (top {n_coins} MC, futures) — {ts}",
        f"Von ao: 1000.00 USDT | Equity: {eq:.2f} ({total_pnl:+.2f} / {total_pnl/10:+.2f}%)",
        f"P&L hom nay: {day_pnl:+.2f} USDT",
        f"Lenh da dong: {n} | Thang: {wins} | Winrate: {wr:.1f}% | Phi: {st['stats']['fees']:.2f}",
        f"Ky vong/lenh sau phi: {exp:+.2f} USDT | Profit factor: {pf:.2f}",
        ("TAM DUNG: " + st["halt_reason"]) if st.get("halted") else "Trang thai: dang chay",
        f"Vi the mo: {len(st['positions'])}",
    ]
    for p in st["positions"]:
        L.append(f"  - #{p['id']} {p['inst']} {p['side']} {p['tag']} entry={p['entry']}")
    if by_inst:
        L.append("P&L theo coin (top):")
        for inst, d in sorted(by_inst.items(), key=lambda x: x[1]["pnl"], reverse=True)[:8]:
            L.append(f"  - {inst}: {d['n']} lenh, {d['pnl']:+.2f}")
    op = os.path.join(BASE, "optimizer_state.json")
    if os.path.exists(op):
        o = json.load(open(op))
        ts_o = datetime.fromtimestamp(o["ts"], tz=timezone.utc).strftime("%d/%m %H:%M")
        L.append(f"Tu toi uu ({ts_o}, {o['trades_7d']} lenh/7d):")
        acts = o.get("actions", [])
        L.append("  - " + ("; ".join(acts[-3:]) if acts else "khong doi gi"))
    L.append("Paper trade mo phong; chua tinh funding rate; ket qua ao khong dam bao ket qua that.")
    print("\n".join(L))


if __name__ == "__main__":
    main()
