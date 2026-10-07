#!/usr/bin/env python3
"""Daemon dong bo JSONL -> PostgreSQL.

Theo doi byte-offset moi file trong 1 state file rieng, moi vong (60s)
doc cac dong moi append tu lan truoc va upsert vao PG. Chi DOC file goc,
khong bao gio sua.

Usage:
    DATABASE_URL=postgres://... python3 sync_jsonl.py \
        --trading-bot ~/workspace/trading-bot \
        --meme-radar  ~/workspace/meme-radar [--interval 60] [--once]
        [--binance-bot ~/path/to/binance-bot]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pg  # noqa: E402

FEE_RATE = 0.0005

OKX_SQL = """INSERT INTO okx_trades
    (id, inst, side, tag, entry, exit, notional, pnl, fee, reason, closed_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s))
    ON CONFLICT (id) DO NOTHING"""

RADAR_SQL = """INSERT INTO radar_trades
    (token, symbol, wallet, opened_at, closed_at, entry, exit,
     size_usd, final_ret, pnl_usd, reason, plan, legs, snaps)
    VALUES (%s,%s,%s, to_timestamp(%s), to_timestamp(%s),
            %s,%s,%s,%s,%s,%s,%s, %s::jsonb, %s::jsonb)
    ON CONFLICT (token, opened_at, wallet, closed_at, final_ret, plan)
    DO NOTHING"""

WALLET_SQL = """INSERT INTO wallets (address, label, src) VALUES (%s,%s,%s)
    ON CONFLICT (address) DO UPDATE SET label = EXCLUDED.label, src = EXCLUDED.src"""

BINANCE_SQL = """INSERT INTO binance_trades
    (id, symbol, side, tag, entry, exit, notional, pnl, reason, closed_at,
     live, dry, close_ord)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s), %s,%s,%s)
    ON CONFLICT (id) DO NOTHING"""

# Cot phi/nguon gia (binance-bot >= c4a1a17). DB cu duoc ALTER idempotent;
# ALTER loi -> dung BINANCE_SQL cu (khong mat trade).
BINANCE_EXT_COLS = ("pnl_gross", "fee_entry", "fee_exit", "fee_estimated",
                    "estimated", "exit_source")
BINANCE_MIGRATE = [
    "ALTER TABLE binance_trades ADD COLUMN IF NOT EXISTS %s %s" % (c, t)
    for c, t in (("pnl_gross", "DOUBLE PRECISION"),
                 ("fee_entry", "DOUBLE PRECISION"),
                 ("fee_exit", "DOUBLE PRECISION"),
                 ("fee_estimated", "BOOLEAN"), ("estimated", "BOOLEAN"),
                 ("exit_source", "TEXT"))]
BINANCE_EXT_SQL = """INSERT INTO binance_trades
    (id, symbol, side, tag, entry, exit, notional, pnl, reason, closed_at,
     live, dry, close_ord, pnl_gross, fee_entry, fee_exit, fee_estimated,
     estimated, exit_source)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s), %s,%s,%s,
            %s,%s,%s,%s,%s,%s)
    ON CONFLICT (id) DO UPDATE SET """ + ", ".join(
    "%s = COALESCE(binance_trades.%s, EXCLUDED.%s)" % (c, c, c)
    for c in BINANCE_EXT_COLS)
_binance_ext = {"ok": False, "retry_at": 0.0}


def binance_ext_ready():
    """ALTER cac cot moi (idempotent); True neu dung duoc insert mo rong.
    Loi (quyen/DB tam mat) -> insert cot cu, thu lai sau 10 phut."""
    if not _binance_ext["ok"] and time.time() >= _binance_ext["retry_at"]:
        try:
            for sql in BINANCE_MIGRATE:
                pg.execute(sql)
            _binance_ext["ok"] = True
        except Exception as e:
            print(f"[{ts()}] WARNING migrate binance_trades: {e} "
                  "-> insert cot cu, thu lai sau 10 phut", flush=True)
            _binance_ext["retry_at"] = time.time() + 600
    return _binance_ext["ok"]


def binance_row(t, extended):
    base = (int(t["id"]), t.get("symbol"), t.get("side"), t.get("tag"),
            t.get("entry"), t.get("exit"),
            float(t.get("notional", 0) or 0), t.get("pnl"),
            t.get("reason"), t.get("closed_at"),
            bool(t.get("live")), bool(t.get("dry")), t.get("close_ord"))
    if not extended:
        return base
    est = t.get("estimated")
    return base + (t.get("pnl_gross"), t.get("fee_entry"), t.get("fee_exit"),
                   t.get("fee_estimated"),
                   None if est is None else bool(est), t.get("exit_source"))


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_state(p):
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {}


def save_state(p, st):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def tail_new(path, offset):
    """Doc cac dong moi tu offset (byte). Tra ve (dong_moi, offset_moi)."""
    if not os.path.exists(path):
        return [], offset
    size = os.path.getsize(path)
    if size < offset:  # file bi rotate/ghi lai -> doc tu dau
        offset = 0
    lines = []
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            try:
                line = raw.decode("utf-8").strip()
            except Exception:
                continue
            if line:
                try:
                    lines.append(json.loads(line))
                except Exception:
                    pass
        offset = f.tell()
    return lines, offset


def sync_okx(path, st):
    rows, off = tail_new(path, st.get("okx", 0))
    vals = []
    for t in rows:
        if "id" not in t:
            continue
        notional = float(t.get("notional", 0) or 0)
        vals.append((int(t["id"]), t.get("inst"), t.get("side"), t.get("tag"),
                     t.get("entry"), t.get("exit"), notional, t.get("pnl"),
                     2 * FEE_RATE * notional, t.get("reason"),
                     t.get("closed_at")))
    if vals:
        pg.executemany(OKX_SQL, vals)
    st["okx"] = off
    return len(vals)


def sync_radar(path, plan, st_key, st):
    rows, off = tail_new(path, st.get(st_key, 0))
    vals = []
    for r in rows:
        if not r.get("token"):
            continue
        size = float(r.get("size_usd", 0) or 0)
        fret = float(r.get("final_ret", 0) or 0)
        vals.append((r.get("token"), r.get("symbol"), r.get("wallet"),
                     r.get("opened_at"), r.get("closed_at"),
                     r.get("entry"), r.get("exit"), size, fret,
                     size * fret, r.get("reason"), plan,
                     json.dumps(r.get("legs", [])),
                     json.dumps(r.get("snaps", {}))))
    if vals:
        pg.executemany(RADAR_SQL, vals)
    st[st_key] = off
    return len(vals)


def sync_wallets(path, st):
    if not os.path.exists(path):
        return 0
    mtime = int(os.path.getmtime(path))
    if st.get("wallets_mtime") == mtime:
        return 0
    ws = json.load(open(path))
    vals = [(w.get("address"), w.get("label"), w.get("src")) for w in ws
            if w.get("address")]
    if vals:
        pg.executemany(WALLET_SQL, vals)
    st["wallets_mtime"] = mtime
    return len(vals)


def sync_binance(path, st):
    rows, off = tail_new(path, st.get("binance", 0))
    ext = binance_ext_ready() if rows else False
    vals = [binance_row(t, ext) for t in rows if "id" in t]
    if vals:
        pg.executemany(BINANCE_EXT_SQL if ext else BINANCE_SQL, vals)
    st["binance"] = off
    return len(vals)


def run_once(a, st):
    n1 = sync_okx(os.path.join(a.trading_bot, "trades.jsonl"), st)
    n2 = sync_radar(os.path.join(a.meme_radar, "paper_trades.jsonl"),
                    "scalp", "radar_scalp", st)
    n3 = sync_radar(os.path.join(a.meme_radar, "paper_trades_holder.jsonl"),
                    "holder", "radar_holder", st)
    n4 = sync_wallets(os.path.join(a.meme_radar, "wallets.json"), st)
    n5 = 0
    if a.binance_bot:
        n5 = sync_binance(os.path.join(a.binance_bot, "trades.jsonl"), st)
    total = n1 + n2 + n3 + n5
    if total or n4:
        print(f"[{ts()}] sync: okx +{n1}, radar_scalp +{n2}, "
              f"radar_holder +{n3}, binance +{n5}, wallets {n4} vi", flush=True)
    else:
        print(f"[{ts()}] sync: khong co dong moi", flush=True)
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trading-bot", required=True)
    ap.add_argument("--meme-radar", required=True)
    ap.add_argument("--binance-bot", default=None,
                    help="thu muc binance-bot (optional); sync trades.jsonl -> binance_trades")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true",
                    help="chay 1 vong roi thoat (dung de test)")
    ap.add_argument("--state",
                    default=os.path.expanduser("~/.cryptobots_sync_state.json"))
    a = ap.parse_args()
    st = load_state(a.state)
    print(f"[{ts()}] sync start, state={a.state}", flush=True)
    while True:
        try:
            run_once(a, st)
            save_state(a.state, st)
        except Exception as e:
            print(f"[{ts()}] LOI: {e}", flush=True)
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
