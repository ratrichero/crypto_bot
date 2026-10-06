#!/usr/bin/env python3
"""Snapshot equity Binance Futures LIVE moi 15s -> Neon (equity_snapshots).

Equity o day = totalMarginBalance THUC tren san (signed GET /fapi/v2/account).
Doc API key/secret tu /home/ubuntu/muse_bot/.env (KHONG in/log secret).
Gap 429/418 -> bo qua ky do, KHONG retry (bai hoc ban IP).
"""
import hashlib
import hmac
import os
import time
import traceback
import urllib.parse
from datetime import datetime, timezone

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_P = os.path.join(BASE, "binance_equity_snap.log")
SYSTEM = "binance"
INTERVAL = 15  # giay
ACCT_URL = "https://fapi.binance.com/fapi/v2/account"


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


def env_val(key):
    if os.environ.get(key):
        return os.environ[key]
    for p in ("/home/ubuntu/muse_bot/.env",):
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return None


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
                    if line.startswith("postgres"):
                        return line
        except OSError:
            pass
    return None


_session = requests.Session()


def fetch_equity(api_key, api_secret):
    """Tra ve totalMarginBalance. Tra ve (None, ly_do) neu bi 429/418."""
    ts = int(time.time() * 1000)
    params = {"timestamp": ts, "recvWindow": 5000}
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(api_secret.encode(), qs.encode(),
                   hashlib.sha256).hexdigest()
    try:
        r = _session.get(
            ACCT_URL, params={**params, "signature": sig},
            headers={"X-MBX-APIKEY": api_key}, timeout=10)
    except Exception as e:
        return None, f"network: {type(e).__name__}"
    if r.status_code in (429, 418):
        return None, f"rate-limited {r.status_code} (bo qua)"
    if r.status_code != 200:
        return None, f"http {r.status_code}"
    try:
        d = r.json()
        return float(d["totalMarginBalance"]), None
    except Exception as e:
        return None, f"parse: {e}"


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


def once(api_key, api_secret):
    t0 = time.time()
    equity, err = fetch_equity(api_key, api_secret)
    if err:
        log(f"skip: {err}")
        return
    try:
        save_snapshot(equity)
        log(f"equity={equity:.2f} U | {time.time()-t0:.1f}s")
    except Exception:
        log("ERROR save:\n" + traceback.format_exc())


def main():
    api_key = env_val("BINANCE_API_KEY")
    api_secret = env_val("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        log("ERROR: thieu BINANCE_API_KEY/SECRET trong .env")
        raise SystemExit(1)
    log(f"binance_equity_snap start: system={SYSTEM}, moi {INTERVAL}s "
        f"(key ****{api_key[-4:]})")
    while True:
        once(api_key, api_secret)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
