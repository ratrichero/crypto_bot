#!/usr/bin/env python3
"""Tests for live_okx.py.

- Signature tests use vectors computed INDEPENDENTLY via openssl CLI
  (not via live_okx.sign), so they validate message assembly.
- No real key file is needed; no real HTTP call is ever made.
Run: python3 test_live_okx.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_okx

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra) if extra and not cond else ""))


# --- 1. signature vectors (openssl-generated, independent of sign()) ---
TS = "2020-12-08T09:08:57.715Z"
SECRET = "testsecret123"
B1 = '{"instId":"BTC-USDT-SWAP","tdMode":"cross","side":"buy","ordType":"market","sz":"1"}'

check("sign POST vector",
      live_okx.sign(TS, "POST", "/api/v5/trade/order", B1, SECRET)
      == "ZANGdaiyIoLeCvL/P/26gur5bNxNa49C920YJgQsjlA=")

check("sign GET vector (empty body, lowercase method)",
      live_okx.sign(TS, "get", "/api/v5/account/balance?ccy=USDT", "", SECRET)
      == "O+dGfCXcD/LhxKOwVxiP4EVQ0EVKx3YJluBHQ7w+wTs=")

check("sign body None == body ''",
      live_okx.sign(TS, "GET", "/api/v5/account/balance", None, SECRET)
      == live_okx.sign(TS, "GET", "/api/v5/account/balance", "", SECRET))

# --- 2. missing key file -> clear error, no secret in message ---
try:
    live_okx.load_credentials("/tmp/okx-key-khong-ton-tai-xyz.json")
    check("missing key raises", False)
except RuntimeError as e:
    msg = str(e)
    check("missing key raises", True)
    check("error names the path", "/tmp/okx-key-khong-ton-tai-xyz.json" in msg)
    check("error mentions chmod + withdraw",
          "chmod 600" in msg and "Withdraw" in msg)
    check("no secret value leaked", SECRET not in msg)

bad = "/tmp/okx-key-bad.json"
open(bad, "w").write(json.dumps({"api_key": "x"}))
try:
    live_okx.load_credentials(bad)
    check("incomplete key raises", False)
except RuntimeError as e:
    check("incomplete key raises", "api_secret" in str(e))
os.remove(bad)

# --- 3. dry_run makes ZERO http calls and needs no key ---
calls = []
import requests as _rq
_orig = _rq.Session.request


def _boom(self, *a, **k):
    calls.append((a, k))
    raise AssertionError("dry_run must not touch network")


_rq.Session.request = _boom
try:
    cfg = {"fee_rate": 0.0005, "leverage": 10, "slippage": 0.0001,
           "max_total_positions": 10, "risk": {"max_notional_mult": 10.0}}
    st = {"equity": 1000.0, "_pid": 0, "positions": [],
          "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0}}
    eng = live_okx.LiveEngine(cfg, st, dry_run=True,
                              log=lambda m: None)
    pos, why = eng.open("BTC-USDT-SWAP", "long", 1000.0, 100000.0,
                        0.004, 0.01, "scalp")
    check("dry_run open ok", pos is not None and why == "ok", why)
    check("dry_run pos has sl/tp", pos and pos["sl"] and pos["tp"])
    check("dry_run sl below entry", pos and pos["sl"] < pos["entry"])
    check("dry_run pos marked dry", pos and pos.get("dry") is True)
    rec = eng.close(pos, 101000.0, "TP")
    check("dry_run close pnl>0", rec["pnl"] > 0, rec["pnl"])
    check("dry_run state empty after close", st["positions"] == [])
    check("dry_run equity moved", st["equity"] != 1000.0)
    check("dry_run unrealized works",
          isinstance(eng.unrealized({"BTC-USDT-SWAP": 100500.0}), float))
    check("dry_run made zero http calls", calls == [], calls)
finally:
    _rq.Session.request = _orig

# --- 4. contract sizing ---
check("contracts_for basic",
      live_okx.contracts_for(1000.0, 100000.0, "0.01", "0.01", "0.01") == "1")
check("contracts_for rounds DOWN",
      live_okx.contracts_for(1500.0, 100000.0, "0.01", "1", "1") == "1")
check("contracts_for below minSz -> None",
      live_okx.contracts_for(1.0, 100000.0, "0.01", "0.01", "0.01") is None)
check("contracts_for tiny lot",
      live_okx.contracts_for(1000.0, 2.5, "10", "1", "1") == "40")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
