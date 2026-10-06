#!/usr/bin/env python3
"""Import 1 lan toan bo JSONL hien tai vao PostgreSQL.

Dung ON CONFLICT DO NOTHING nen chay lai an toan (idempotent).

Usage:
    DATABASE_URL=postgres://... python3 migrate_jsonl.py \
        --trading-bot ~/workspace/trading-bot \
        --meme-radar  ~/workspace/meme-radar
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg  # noqa: E402

FEE_RATE = 0.0005  # 0.05% moi chieu


def load_jsonl(path):
    out = []
    if not os.path.exists(path):
        print(f"  [bo qua] khong thay {path}")
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def migrate_okx(d):
    rows = load_jsonl(os.path.join(d, "trades.jsonl"))
    vals = []
    for t in rows:
        notional = float(t.get("notional", 0) or 0)
        vals.append((
            int(t["id"]), t.get("inst"), t.get("side"), t.get("tag"),
            t.get("entry"), t.get("exit"), notional,
            t.get("pnl"), 2 * FEE_RATE * notional,
            t.get("reason"), t.get("closed_at"),
        ))
    sql = """INSERT INTO okx_trades
        (id, inst, side, tag, entry, exit, notional, pnl, fee, reason, closed_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s))
        ON CONFLICT (id) DO NOTHING"""
    pg.executemany(sql, vals)
    print(f"  okx_trades: {len(vals)} dong doc, upsert xong")


def migrate_radar(d):
    total = 0
    for fname, plan in (("paper_trades.jsonl", "scalp"),
                        ("paper_trades_holder.jsonl", "holder")):
        rows = load_jsonl(os.path.join(d, fname))
        vals = []
        for r in rows:
            size = float(r.get("size_usd", 0) or 0)
            fret = float(r.get("final_ret", 0) or 0)
            vals.append((
                r.get("token"), r.get("symbol"), r.get("wallet"),
                r.get("opened_at"), r.get("closed_at"),
                r.get("entry"), r.get("exit"), size, fret,
                size * fret, r.get("reason"), plan,
                json.dumps(r.get("legs", [])), json.dumps(r.get("snaps", {})),
            ))
        # dedupe theo key (token, opened_at, wallet, closed_at, final_ret, plan)
        sql = """INSERT INTO radar_trades
            (token, symbol, wallet, opened_at, closed_at, entry, exit,
             size_usd, final_ret, pnl_usd, reason, plan, legs, snaps)
            VALUES (%s,%s,%s, to_timestamp(%s), to_timestamp(%s),
                    %s,%s,%s,%s,%s,%s,%s, %s::jsonb, %s::jsonb)
            ON CONFLICT (token, opened_at, wallet, closed_at, final_ret, plan)
            DO NOTHING"""
        pg.executemany(sql, vals)
        total += len(vals)
        print(f"  radar_trades[{plan}]: {len(vals)} dong doc, upsert xong")
    return total


def migrate_wallets(d):
    p = os.path.join(d, "wallets.json")
    if not os.path.exists(p):
        print("  [bo qua] khong thay wallets.json")
        return
    ws = json.load(open(p))
    vals = [(w.get("address"), w.get("label"), w.get("src")) for w in ws
            if w.get("address")]
    sql = """INSERT INTO wallets (address, label, src) VALUES (%s,%s,%s)
             ON CONFLICT (address) DO UPDATE
             SET label = EXCLUDED.label, src = EXCLUDED.src"""
    pg.executemany(sql, vals)
    print(f"  wallets: {len(vals)} vi upsert xong")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trading-bot", required=True)
    ap.add_argument("--meme-radar", required=True)
    a = ap.parse_args()
    print("Migrate OKX...")
    migrate_okx(a.trading_bot)
    print("Migrate radar...")
    migrate_radar(a.meme_radar)
    print("Migrate wallets...")
    migrate_wallets(a.meme_radar)
    print("XONG")


if __name__ == "__main__":
    main()
