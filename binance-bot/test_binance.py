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
check("config adx_threshold == 25", CFG.get("adx_threshold") == 25)
check("config adx hysteresis threshold == 20",
      CFG.get("adx_range_threshold") == 20)
check("config regime confirmation == 2 bars",
      CFG.get("regime_confirm_bars") == 2)
check("config grid.step_mult == 0.8 (optimizer-tuned)",
      CFG.get("grid", {}).get("step_mult") == 0.8)
for k in ("scalp", "grid", "risk", "leverage", "order_margin_usdt",
          "max_total_positions", "hedge_mode", "fee_rate", "recv_window_ms",
          "order_event_timeout_seconds", "reconcile_interval_seconds",
          "equity_refresh_seconds", "min_liquidation_buffer_pct",
          "exchange_protection"):
    check("config has " + k, k in CFG)
check("exchange protection disabled by default",
      CFG.get("exchange_protection") is False)
check("daily stop starts at 10 percent",
      CFG.get("risk", {}).get("daily_max_loss_pct") == 0.1)
check("grid basket stop configured",
      CFG.get("risk", {}).get("grid_basket_max_loss_pct") == 0.02)
check("grid opens at most one level per cycle",
      CFG.get("grid", {}).get("max_entries_per_cycle") == 1)
check("liquidation buffer configured",
      CFG.get("min_liquidation_buffer_pct") == 0.05)

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
mtm_st = fresh_state()
mtm_eng = live_binance.BinanceEngine(CFG, mtm_st, dry_run=True,
                                     log=lambda _: None)
mtm_st["positions"].append({"side": "long", "symbol": "BTCUSDT",
                             "entry": 50000.0, "qty": 0.02})
check("dry_run mark-to-market includes unrealized",
      abs(mtm_eng.mark_to_market_equity({"BTCUSDT": 49000.0}) - 980.0) < 1e-9)
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
agg_eng.cfg = {"reconcile_interval_seconds": 0,
                "min_liquidation_buffer_pct": 0.05}
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
agg_eng.state["halted"] = False
agg_eng.state["halt_reason"] = ""
liq_rows = [{"contracts": 0.02,
             "info": {"symbol": "BTCUSDT", "positionSide": "LONG",
                       "markPrice": "50000", "liquidationPrice": "48000"}}]
check("liquidation buffer halts near liquidation",
      not agg_eng.reconcile_positions(force=True, rows=liq_rows)
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
check("algo protection uses documented boolean string",
      all(p.get("priceProtect") == "false" for p in algo_calls))

# --- 4c. fail-closed order/protection recovery paths ---
status_eng = object.__new__(live_binance.BinanceEngine)
status_eng.dry_run = False
status_eng.cfg = {"new_order_resp_type": "RESULT"}
status_eng.state = {"_client_nonce": 0, "halted": False}
status_eng._client_nonce = 0
status_eng._last_order_response = None
status_eng.log = lambda _: None
status_eng._ccxt_symbol = lambda _: "BTC/USDT:USDT"
status_eng.ex = type("FakeExchange", (), {
    "create_market_buy_order": lambda *args: None,
    "create_market_sell_order": lambda *args: None,
    "fapiPrivateGetOrder": lambda *args: None,
})()
status_calls = []
def fake_status_private(endpoint, fn, *args):
    if endpoint == "private:trade":
        status_calls.append(args)
        raise TimeoutError("simulated transport timeout")
    return {"orderId": "9002", "status": "CANCELED",
            "executedQty": "0", "origQty": "0.02"}
status_eng._private_call = fake_status_private
try:
    status_eng._place_market("BTCUSDT", "buy", 0.02, "LONG", ref_price=50000)
    status_error = None
except RuntimeError as exc:
    status_error = str(exc)
check("timeout recovery rejects canceled order",
      status_error is not None and "CANCELED" in status_error)
check("canceled recovery does not create a second order",
      len(status_calls) == 1)

fill_eng = object.__new__(live_binance.BinanceEngine)
fill_eng.dry_run = False
fill_eng.cfg = {"order_event_timeout_seconds": 0}
fill_eng.state = {"halted": False}
fill_eng._last_order_response = {
    "orderId": "9003", "status": "NEW", "origQty": "0.02",
    "executedQty": "0",
}
fill_eng._user_ws = None
fill_eng._ccxt_symbol = lambda _: "BTC/USDT:USDT"
fill_eng.log = lambda _: None
fill_eng._private_call = lambda *args: {
    "status": "NEW", "amount": "0.02", "filled": "0"
}
old_sleep = live_binance.time.sleep
live_binance.time.sleep = lambda _: None
try:
    fill_eng._fill_price("BTCUSDT", "9003", 50000)
    fill_error = None
except RuntimeError as exc:
    fill_error = str(exc)
finally:
    live_binance.time.sleep = old_sleep
check("unknown market fill does not use reference price",
      fill_error is not None and fill_eng.state["halted"])

close_eng = object.__new__(live_binance.BinanceEngine)
close_eng.dry_run = False
close_eng.cfg = {"exchange_protection": True}
close_eng.state = fresh_state()
close_eng._action_failures = {}
close_eng._action_cooldowns = {}
close_eng._symbol_cooldowns = {}
close_eng._cooldown_base = 30.0
close_eng._cooldown_max = 900.0
close_eng.log = lambda _: None
close_eng._filters_for = lambda _: (0.001, 0.001, 5.0)
close_eng.ex = type("FakeExchange", (), {
    "fapiPrivateDeleteAlgoOrder": lambda *args: None,
})()
close_eng._private_call = lambda *args, **kwargs: (_ for _ in ()).throw(
    RuntimeError("HTTP 500 protection delete failed"))
market_close_calls = []
close_eng._place_market = lambda *args, **kwargs: market_close_calls.append(args)
close_pos = {"id": 77, "symbol": "BTCUSDT", "side": "long", "qty": 0.02,
             "entry": 50000.0, "notional": 1000.0, "tag": "scalp",
             "sl_algo_id": "701", "tp_algo_id": "702"}
close_eng.state["positions"] = [close_pos]
check("protection cancel failure blocks market close",
      close_eng.close(close_pos, 50000.0, "SL") is None
      and market_close_calls == [])
check("protection cancel failure keeps local position",
      close_eng.state["positions"] == [close_pos])

# A missing Algo id is only acceptable when the Hedge position is verified to
# still exist; otherwise the opposite-side close could reverse the account.
absent_eng = object.__new__(live_binance.BinanceEngine)
absent_eng.dry_run = False
absent_eng.cfg = {"exchange_protection": True}
absent_eng.state = fresh_state()
absent_eng._filters_for = lambda _: (0.001, 0.001, 5.0)
absent_eng._aggregate_positions = lambda rows: {}
absent_eng.log = lambda _: None
absent_eng.ex = type("FakeExchange", (), {
    "fapiPrivateDeleteAlgoOrder": lambda *args: None,
    "fetch_positions": lambda *args: [],
})()
def absent_private(endpoint, fn, *args):
    if endpoint == "private:trade":
        raise RuntimeError("-2013 Order does not exist")
    return []
absent_eng._private_call = absent_private
absent_pos = {"symbol": "BTCUSDT", "side": "long", "qty": 0.02,
              "sl_algo_id": "703"}
try:
    absent_eng._cancel_exchange_protection(absent_pos)
    absent_error = None
except RuntimeError as exc:
    absent_error = str(exc)
check("absent protection with no Hedge leg blocks close",
      absent_error is not None and "position" in absent_error.lower())

startup_prot = object.__new__(live_binance.BinanceEngine)
startup_prot.dry_run = False
startup_prot.cfg = {"exchange_protection": True}
startup_prot.state = {"positions": [{
    "id": 78, "symbol": "BTCUSDT", "side": "long", "qty": 0.02,
    "sl": 49000.0, "tp": 51000.0,
    "sl_algo_id": "801", "tp_algo_id": "802",
}], "halted": False}
startup_prot._bot_symbols = {"BTCUSDT"}
startup_prot.log = lambda _: None
startup_prot._raw_symbol = lambda p: (p.get("info") or {}).get("symbol") or p.get("symbol")
startup_prot.ex = type("FakeExchange", (), {
    "fapiPrivateGetOpenAlgoOrders": lambda *args: None,
})()
startup_prot._private_call = lambda *args, **kwargs: [
    {"algoId": "801", "symbol": "BTCUSDT", "positionSide": "LONG",
     "side": "SELL", "orderType": "STOP_MARKET", "quantity": "0.02"},
    {"algoId": "802", "symbol": "BTCUSDT", "positionSide": "LONG",
     "side": "SELL", "orderType": "TAKE_PROFIT_MARKET", "quantity": "0.02"},
]
check("startup protection reconciliation accepts matching Hedge guards",
      startup_prot._reconcile_exchange_protection([]))
startup_prot._private_call = lambda *args, **kwargs: [
    {"algoId": "801", "symbol": "BTCUSDT", "positionSide": "SHORT",
     "side": "SELL", "orderType": "STOP_MARKET", "quantity": "0.02"},
    {"algoId": "802", "symbol": "BTCUSDT", "positionSide": "LONG",
     "side": "SELL", "orderType": "TAKE_PROFIT_MARKET", "quantity": "0.02"},
]
startup_prot.state["halted"] = False
check("startup protection reconciliation halts wrong Hedge side",
      not startup_prot._reconcile_exchange_protection([])
      and startup_prot.state["halted"])

# Keepalive must retry after one minute, not wait another 30-minute cycle.
class _FakeStopEvent:
    def __init__(self):
        self.calls = 0
        self.stopped = False
    def wait(self, _seconds):
        self.calls += 1
        if self.calls >= 3:
            self.stopped = True
            return True
        return False
    def is_set(self):
        return self.stopped

keepalive_calls = []
keep = binance_user_ws.BinanceUserDataWS(
    "listen", lambda: "listen", lambda _: None, lambda _: None,
)
def renewal_sequence():
    keepalive_calls.append(1)
    if len(keepalive_calls) == 1:
        raise RuntimeError("temporary")
    return "listen"
keep.renew = renewal_sequence
keep._stop = _FakeStopEvent()
keep._keepalive_loop()
check("listenKey keepalive retries before expiry", len(keepalive_calls) == 2)

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
ws_mock = binance_ws.BinanceWS(["BTCUSDT"], lambda _: None)
ws_mock.prices["BTCUSDT"] = 50000.0
ws_mock.mark_prices["BTCUSDT"] = 49998.0
check("WS uses routed market endpoint", "/market/stream?streams=" in binance_ws.URL)
check("WS exposes mark price snapshot",
      ws_mock.mark_snapshot().get("BTCUSDT") == 49998.0)
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

# --- 8. signal/bar and kill-switch wiring ---
class _SignalEngine:
    def __init__(self):
        self.calls = 0

    def open(self, *args, **kwargs):
        self.calls += 1
        return ({"id": 1, "entry": 100.0, "sl": 99.0, "tp": 101.0}, "ok")

old_signal = binance_bot.strategy.scalp_signal
old_bot_log = binance_bot.log
signal_engine = _SignalEngine()
signal_st = {"positions": [], "cooldown_until": 0, "signal_bars": {}}
binance_bot.strategy.scalp_signal = lambda *args: ("long", {"mock": True})
binance_bot.log = lambda _: None
try:
    c5_mock = [{"o": 100.0, "h": 100.1, "l": 99.9, "c": 100.0,
                "ts": i * 300000} for i in range(45)]
    c5_mock.append({"o": 100.0, "h": 100.2, "l": 99.9, "c": 100.1,
                    "ts": 45 * 300000})
    c15_mock = [{"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0,
                 "ts": i * 900000} for i in range(35)]
    first_signal = binance_bot.manage_scalp(
        signal_engine, signal_st, "BTCUSDT", 100.0, c5_mock, c15_mock
    )
    second_signal = binance_bot.manage_scalp(
        signal_engine, signal_st, "BTCUSDT", 100.0, c5_mock, c15_mock
    )
    check("scalp signal is consumed once per closed bar",
          first_signal is True and second_signal is False
          and signal_engine.calls == 1)
finally:
    binance_bot.strategy.scalp_signal = old_signal
    binance_bot.log = old_bot_log

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


# --- CCXT warning khong duoc trip circuit (bug 06/10/2026) ---
from binance_safety import is_rate_limit_failure
ccxt_warn = 'binanceusdm fetchOpenOrders() WARNING: fetching open orders without specifying a symbol has stricter rate limits'
check("CCXT warning khong trip circuit",
      is_rate_limit_failure(status_code=None, api_code=None, body=ccxt_warn) == False)
check("429 that van trip",
      is_rate_limit_failure(status_code=429, api_code=None, body="") == True)
check("418 that van trip",
      is_rate_limit_failure(status_code=418, api_code=None, body="") == True)
check("-1003 van trip",
      is_rate_limit_failure(status_code=None, api_code=-1003, body="") == True)
check("ban message that van trip",
      is_rate_limit_failure(status_code=None, api_code=None,
                            body="Way too many requests; IP banned until 123") == True)

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
