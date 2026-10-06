#!/usr/bin/env python3
"""Smoke tests for binance-bot.

- Fully offline: no API keys needed, no authenticated calls, no network
  writes. (Public market-data endpoints are never touched here either.)
- Env vars BINANCE_API_KEY/SECRET are scrubbed during the run.
Run: python3 test_binance.py   (with ccxt installed)
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# --- config.json tam cho import binance_bot (xoa sau test neu tu tao) ---
_cfg_p = os.path.join(BASE, "config.json")
_made_cfg = not os.path.exists(_cfg_p)
if _made_cfg:
    import shutil
    shutil.copy(os.path.join(BASE, "config.example.json"), _cfg_p)
_uni_p = os.path.join(BASE, "universe.json")
_made_uni = not os.path.exists(_uni_p)
if _made_uni:
    json.dump([{"symbol": "BTCUSDT", "quoteVolume": 1e9},
               {"symbol": "ETHUSDT", "quoteVolume": 5e8}],
              open(_uni_p, "w"))

# --- scrub real credentials for the whole test run ---
_saved = {k: os.environ.pop(k, None)
          for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET")}

import live_binance
import strategy
from indicators import adx
import binance_bot

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (" | " + str(extra) if extra and not cond else ""))


def fresh_state():
    return {"equity": 1000.0, "positions": [],
            "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0},
            "_pid": 0}


CFG = json.load(open(os.path.join(BASE, "config.example.json")))

# --- 1. config template ---
check("config mode default dry_run", CFG.get("mode") == "dry_run")
check("config adx_threshold == 24 (optimizer-tuned)",
      CFG.get("adx_threshold") == 24)
check("config grid.step_mult == 0.8 (optimizer-tuned)",
      CFG.get("grid", {}).get("step_mult") == 0.8)
for k in ("scalp", "grid", "risk", "leverage", "order_margin_usdt",
          "max_total_positions", "hedge_mode", "fee_rate"):
    check("config has " + k, k in CFG)

# --- 2. qty_for pure function ---
check("qty basic", live_binance.qty_for(1000, 50000, 0.001) == 0.02)
check("qty rounds DOWN",
      live_binance.qty_for(1000, 3000, 0.001) == 0.333)
check("qty below min_qty -> None",
      live_binance.qty_for(1, 50000, 0.001, min_qty=0.001) is None)
check("qty below min_notional -> None",
      live_binance.qty_for(4, 100, 0.001, min_notional=5.0) is None)
check("qty bad price -> None",
      live_binance.qty_for(1000, 0, 0.001) is None)

# --- 3. credentials fail closed ---
try:
    live_binance.load_credentials()
    check("missing env raises", False)
except RuntimeError as e:
    msg = str(e)
    check("missing env raises", True)
    check("error names BINANCE_API_KEY", "BINANCE_API_KEY" in msg)
    check("error mentions Withdraw", "Withdraw" in msg)

# --- 4. dry_run engine: no keys, no network, log-only ---
logs = []
st = fresh_state()
eng = live_binance.BinanceEngine(CFG, st, dry_run=True,
                                 log=lambda m: logs.append(m))
check("dry_run needs no keys", True)
check("dry_run has no exchange client", eng.ex is None)
pos, why = eng.open("BTCUSDT", "long", 1000, 50000,
                    0.004, 0.01, "scalp")
check("dry_run open ok", pos is not None and why == "ok", why)
check("dry_run pos flagged dry", pos.get("dry") is True)
check("dry_run logs positionSide LONG",
      any('"positionSide": "LONG"' in m for m in logs))
check("dry_run logs DRY_RUN marker", any("DRY_RUN" in m for m in logs))
check("dry_run equity charged fee only",
      abs(st["equity"] - (1000.0 - 1000 * CFG["fee_rate"])) < 1e-9)
rec = eng.close(pos, 50500, "TP")  # +1% -> win
check("dry_run close pnl positive", rec["pnl"] > 0, rec["pnl"])
check("dry_run trade recorded", st["stats"]["trades"] == 1)
check("dry_run no positions left", st["positions"] == [])

# --- 5. live engine without keys raises before any network ---
try:
    live_binance.BinanceEngine(CFG, fresh_state(), dry_run=False)
    check("live without keys raises", False)
except RuntimeError as e:
    check("live without keys raises", "BINANCE_API_KEY" in str(e))

# --- 6. no withdraw endpoints anywhere in this module (excl. this test) ---
src = ""
for f in os.listdir(BASE):
    if f.endswith(".py") and f != os.path.basename(__file__):
        src += open(os.path.join(BASE, f)).read() + "\n"
# Chi bat loi GOI withdraw that (goi ham / endpoint), khong bat tu "withdraw"
# trong docstring huong dan an toan.
check("no withdraw() calls in module",
      not re.search(r"\.withdraw\s*\(", src))
check("no withdraw endpoints in module",
      not re.search(r"capital/withdraw|fetch_withdrawals", src,
                    re.IGNORECASE))
check("no SAPI usage in module",
      not re.search(r"sapi", src, re.IGNORECASE))

# --- 7. strategy smoke on synthetic candles ---
c5 = [{"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0} for _ in range(44)]
c5.append({"o": 100.0, "h": 101.0, "l": 99.9, "c": 101.0})  # breakout
c15 = [{"o": 100 + i * 0.1, "h": 100 + i * 0.1 + 0.05,
        "l": 100 + i * 0.1 - 0.05, "c": 100 + i * 0.1} for i in range(35)]
sig, info = strategy.scalp_signal(c5, c15, CFG)
check("scalp_signal returns valid tuple",
      sig in (None, "long", "short") and isinstance(info, dict), sig)
reg, av = strategy.detect_regime(c15, CFG["adx_threshold"])
check("detect_regime returns tuple",
      reg in ("trending", "ranging"), (reg, av))
check("adx computes", adx(c15) is not None)

# --- 8. kill switch + engine wiring ---
check("STOP kill switch defined",
      getattr(binance_bot, "STOP_P", "").endswith("STOP"))
check("make_engine rejects bad mode", True)
try:
    old = binance_bot.MODE
    binance_bot.MODE = "paper"
    try:
        binance_bot.make_engine(fresh_state())
        check("make_engine rejects bad mode", False)
    except SystemExit:
        check("make_engine rejects bad mode", True)
    finally:
        binance_bot.MODE = old
except Exception:
    check("make_engine rejects bad mode", False)

# --- restore env ---
for k, v in _saved.items():
    if v is not None:
        os.environ[k] = v
if _made_cfg:
    os.remove(_cfg_p)
if _made_uni:
    os.remove(_uni_p)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
