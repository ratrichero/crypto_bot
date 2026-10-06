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
import tempfile
import threading
import time

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
import binance_client
import binance_safety
import binance_ws
import binance_user_ws
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
          "max_total_positions", "hedge_mode", "fee_rate", "recv_window_ms",
          "order_event_timeout_seconds", "reconcile_interval_seconds",
          "exchange_protection"):
    check("config has " + k, k in CFG)
check("exchange protection disabled by default",
      CFG.get("exchange_protection") is False)

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
check("hedge close omits reduceOnly", not any("reduceOnly" in m
      for m in logs if 'DRY_RUN dat lenh' in m and 'LONG' in m))

# --- 4b. private-event and idempotent-order unit wiring (no network) ---
user_eng = object.__new__(live_binance.BinanceEngine)
user_eng._order_events = {}
user_eng._order_condition = threading.Condition()
user_eng._last_reconcile = time.time()
user_eng._last_account_event = 0.0
user_eng._account_position_snapshot = {}
user_logs = []
user_eng.log = user_logs.append
user_eng.on_user_event({
    "e": "ORDER_TRADE_UPDATE",
    "o": {"i": "123", "s": "BTCUSDT", "X": "FILLED",
          "x": "TRADE", "ps": "LONG", "ap": "50001"},
})
order_event = user_eng._wait_order_event("123", 0.01)
check("ORDER_TRADE_UPDATE wakes waiter",
      order_event and order_event.get("X") == "FILLED")
check("ORDER_TRADE_UPDATE logs positionSide",
      any("positionSide=LONG" in m for m in user_logs))
user_eng.on_user_event({"e": "ACCOUNT_UPDATE", "a": {
    "P": [{"s": "BTCUSDT", "ps": "LONG", "pa": "0.02"}],
}})
check("ACCOUNT_UPDATE stores aggregate position",
      user_eng._account_position_snapshot[("BTCUSDT", "long")] == 0.02)
check("ACCOUNT_UPDATE requests reconciliation", user_eng._last_reconcile == 0.0)

agg_eng = object.__new__(live_binance.BinanceEngine)
agg_eng.dry_run = False
agg_eng._symbol_map = {}
agg_eng._markets = {}
agg_eng._raw_symbol = lambda p: p.get("info", {}).get("symbol")
agg_rows = [
    {"contracts": 0.02, "info": {"symbol": "BTCUSDT", "positionSide": "LONG"}},
    {"contracts": 0.03, "info": {"symbol": "BTCUSDT", "positionSide": "LONG"}},
    {"contracts": 0.01, "info": {"symbol": "BTCUSDT", "positionSide": "SHORT"}},
]
agg = agg_eng._aggregate_positions(agg_rows)
check("aggregate reconciliation groups grid lots", agg[("BTCUSDT", "long")] == 0.05)
check("aggregate reconciliation keeps Hedge sides", agg[("BTCUSDT", "short")] == 0.01)
agg_eng.cfg = {"reconcile_interval_seconds": 0}
agg_eng._last_reconcile = 0.0
agg_eng.state = {"positions": [
    {"symbol": "BTCUSDT", "side": "long", "qty": 0.02},
    {"symbol": "BTCUSDT", "side": "long", "qty": 0.03},
    {"symbol": "BTCUSDT", "side": "short", "qty": 0.01},
], "halted": False, "halt_reason": ""}
agg_eng._filters_for = lambda _: (0.001, 0.001, 5.0)
agg_eng.log = lambda _: None
check("aggregate reconciliation accepts matching lots",
      agg_eng.reconcile_positions(force=True, rows=agg_rows))
agg_eng.state["positions"][1]["qty"] = 0.02
check("aggregate reconciliation halts on quantity drift",
      not agg_eng.reconcile_positions(force=True, rows=agg_rows)
      and agg_eng.state["halted"])

id_eng = object.__new__(live_binance.BinanceEngine)
id_eng.dry_run = False
id_eng.cfg = {"new_order_resp_type": "RESULT"}
id_eng.state = {"_client_nonce": 0}
id_eng._client_nonce = 0
id_eng._last_order_response = None
id_eng.log = lambda _: None
id_eng._ccxt_symbol = lambda _: "BTC/USDT:USDT"
id_eng.ex = type("FakeExchange", (), {
    "create_market_buy_order": lambda *args: None,
    "create_market_sell_order": lambda *args: None,
    "fapiPrivateGetOrder": lambda *args: None,
})()
order_calls = []
def fake_private(endpoint, fn, *args):
    if endpoint == "private:trade":
        order_calls.append((endpoint, args))
        raise TimeoutError("simulated transport timeout")
    return {"orderId": "9001", "clientOrderId": args[0].get("origClientOrderId")}
id_eng._private_call = fake_private
reconciled_id, reconciled_client_id = id_eng._place_market(
    "BTCUSDT", "buy", 0.02, "LONG", ref_price=50000
)
params_sent = order_calls[0][1][2]
check("timeout recovery returns existing order", reconciled_id == "9001")
check("timeout recovery keeps client id", reconciled_client_id == params_sent["newClientOrderId"])
check("timeout recovery makes one create call", len(order_calls) == 1)
check("order payload has RESULT response", params_sent["newOrderRespType"] == "RESULT")

prot = object.__new__(live_binance.BinanceEngine)
prot.dry_run = False
prot.cfg = {"exchange_protection": True, "protection_working_type": "MARK_PRICE",
            "protection_price_protect": False}
prot._price_ticks = {"BTCUSDT": 0.1}
prot._filters_for = lambda _: (0.001, 0.001, 5.0)
prot._new_client_order_id = lambda symbol, side: "algo-test-" + side
prot.ex = type("FakeExchange", (), {
    "fapiPrivatePostAlgoOrder": lambda *args: None,
    "fapiPrivateDeleteAlgoOrder": lambda *args: None,
})()
algo_calls = []
def fake_algo(endpoint, fn, params):
    algo_calls.append(params)
    return {"algoId": str(7000 + len(algo_calls))}
prot._private_call = fake_algo
protection_ids = prot._create_exchange_protection({
    "symbol": "BTCUSDT", "side": "long", "qty": 0.02,
    "sl": 49000.03, "tp": 51000.07,
})
check("algo protection creates SL and TP", set(protection_ids) == {"sl", "tp"})
check("algo protection sends Hedge positionSide",
      all(p.get("positionSide") == "LONG" for p in algo_calls))
check("algo protection has no reduceOnly",
      all("reduceOnly" not in p for p in algo_calls))
check("algo protection rounds trigger to tick",
      algo_calls[0]["triggerPrice"] == "49000.0")

# Failed order actions cool both the action and the symbol.
fail_st = fresh_state()
fail_eng = live_binance.BinanceEngine(CFG, fail_st, dry_run=True,
                                      log=lambda _: None)
fail_eng.get_balance_usdt = lambda: (_ for _ in ()).throw(
    RuntimeError("synthetic balance failure"))
_, first_why = fail_eng.open("BTCUSDT", "long", 1000, 50000,
                             0.004, 0.01, "grid", level="b1")
_, second_why = fail_eng.open("BTCUSDT", "short", 1000, 50000,
                              0.004, 0.01, "grid", level="s1")
check("failed action has cooldown", "action_failed" in first_why)
check("same symbol is cooled", "cooldown" in second_why)

# --- 5. live engine without keys raises before any network ---
try:
    live_binance.BinanceEngine(CFG, fresh_state(), dry_run=False)
    check("live without keys raises", False)
except RuntimeError as e:
    check("live without keys raises", "BINANCE_API_KEY" in str(e))

# --- 6. request safety: rate-limit response is observed once, never retried ---
class _FakeResponse:
    status_code = 429
    headers = {
        "Retry-After": "7",
        "X-MBX-USED-WEIGHT-1M": "2400",
    }
    text = '{"code":-1003,"msg":"Too many requests; IP banned"}'

    def json(self):
        return {"code": -1003, "msg": "Too many requests; IP banned"}


class _FakeSession:
    def __init__(self):
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return _FakeResponse()


old_session = binance_client.SESSION
fake_session = _FakeSession()
state_fd, state_path = tempfile.mkstemp(prefix="binance-circuit-test-", suffix=".json")
os.close(state_fd)
os.remove(state_path)
binance_client.SESSION = fake_session
binance_safety.configure(state_path=state_path)
try:
    try:
        binance_client._get("/fapi/v2/ticker/price", tries=3)
        check("429 raises", False)
    except binance_safety.BinanceRateLimitError as e:
        check("429 raises", True)
        check("429 keeps API code -1003", "api_code=-1003" in str(e))
    check("429 is not retried", fake_session.calls == 1, fake_session.calls)
    try:
        binance_client._get("/fapi/v2/ticker/price", tries=3)
        check("circuit blocks next request", False)
    except binance_safety.BinanceCircuitOpen:
        check("circuit blocks next request", True)
finally:
    binance_client.SESSION = old_session
    binance_safety.reset_for_tests()
    try:
        os.remove(state_path)
    except FileNotFoundError:
        pass

# --- 7. data-only mode and routed websocket endpoint ---
data_engine = live_binance.DataOnlyEngine(log=lambda _: None)
check("data_only has no exchange client", data_engine.ex is None)
check("WS uses routed market endpoint", "/market/stream?streams=" in binance_ws.URL)
check("user WS uses private endpoint", binance_user_ws.URL.endswith("/private/ws/"))

# --- 8. no withdraw endpoints anywhere in this module (excl. this test) ---
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
