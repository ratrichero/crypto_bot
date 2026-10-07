#!/usr/bin/env python3
"""Snapshot equity VI LIVE Solana moi 15s -> Neon (equity_snapshots,
system='radar_live'). Tuong tu binance_equity_snap cho tab Binance LIVE.

Equity = SOL trong vi x gia SOL + gia tri token cua vi the live dang mo
(live_positions.json: tokens_base x remaining x gia Jupiter; token chua co
gia -> tinh theo gia vao de do thi khong tut ao).

CHI DOC: khong can private key, khong gui tx. Helius key/vi lay giong
reconcile_wallet (env -> .env goc -> .helius_key / config.live.json).
Gia: Jupiter Price v3 (api.jup.ag + JUPITER_API_KEY neu co, loi -> lite-api).
Chay duoi pm2: app muse-live-equity (deploy/apps.json).
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from reconcile_wallet import read_env_file, resolve_settings  # noqa: E402
from txparse import SOL_MINT  # noqa: E402

REPO = os.path.dirname(BASE)
LOG_P = os.path.join(BASE, "live_equity_snap.log")
POS_P = os.path.join(BASE, "live_positions.json")
SYSTEM = "radar_live"
INTERVAL = float(os.environ.get("LIVE_EQUITY_INTERVAL") or 15)
JUP_KEYED = "https://api.jup.ag/price/v3"
JUP_FREE = "https://lite-api.jup.ag/price/v3"


def log(msg):
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_P) and os.path.getsize(LOG_P) > 2 * 1024 * 1024:
            os.replace(LOG_P, LOG_P + ".1")
        with open(LOG_P, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- pure

def position_tokens(p):
    """So token (don vi day du) vi the con giu."""
    try:
        base = float(p.get("tokens_base") or 0)
        rem = float(p.get("remaining", 1.0) or 0)
        dec = int(p.get("decimals") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(base * rem, 0.0) / (10 ** dec)


def compute_equity(sol_lamports, sol_px, positions, prices):
    """-> (equity_usd, chi_tiet). prices: {mint: usd}. Token khong co gia ->
    gia vao (entry) de khong tut ao; dem so token thieu gia."""
    sol_usd = sol_lamports / 1e9 * sol_px
    tok_usd, missing = 0.0, 0
    for p in positions or []:
        n = position_tokens(p)
        if n <= 0:
            continue
        px = prices.get(p.get("token"))
        if not px:
            missing += 1
            try:
                px = float(p.get("entry") or 0)
            except (TypeError, ValueError):
                px = 0.0
        tok_usd += n * px
    return sol_usd + tok_usd, {"sol_usd": sol_usd, "tokens_usd": tok_usd,
                               "missing_price": missing}


def parse_prices(d, mints):
    out = {}
    for m in mints:
        try:
            px = float((d.get(m) or {}).get("usdPrice") or 0)
        except (TypeError, ValueError, AttributeError):
            px = 0.0
        if px > 0:
            out[m] = px
    return out


# ---------------------------------------------------------------- io

def jupiter_key(env=None):
    env = dict(os.environ if env is None else env)
    for k, v in read_env_file(os.path.join(REPO, ".env"),
                              {"JUPITER_API_KEY"}).items():
        env.setdefault(k, v)
    key = (env.get("JUPITER_API_KEY") or "").strip()
    if key:
        return key
    try:
        with open(os.path.join(BASE, ".jupiter_key")) as f:
            return f.read().strip()
    except (IOError, OSError):
        return ""


class Snapper:
    def __init__(self, helius_key, wallet, jup_key="", db_url=None,
                 session=None, connect=None):
        self.rpc = ("https://mainnet.helius-rpc.com/?api-key=%s" % helius_key
                    if helius_key else "https://api.mainnet-beta.solana.com")
        self.wallet, self.jup_key = wallet, jup_key
        self.db_url = db_url
        self.s = session or requests.Session()
        self._connect = connect
        self._conn = None
        self.keyed_off_until = 0.0
        self.n = 0

    def sol_lamports(self):
        r = self.s.post(self.rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [self.wallet, {"commitment": "confirmed"}]}, timeout=10)
        r.raise_for_status()
        return int(r.json()["result"]["value"])

    def prices(self, mints):
        use_key = bool(self.jup_key) and time.time() >= self.keyed_off_until
        url = JUP_KEYED if use_key else JUP_FREE
        hdr = {"x-api-key": self.jup_key} if use_key else None
        r = self.s.get(url, params={"ids": ",".join(mints)}, headers=hdr,
                       timeout=10)
        if use_key and r.status_code in (401, 403):
            self.keyed_off_until = time.time() + 1800
            log("Jupiter tu choi key (%d) -> lite-api 30 phut" % r.status_code)
            return self.prices(mints)
        r.raise_for_status()
        return parse_prices(r.json(), mints)

    def save(self, equity):
        if self._conn is None or getattr(self._conn, "closed", False):
            if self._connect:
                self._conn = self._connect()
            else:
                import psycopg
                self._conn = psycopg.connect(self.db_url, autocommit=True,
                                             connect_timeout=10)
        try:
            self._conn.execute(
                """INSERT INTO equity_snapshots (ts, system, equity)
                   VALUES (now(), %s, %s)
                   ON CONFLICT (ts, system) DO NOTHING""",
                (SYSTEM, float(equity)))
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            raise

    def once(self, positions):
        mints = [SOL_MINT] + list(dict.fromkeys(
            p.get("token") for p in positions if p.get("token")))
        lam = self.sol_lamports()
        px = self.prices(mints)
        sol_px = px.get(SOL_MINT)
        if not sol_px:
            log("skip: khong co gia SOL")
            return None
        eq, det = compute_equity(lam, sol_px, positions, px)
        self.save(eq)
        self.n += 1
        if self.n == 1 or self.n % 40 == 0:
            log("equity=%.2f U (SOL %.2f + token %.2f, %d vi the%s)" % (
                eq, det["sol_usd"], det["tokens_usd"], len(positions),
                ", %d thieu gia" % det["missing_price"]
                if det["missing_price"] else ""))
        return eq


def load_positions(path=POS_P):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def main():
    helius, wallet, src = resolve_settings()
    url = os.environ.get("DATABASE_URL") or read_env_file(
        os.path.join(REPO, ".env"), {"DATABASE_URL"}).get("DATABASE_URL")
    if not wallet or not url:
        log("ERROR: thieu %s" % ("vi (SOL_WALLET/config.live.json)"
                                 if not wallet else "DATABASE_URL"))
        raise SystemExit(1)
    jk = jupiter_key()
    sn = Snapper(helius, wallet, jk, url)
    log("live_equity_snap start: system=%s, moi %gs, vi %s...%s, RPC %s, "
        "Jupiter %s" % (SYSTEM, INTERVAL, wallet[:6], wallet[-4:],
                        "Helius" if helius else "public",
                        "co key" if jk else "lite-api"))
    while True:
        t0 = time.time()
        try:
            sn.once(load_positions())
        except Exception as e:
            log("skip: %s: %s" % (type(e).__name__, str(e)[:200]
                                  .replace(helius or "\0", "***")))
            if not isinstance(e, (requests.RequestException, ValueError,
                                  KeyError)):
                log(traceback.format_exc(limit=3).replace(helius or "\0",
                                                          "***"))
        time.sleep(max(1.0, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
