#!/usr/bin/env python3
"""Snapshot equity paper cua radar meme moi 60s -> Neon (equity_snapshots).

Equity o day = P&L TICH LUY (USD), bat dau tu 0 — KHONG phai tong von.
  equity = tong pnl lenh da dong (paper_trades*.jsonl, tinh tu legs)
         + tong 'realized' cua vi the dang mo (phan da chot TP1/TP2...)
         + tong unrealized phan con lai: (mark - entry)/entry * size_usd * remaining

Gia mark lay tu DexScreener (cache 60s, giong radar.py). Token nao khong lay
duoc gia thi bo qua (khong crash vong lap).
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_P = os.path.join(BASE, "radar_state.json")
LOG_P = os.path.join(BASE, "equity_snap.log")
SYSTEM = "radar"
INTERVAL = 15  # giay — realtime do tre thap theo yeu cau
JUP_PRICE = "https://lite-api.jup.ag/price/v3"


def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_P) and os.path.getsize(LOG_P) > 2 * 1024 * 1024:
            os.replace(LOG_P, LOG_P + ".1")
    except OSError:
        pass
    try:
        with open(LOG_P, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


_price_cache = {}


def jup_prices(mints):
    """Gia USD batch qua Jupiter Price v3: 1 request cho tat ca mint.
    Tra ve dict {mint: usdPrice}. Mint nao loi -> khong co trong dict."""
    out = {}
    mints = [m for m in dict.fromkeys(mints) if m]
    if not mints:
        return out
    try:
        r = requests.get(JUP_PRICE, params={"ids": ",".join(mints)},
                         timeout=12)
        r.raise_for_status()
        d = r.json()
        if isinstance(d, dict):
            for m in mints:
                px = (d.get(m) or {}).get("usdPrice")
                if px:
                    out[m] = float(px)
    except Exception:
        pass
    return out


def ds_price(addr):
    """Gia USD tu DexScreener (fallback), cache 60s. Tra ve None neu loi."""
    now = time.time()
    c = _price_cache.get(addr)
    if c and now - c[0] < 60:
        return c[1]
    px = None
    try:
        r = requests.get(
            f"https://api.dexscreener.com/tokens/v1/solana/{addr}", timeout=12)
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, list) and rows:
            px = float(rows[0]["priceUsd"])
    except Exception:
        px = None
    _price_cache[addr] = (now, px)
    return px


def legs_pnl(t):
    """P&L USD cua 1 lenh (dong hoac dang mo) tu legs."""
    try:
        return sum(l.get("frac", 0) * l.get("ret", 0)
                   for l in t.get("legs", [])) * t.get("size_usd", 0)
    except Exception:
        return 0.0


def realized_closed():
    """Tong pnl cac lenh da dong tu 2 file JSONL."""
    tot, n = 0.0, 0
    for fn in ("paper_trades.jsonl", "paper_trades_holder.jsonl"):
        p = os.path.join(BASE, fn)
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        tot += legs_pnl(json.loads(line))
                        n += 1
                    except Exception:
                        pass
        except OSError:
            pass
    return tot, n


def compute_equity():
    """Tra ve (equity, chi_tiet)."""
    try:
        st = json.load(open(STATE_P))
    except Exception as e:
        raise RuntimeError(f"khong doc duoc radar_state.json: {e}")
    positions = st.get("paper", []) + st.get("paper_holder", [])
    closed_pnl, n_closed = realized_closed()

    realized_open = 0.0
    unreal = 0.0
    n_priced = 0
    n_noprice = 0
    # gia batch qua Jupiter (1 request), thieu con nao thi fallback DexScreener
    mints = [p.get("token", "") for p in positions]
    marks = jup_prices(mints)
    for p in positions:
        try:
            realized_open += float(p.get("realized", 0) or 0)
            entry = float(p.get("entry", 0) or 0)
            size = float(p.get("size_usd", 0) or 0)
            rem = float(p.get("remaining", 1) or 0)
            if entry <= 0 or size <= 0 or rem <= 0:
                continue
            mark = marks.get(p.get("token", ""))
            if mark is None:
                mark = ds_price(p.get("token", ""))
            if mark is None or mark <= 0:
                n_noprice += 1
                continue
            unreal += (mark - entry) / entry * size * rem
            n_priced += 1
        except Exception:
            n_noprice += 1
    equity = closed_pnl + realized_open + unreal
    detail = (f"closed={closed_pnl:+.1f}({n_closed}) "
              f"realized_open={realized_open:+.1f} unreal={unreal:+.1f} "
              f"priced={n_priced}/{len(positions)}")
    return equity, detail


def db_url():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    for p in ("/home/ubuntu/muse_bot/.env",
              os.path.expanduser("~/.neon_db_url")):
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        return line.split("=", 1)[1].strip()
                    # file chi chua URL tran
                    if line.startswith("postgres"):
                        return line
        except OSError:
            pass
    return None


def save_snapshot(equity):
    import psycopg
    url = db_url()
    if not url:
        raise RuntimeError("thieu DATABASE_URL")
    with psycopg.connect(url) as c:
        c.execute(
            """INSERT INTO equity_snapshots (ts, system, equity)
               VALUES (now(), %s, %s)
               ON CONFLICT (ts, system) DO NOTHING""",
            (SYSTEM, float(equity)))


def once():
    t0 = time.time()
    try:
        equity, detail = compute_equity()
    except Exception:
        log("ERROR compute:\n" + traceback.format_exc())
        return
    try:
        save_snapshot(equity)
        log(f"equity={equity:+.2f} U | {detail} | {time.time()-t0:.1f}s")
    except Exception:
        log("ERROR save:\n" + traceback.format_exc())


def main():
    log(f"equity_snap start: system={SYSTEM}, moi {INTERVAL}s")
    while True:
        once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
