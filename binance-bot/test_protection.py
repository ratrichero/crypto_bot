"""Offline scenario tests for live position/protection lifecycle.

A small in-memory Binance USD-M (Hedge Mode) simulator drives the real
BinanceEngine code paths: market orders, Algo (conditional) orders that the
exchange can trigger on its own, positionRisk, userTrades and order lookup.
No network, no keys.  Run: python3 test_protection.py
"""
import copy
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
for _k in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
    os.environ.pop(_k, None)

_CFG_P = os.path.join(BASE, "config.json")
_UNI_P = os.path.join(BASE, "universe.json")
_MADE_CFG = not os.path.exists(_CFG_P)
_MADE_UNI = not os.path.exists(_UNI_P)
if _MADE_CFG:
    shutil.copy(os.path.join(BASE, "config.example.json"), _CFG_P)
if _MADE_UNI:
    json.dump([{"symbol": "BTCUSDT", "quoteVolume": 1e9}], open(_UNI_P, "w"))

import live_binance  # noqa: E402
import binance_bot  # noqa: E402

BOT_TRADES, BOT_LOGS = [], []
binance_bot.record_trade = BOT_TRADES.append
binance_bot.log = BOT_LOGS.append

CFG = json.load(open(os.path.join(BASE, "config.example.json")))
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (" | " + str(extra) if extra and not cond else ""))


class Clock:
    """Deterministic wall clock shared by the engine and the fake exchange."""

    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds or 0))


CLOCK = Clock()
live_binance.time.time = CLOCK.time
live_binance.time.sleep = CLOCK.sleep


class BinanceError(Exception):
    pass


class FakeBinance:
    """Hedge-mode USD-M simulator (one aggregate LONG/SHORT per symbol)."""

    def __init__(self, prices=None):
        self.prices = dict(prices or {"BTCUSDT": 60000.0})
        self.positions = {}          # (symbol, "long"/"short") -> qty
        self.algos = {}              # algoId -> raw algo dict
        self.orders = {}             # orderId -> ccxt-like order
        self.trades = []             # ccxt-like userTrades
        self.next_id = 5000
        self.post_failures = []      # queued exceptions for POST algoOrder
        self.positions_override = None
        self.calls = []

    # ---------------------------------------------------------- helpers
    def _id(self):
        self.next_id += 1
        return self.next_id

    def _fill(self, symbol, side, position_side, qty, price=None,
              reduce_check=True):
        """Execute one MARKET fill and update the hedge positions."""
        price = float(price or self.prices[symbol])
        key = (symbol, position_side.lower())
        closing = ((position_side == "LONG" and side == "sell")
                   or (position_side == "SHORT" and side == "buy"))
        if closing:
            have = self.positions.get(key, 0.0)
            if reduce_check and qty > have + 1e-12:
                raise BinanceError('binance {"code":-2022,"msg":'
                                   '"ReduceOnly Order is rejected."}')
            self.positions[key] = round(have - qty, 10)
        else:
            self.positions[key] = round(self.positions.get(key, 0.0) + qty, 10)
        oid = self._id()
        order = {"id": str(oid), "orderId": oid, "status": "closed",
                 "filled": qty, "amount": qty, "average": price,
                 "symbol": symbol, "side": side,
                 "info": {"positionSide": position_side}}
        self.orders[str(oid)] = order
        self.trades.append({
            "id": str(self._id()), "order": str(oid), "symbol": symbol,
            "side": side, "price": price, "amount": qty,
            "timestamp": int(CLOCK.now * 1000),
            "info": {"positionSide": position_side, "orderId": str(oid)},
        })
        return order

    def fire(self, algo_id, price=None):
        """The exchange triggers an Algo order on its own (TP/SL hit)."""
        algo = self.algos[int(algo_id)]
        assert algo["algoStatus"] == "NEW", algo
        side = algo["side"].lower()
        try:
            order = self._fill(algo["symbol"], side, algo["positionSide"],
                               float(algo["quantity"]), price)
        except BinanceError:
            algo["algoStatus"] = "REJECTED"
            return None
        algo["algoStatus"] = "FINISHED"
        algo["actualOrderId"] = str(order["orderId"])
        algo["actualPrice"] = "0.00000"
        algo["updateTime"] = int(CLOCK.now * 1000)
        return order

    def open_algos(self, symbol=None):
        return [a for a in self.algos.values() if a["algoStatus"] == "NEW"
                and (symbol is None or a["symbol"] == symbol)]

    # ------------------------------------------------------ ccxt surface
    def fetch_positions(self, *args, **kwargs):
        self.calls.append("fetch_positions")
        if self.positions_override is not None:
            return copy.deepcopy(self.positions_override)
        return [{"symbol": s, "contracts": q,
                 "info": {"symbol": s, "positionSide": side.upper()}}
                for (s, side), q in self.positions.items() if q]

    def create_market_buy_order(self, symbol, qty, params=None):
        return self._fill(symbol, "buy", (params or {})["positionSide"], qty)

    def create_market_sell_order(self, symbol, qty, params=None):
        return self._fill(symbol, "sell", (params or {})["positionSide"], qty)

    def fetch_open_orders(self, symbol=None, since=None, limit=None,
                          params=None):
        self.calls.append("fetch_open_orders")
        return []

    def fetch_order(self, order_id, symbol=None, params=None):
        self.calls.append("fetch_order")
        return copy.deepcopy(self.orders[str(order_id)])

    def fetch_my_trades(self, symbol=None, since=None, limit=None,
                        params=None):
        self.calls.append("fetch_my_trades")
        rows = [t for t in self.trades if t["symbol"] == symbol]
        if since is not None:
            rows = [t for t in rows if t["timestamp"] >= since]
        return copy.deepcopy(rows[-(limit or 500):])

    def fapiPrivatePostAlgoOrder(self, params):
        self.calls.append("post_algo")
        if self.post_failures:
            exc = self.post_failures.pop(0)
            if exc is not None:
                raise exc
        aid = self._id()
        self.algos[aid] = {
            "algoId": aid, "clientAlgoId": params["clientAlgoId"],
            "algoType": "CONDITIONAL", "orderType": params["type"],
            "symbol": params["symbol"], "side": params["side"],
            "positionSide": params["positionSide"],
            "quantity": str(params["quantity"]),
            "triggerPrice": str(params["triggerPrice"]),
            "algoStatus": "NEW", "actualOrderId": "", "actualPrice": "0",
            "createTime": int(CLOCK.now * 1000),
        }
        return {"algoId": aid, "clientAlgoId": params["clientAlgoId"]}

    def fapiPrivateDeleteAlgoOrder(self, params):
        self.calls.append("delete_algo")
        algo = None
        if params.get("algoId") is not None:
            algo = self.algos.get(int(params["algoId"]))
        else:
            for candidate in self.algos.values():
                if candidate["clientAlgoId"] == params.get("clientAlgoId"):
                    algo = candidate
        if algo is None or algo["algoStatus"] != "NEW":
            raise BinanceError('binance {"code":-2011,'
                               '"msg":"Unknown order sent."}')
        algo["algoStatus"] = "CANCELED"
        return {"algoId": algo["algoId"], "code": "200"}

    def fapiPrivateGetOpenAlgoOrders(self, params=None):
        self.calls.append("open_algos")
        symbol = (params or {}).get("symbol")
        return copy.deepcopy(self.open_algos(symbol))

    def fapiPrivateGetAlgoOrder(self, params):
        self.calls.append("get_algo")
        for algo in self.algos.values():
            if (str(algo["algoId"]) == str(params.get("algoId"))
                    or algo["clientAlgoId"] == params.get("clientAlgoId")):
                return copy.deepcopy(algo)
        raise BinanceError('binance {"code":-2013,'
                           '"msg":"Order does not exist."}')


def make_engine(fake=None, protection=True, **cfg_overrides):
    cfg = copy.deepcopy(CFG)
    cfg["exchange_protection"] = protection
    cfg.update(cfg_overrides)
    st = {"equity": 1000.0, "positions": [],
          "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0},
          "_pid": 0}
    logs = []
    eng = live_binance.BinanceEngine(cfg, st, dry_run=True,
                                     log=logs.append)
    eng.dry_run = False
    eng.ex = fake or FakeBinance()
    eng._private_call = (lambda endpoint, fn, *a, **k:
                         (k.pop("_weight", None), fn(*a, **k))[1])
    eng._filters_for = lambda symbol: (0.001, 0.001, 5.0)
    eng._ccxt_symbol = lambda symbol: symbol
    eng._set_leverage = lambda symbol: None
    eng.get_balance_usdt = lambda: {"free": 1e9, "total": 1e9}
    eng.refresh_equity = lambda: None
    eng.db_rows = []
    eng._db_insert_trade = lambda rec: (eng.db_rows.append(dict(rec)), True)[1]
    eng.logs = logs
    return eng, st


def open_lot(eng, side, price, tag="grid", level=None, sl_pct=0.03,
             tp_pct=0.005, notional=600.0):
    eng.ex.prices["BTCUSDT"] = price
    pos, why = eng.open("BTCUSDT", side, notional, price, sl_pct, tp_pct, tag,
                        level=level)
    assert pos is not None, why
    CLOCK.sleep(1)
    return pos


# ===================================================================
# 1. close(): verification must account for sibling grid lots
# ===================================================================
def test_close_one_of_many_grid_lots():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000, level="b1")
    open_lot(eng, "long", 59700, level="b2")
    fake.prices["BTCUSDT"] = 60300
    rec = eng.close(a, 60300, "TP")
    check("close 1/2 grid lots: tra ve record", rec is not None,
          eng.logs[-3:])
    check("close 1/2 grid lots: khong halt", not st.get("halted"),
          st.get("halt_reason"))
    check("close 1/2 grid lots: state con dung 1 lot",
          [p["level"] for p in st["positions"]] == ["b2"])
    check("close 1/2 grid lots: san con dung qty lot con lai",
          abs(fake.positions[("BTCUSDT", "long")] - 0.01) < 1e-9,
          fake.positions)


def test_close_detects_real_partial():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000, level="b1")
    open_lot(eng, "long", 59700, level="b2")
    real_fill = fake._fill

    def half_fill(symbol, side, position_side, qty, price=None, **kw):
        return real_fill(symbol, side, position_side, qty / 2, price, **kw)
    fake._fill = half_fill
    rec = eng.close(a, 60300, "TP")
    fake._fill = real_fill
    check("close partial that van bi chan", rec is None)
    check("close partial that -> halt", st.get("halted") is True)


def test_cancel_absent_guard_compares_aggregate():
    """A guard id that is already absent must not fail the close merely
    because another lot shares the same symbol/side."""
    fake = FakeBinance()
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000, level="b1")
    open_lot(eng, "long", 59700, level="b2")
    # Operator cancelled the TP by hand; the lot itself is untouched.
    fake.fapiPrivateDeleteAlgoOrder({"algoId": a["tp_algo_id"]})
    rec = eng.close(a, 60100, "GRID_BASKET_STOP")
    check("guard vang + 2 lot cung chieu: close van chay",
          rec is not None, (st.get("halt_reason"), eng.logs[-4:]))


# ===================================================================
# 2. detect_exchange_closed: fail-closed confirmation
# ===================================================================
def detect_round(eng, marks=None, advance=11):
    CLOCK.sleep(advance)
    return eng.detect_exchange_closed(marks or {"BTCUSDT": 60000})


def manual_close(fake, side, qty, price):
    fake.prices["BTCUSDT"] = price
    ps = "LONG" if side == "long" else "SHORT"
    fake._fill("BTCUSDT", "sell" if side == "long" else "buy", ps, qty, price)


def test_detect_ignores_single_empty_read():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    open_lot(eng, "long", 60000, level="b1")
    CLOCK.sleep(300)
    fake.positions_override = []          # one glitchy empty positionRisk
    first = detect_round(eng)
    fake.positions_override = None        # next read is healthy again
    second = detect_round(eng)
    third = detect_round(eng)
    check("detect: 1 lan doc rong khong xoa lot",
          first == [] and second == [] and third == [])
    check("detect: lot van con trong state", len(st["positions"]) == 1)


def test_detect_skips_fresh_lot():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    open_lot(eng, "long", 60000, level="b1")
    fake.positions_override = []          # positionRisk lag right after fill
    recs = detect_round(eng, advance=5) + detect_round(eng, advance=11)
    check("detect: lot moi mo (<60s) khong bi coi la da dong", recs == [])
    check("detect: lot moi mo van con", len(st["positions"]) == 1)


def test_detect_confirms_real_external_close():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    lot = open_lot(eng, "long", 60000, level="b1")
    CLOCK.sleep(300)
    manual_close(fake, "long", lot["qty"], 60450)
    first = detect_round(eng)
    second = detect_round(eng)
    check("detect: lan quet 1 chi danh dau cho xac nhan", first == [])
    check("detect: lan quet 2 ghi nhan dong", len(second) == 1, second)
    check("detect: lot bi xoa sau xac nhan", st["positions"] == [])


# ===================================================================
# 3. detect_exchange_closed: real exit price from the right fill
# ===================================================================
def confirm_detect(eng, marks=None):
    return detect_round(eng, marks) + detect_round(eng, marks)


def test_detect_price_ignores_opposite_leg_and_old_fills():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    # An old long from yesterday closed at 50000 (must be ignored).
    fake._fill("BTCUSDT", "buy", "LONG", 0.01, 51000)
    fake._fill("BTCUSDT", "sell", "LONG", 0.01, 50000)
    CLOCK.sleep(3600)
    lot = open_lot(eng, "long", 60000, level="b1")
    CLOCK.sleep(300)
    manual_close(fake, "long", lot["qty"], 59700)       # real SL fill
    CLOCK.sleep(1)
    open_lot(eng, "short", 61200, level="s1")           # SELL opens SHORT
    recs = confirm_detect(eng, {"BTCUSDT": 61000})
    rec = recs[0] if recs else {}
    check("gia that: bo qua SELL mo SHORT va fill cu",
          rec.get("exit") == 59700, rec)
    check("gia that: danh dau khong uoc tinh", rec.get("estimated") is False,
          rec)


def test_detect_price_grid_lots_get_their_own_fill():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    a = open_lot(eng, "long", 60000, level="b1")
    b = open_lot(eng, "long", 59700, level="b2", notional=1200.0)
    CLOCK.sleep(300)
    manual_close(fake, "long", a["qty"], 60300)
    CLOCK.sleep(5)
    manual_close(fake, "long", b["qty"], 60010)
    recs = {r["id"]: r for r in confirm_detect(eng)}
    check("grid: moi lot lay dung fill cua minh",
          recs.get(a["id"], {}).get("exit") == 60300
          and recs.get(b["id"], {}).get("exit") == 60010, recs)


def test_detect_price_one_order_closing_whole_leg():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    a = open_lot(eng, "long", 60000, level="b1")
    b = open_lot(eng, "long", 59700, level="b2")
    CLOCK.sleep(300)
    manual_close(fake, "long", a["qty"] + b["qty"], 59900)   # close-all
    recs = confirm_detect(eng)
    check("close-all 1 lenh: ca 2 lot dung gia lenh do",
          len(recs) == 2 and all(r["exit"] == 59900 and not r["estimated"]
                                 for r in recs), recs)


# ===================================================================
# 4. detect_exchange_closed: leftover guards are cancelled
# ===================================================================
def test_detect_cancels_leftover_guards():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=True)
    lot = open_lot(eng, "long", 60000, level="b1")
    check("guard: lot co du SL+TP tren san", len(fake.open_algos()) == 2)
    CLOCK.sleep(300)
    manual_close(fake, "long", lot["qty"], 60100)      # closed by hand
    recs = confirm_detect(eng)
    check("detect + protection: ghi nhan dong", len(recs) == 1)
    check("detect + protection: khong con algo treo", fake.open_algos() == [],
          fake.open_algos())


# ===================================================================
# 5. detect_exchange_closed: reason drives the scalp SL cooldown
# ===================================================================
def test_detect_reason_from_real_fill():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    lot = open_lot(eng, "long", 60000, tag="scalp", sl_pct=0.004,
                   tp_pct=0.01)
    CLOCK.sleep(300)
    manual_close(fake, "long", lot["qty"], lot["sl"] - 5)   # SL slipped
    recs = confirm_detect(eng)
    check("reason: fill that qua SL -> 'SL' (kich hoat cooldown scalp)",
          recs and recs[0]["reason"] == "SL", recs)
    check("log: fill that khong ghi [estimated]",
          any("[exchange fill]" in m for m in eng.logs)
          and not any("[estimated]" in m for m in eng.logs))


def test_detect_reason_unknown_when_estimated():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    open_lot(eng, "long", 60000, tag="scalp", sl_pct=0.004, tp_pct=0.01)
    CLOCK.sleep(300)
    fake.positions.clear()                # gone, but no trade visible
    recs = confirm_detect(eng)
    check("reason: gia uoc tinh -> CLOSED_ON_EXCHANGE",
          recs and recs[0]["reason"] == "CLOSED_ON_EXCHANGE"
          and recs[0]["estimated"] is True, recs)


# ===================================================================
# 6. Algo lifecycle: Binance closes a lot via its own TP/SL
# ===================================================================
def sync(eng, advance=11):
    CLOCK.sleep(advance)
    return eng.sync_exchange_protection()


def test_sync_books_exchange_tp_with_real_fill():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000, level="b1")
    b = open_lot(eng, "long", 59700, level="b2")
    fake.fire(a["tp_algo_id"], price=60305.5)          # Binance TP fills
    recs = sync(eng)
    rec = recs[0] if recs else {}
    check("sync: TP khop tren san duoc ghi nhan", len(recs) == 1, eng.logs[-5:])
    check("sync: dung lot, reason TP", rec.get("id") == a["id"]
          and rec.get("reason") == "TP", rec)
    check("sync: gia = fill that cua lenh TP", rec.get("exit") == 60305.5
          and rec.get("estimated") is False, rec)
    check("sync: PnL ghi vao DB", eng.db_rows and eng.db_rows[-1]["id"] == a["id"])
    check("sync: SL anh em cua lot da dong bi huy",
          fake.algos[a["sl_algo_id"]]["algoStatus"] == "CANCELED")
    check("sync: guard cua lot con lai giu nguyen",
          fake.algos[b["sl_algo_id"]]["algoStatus"] == "NEW"
          and fake.algos[b["tp_algo_id"]]["algoStatus"] == "NEW")
    check("sync: state chi con lot b2",
          [p["id"] for p in st["positions"]] == [b["id"]])
    check("sync: reconcile khop sau khi ghi nhan",
          eng.reconcile_positions(force=True) and not st.get("halted"),
          st.get("halt_reason"))


def test_sync_uses_ws_algo_event():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "short", 60000, tag="scalp", sl_pct=0.004,
                   tp_pct=0.01)
    order = fake.fire(lot["sl_algo_id"], price=60252.0)
    eng.on_user_event({"e": "ALGO_UPDATE", "o": {
        "s": "BTCUSDT", "aid": lot["sl_algo_id"], "X": "FINISHED",
        "ai": str(order["orderId"]), "ap": "60252.0",
        "aq": str(lot["qty"]), "ps": "SHORT"}})
    before = list(fake.calls)
    recs = eng.sync_exchange_protection()        # due immediately via WS
    new_calls = fake.calls[len(before):]
    check("ws: ALGO_UPDATE kich hoat sync ngay", len(recs) == 1, recs)
    check("ws: reason SL + gia ap tu event",
          recs and recs[0]["reason"] == "SL" and recs[0]["exit"] == 60252.0,
          recs)
    check("ws: khong can query REST algo/order",
          "get_algo" not in new_calls and "fetch_order" not in new_calls,
          new_calls)


def test_sync_guard_cancelled_keeps_lot_and_rearms():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.fapiPrivateDeleteAlgoOrder({"algoId": lot["sl_algo_id"]})  # by hand
    recs = sync(eng)
    check("guard bi huy: khong ghi dong lot", recs == [] and
          len(st["positions"]) == 1)
    check("guard bi huy: lot chuyen sang dat lai",
          st["positions"][0]["protection_status"] == "retrying"
          and st["positions"][0]["sl_algo_id"] is None)


def test_sync_unknown_status_changes_nothing():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.fire(lot["tp_algo_id"])

    def broken(params):
        raise BinanceError("timeout")
    fake.fapiPrivateGetAlgoOrder = broken
    recs = sync(eng)
    check("query loi: khong doan, khong xoa lot", recs == []
          and len(st["positions"]) == 1
          and st["positions"][0]["protection_status"] == "armed")


# ===================================================================
# 7. No race between the bot's local exit and the exchange guard
# ===================================================================
def bot_state(st):
    st.setdefault("grids", {})
    st.setdefault("cooldown_until", 0)
    return st


def market_orders(fake):
    return [o for o in fake.orders.values()]


def test_local_exit_defers_to_armed_guard():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    bot_state(st)
    lot = open_lot(eng, "long", 60000, level="b1")
    n_orders = len(market_orders(fake))
    fake.fire(lot["tp_algo_id"], price=60301)        # Binance fires first
    changed = binance_bot.update_positions(eng, st, "BTCUSDT", 60310)
    check("race: bot khong tu dong khi guard armed", not changed
          and len(market_orders(fake)) == n_orders + 1)   # only the TP fill
    recs = eng.sync_exchange_protection()            # due soon after defer
    if not recs:
        recs = sync(eng, advance=2)
    check("race: sync ghi TP tu fill san", len(recs) == 1
          and recs[0]["exit"] == 60301, recs)
    check("race: khong halt", not st.get("halted"), st.get("halt_reason"))


def test_local_exit_fallback_after_grace():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    bot_state(st)
    lot = open_lot(eng, "long", 60000, level="b1")
    binance_bot.update_positions(eng, st, "BTCUSDT", 60310)
    CLOCK.sleep(16)                                  # guard never fired
    changed = binance_bot.update_positions(eng, st, "BTCUSDT", 60310)
    check("grace het: bot tu dong lenh", changed and st["positions"] == [],
          eng.logs[-4:])
    check("grace het: guard cua lot da duoc huy truoc khi dong",
          all(fake.algos[lot[k]]["algoStatus"] == "CANCELED"
              for k in ("sl_algo_id", "tp_algo_id")))


def test_close_after_guard_filled_books_exchange_fill():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    bot_state(st)
    lot = open_lot(eng, "long", 60000, level="b1")
    open_lot(eng, "long", 59700, level="b2")
    fake.fire(lot["tp_algo_id"], price=60302)
    n_orders = len(market_orders(fake))
    rec = eng.close(lot, 60310, "GRID_BASKET_STOP")
    check("close sau khi TP da khop: ghi nhan fill san",
          rec is not None and rec["reason"] == "TP" and rec["exit"] == 60302,
          (rec, st.get("halt_reason")))
    check("close sau khi TP da khop: khong gui them lenh market",
          len(market_orders(fake)) == n_orders)
    check("close sau khi TP da khop: khong halt", not st.get("halted"),
          st.get("halt_reason"))


def test_close_while_guard_in_flight_does_not_halt():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.algos[lot["tp_algo_id"]]["algoStatus"] = "TRIGGERED"
    rec = eng.close(lot, 60310, "TP")
    check("guard dang khop: close tam hoan, khong halt",
          rec is None and not st.get("halted"), st.get("halt_reason"))


def test_restart_after_offline_tp_books_pnl():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.fire(lot["tp_algo_id"], price=60300)        # bot was down
    eng2, st2 = make_engine(fake)
    st2["positions"] = copy.deepcopy(st["positions"])
    eng2._fetch_open_algo_orders = lambda symbol=None: fake.open_algos(symbol)
    eng2._reconcile_startup()
    check("restart: lot khong bi xoa am tham", len(st2["positions"]) == 1)
    recs = eng2.sync_exchange_protection()
    check("restart: sync ghi PnL TP da khop luc offline",
          len(recs) == 1 and recs[0]["reason"] == "TP"
          and eng2.db_rows, recs)
    ok = eng2.reconcile_positions(force=True)
    check("restart: reconcile tu het halt sau khi ghi nhan",
          ok and not st2.get("halted"), st2.get("halt_reason"))


# ===================================================================
# 8. Opening: every lot ends up with exactly one SL and one TP
# ===================================================================
def guards_of(fake, lot_id_pos):
    cids = {lot_id_pos.get("sl_client_algo_id"),
            lot_id_pos.get("tp_client_algo_id")}
    return [a for a in fake.open_algos() if a["clientAlgoId"] in cids]


def retry(eng, advance):
    CLOCK.sleep(advance)
    return eng.retry_protection()


def test_tp_failure_keeps_sl_and_rearms_only_tp():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    fake.post_failures = [None, BinanceError("binance -1001 internal error")]
    lot = open_lot(eng, "long", 60000, level="b1")
    check("TP loi: SL da dat van duoc giu tren san",
          lot["sl_algo_id"] and fake.algos[lot["sl_algo_id"]]["algoStatus"]
          == "NEW", fake.algos)
    check("TP loi: lot o trang thai retrying, khong co deadline dong",
          lot["protection_status"] == "retrying"
          and "protection_deadline" not in lot)
    sl_before = lot["sl_algo_id"]
    retry(eng, 31)
    check("TP loi: retry chi dat TP, giu nguyen SL",
          lot["protection_status"] == "armed" and lot["sl_algo_id"] == sl_before
          and lot["tp_algo_id"])
    check("TP loi: tren san dung 1 SL + 1 TP", len(fake.open_algos()) == 2,
          fake.open_algos())


def test_sl_failure_closes_after_deadline():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    fake.post_failures = [BinanceError("binance -1001 internal error")] * 50
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.post_failures = [BinanceError("binance -1001 internal error")] * 50
    check("SL loi: co deadline", "protection_deadline" in lot)
    for _ in range(13):
        retry(eng, 10)
    recs = eng.drain_close_records()
    check("SL loi qua deadline: dong lot va tra record de ghi JSONL/DB",
          st["positions"] == [] and len(recs) == 1
          and recs[0]["reason"] == "PROTECTION_FAILED", (recs, eng.logs[-3:]))


def test_only_tp_missing_never_force_closes():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.fapiPrivateDeleteAlgoOrder({"algoId": lot["tp_algo_id"]})
    sync(eng)                                    # guard reported lost
    fake.post_failures = [BinanceError("-2021 would immediately trigger")] * 50
    for _ in range(10):
        retry(eng, 31)
    check("chi thieu TP: khong ep dong lot", len(st["positions"]) == 1)
    check("chi thieu TP: SL van tren san",
          fake.algos[lot["sl_algo_id"]]["algoStatus"] == "NEW")


def test_lost_guard_rearmed_without_duplicates():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.algos[lot["sl_algo_id"]]["algoStatus"] = "EXPIRED"   # system cancel
    sync(eng)
    eng.retry_protection()
    check("guard het han: dat lai dung chan SL",
          lot["protection_status"] == "armed" and lot["sl_algo_id"]
          and fake.algos[lot["sl_algo_id"]]["algoStatus"] == "NEW")
    check("guard het han: tren san dung 2 guard cho lot",
          len(fake.open_algos()) == 2, fake.open_algos())


def test_ambiguous_post_is_adopted_not_duplicated():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    real_post = fake.fapiPrivatePostAlgoOrder
    state = {"n": 0}

    def landed_then_timeout(params):
        state["n"] += 1
        result = real_post(params)
        if state["n"] == 2:
            raise BinanceError("RequestTimeout: timed out")
        return result
    fake.fapiPrivatePostAlgoOrder = landed_then_timeout
    lot = open_lot(eng, "long", 60000, level="b1")
    check("POST mo ho: nhan lai guard theo clientAlgoId",
          lot["protection_status"] == "armed" and lot["tp_algo_id"]
          and len(fake.open_algos()) == 2, (lot, fake.open_algos()))


# ===================================================================
# 9. Orphan conditional orders
# ===================================================================
def add_algo(fake, cid, side="SELL", position_side="LONG",
             order_type="STOP_MARKET", qty="0.01", symbol="BTCUSDT"):
    return fake.fapiPrivatePostAlgoOrder({
        "clientAlgoId": cid, "type": order_type, "symbol": symbol,
        "side": side, "positionSide": position_side, "quantity": qty,
        "triggerPrice": "50000"})["algoId"]


def test_orphans_cancelled_on_grid_symbol_with_live_lots():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    orphan_sl = add_algo(fake, "bBTCUSDTL900")                 # old lot
    orphan_tp = add_algo(fake, "bBTCUSDTS901", side="BUY",
                         position_side="SHORT",
                         order_type="TAKE_PROFIT_MARKET")      # flat leg
    cleaned = eng.cleanup_orphan_orders()
    check("mo coi: huy ca khi symbol con lot grid",
          cleaned == 2 and fake.algos[orphan_sl]["algoStatus"] == "CANCELED"
          and fake.algos[orphan_tp]["algoStatus"] == "CANCELED",
          (cleaned, eng.logs[-4:]))
    check("mo coi: guard cua lot dang song khong bi dung",
          fake.algos[lot["sl_algo_id"]]["algoStatus"] == "NEW"
          and fake.algos[lot["tp_algo_id"]]["algoStatus"] == "NEW")


def test_orphans_keep_foreign_and_unmanaged_leg():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    manual = add_algo(fake, "web_manual_123")                  # user's own
    fake.positions[("BTCUSDT", "short")] = 0.02                # unmanaged
    bot_on_unmanaged = add_algo(fake, "bBTCUSDTS902", side="BUY",
                                position_side="SHORT")
    cleaned = eng.cleanup_orphan_orders()
    check("mo coi: khong huy lenh khong do bot tao",
          fake.algos[manual]["algoStatus"] == "NEW")
    check("mo coi: khong huy khi leg co vi the bot khong quan ly",
          cleaned == 0
          and fake.algos[bot_on_unmanaged]["algoStatus"] == "NEW")


def test_orphan_after_exchange_tp_never_hits_new_lot():
    """End-to-end: TP fires, a new lot opens on the same leg; the old SL
    must already be gone so it can never close the new lot."""
    fake = FakeBinance()
    eng, st = make_engine(fake)
    old = open_lot(eng, "long", 60000, level="b1")
    fake.fire(old["tp_algo_id"], price=60300)
    sync(eng)
    new = open_lot(eng, "long", 60100, level="b1")
    old_sl = fake.algos[old["sl_algo_id"]]
    check("e2e: SL cu da bi huy truoc khi mo lot moi",
          old_sl["algoStatus"] == "CANCELED", old_sl)
    check("e2e: tren san chi con guard cua lot moi",
          {a["algoId"] for a in fake.open_algos()}
          == {new["sl_algo_id"], new["tp_algo_id"]})


def test_startup_orphans_cancelled_instead_of_halt():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    add_algo(fake, "bBTCUSDTL903")                             # stale orphan
    eng2, st2 = make_engine(fake)
    st2["positions"] = copy.deepcopy(st["positions"])
    eng2._reconcile_startup()
    check("startup: mo coi bi huy, bot khong halt",
          not st2.get("halted") and len(fake.open_algos()) == 2,
          (st2.get("halt_reason"), fake.open_algos(), eng2.logs[-5:]))


TESTS = [
    test_close_one_of_many_grid_lots,
    test_close_detects_real_partial,
    test_cancel_absent_guard_compares_aggregate,
    test_detect_ignores_single_empty_read,
    test_detect_skips_fresh_lot,
    test_detect_confirms_real_external_close,
    test_detect_price_ignores_opposite_leg_and_old_fills,
    test_detect_price_grid_lots_get_their_own_fill,
    test_detect_price_one_order_closing_whole_leg,
    test_detect_cancels_leftover_guards,
    test_detect_reason_from_real_fill,
    test_detect_reason_unknown_when_estimated,
    test_sync_books_exchange_tp_with_real_fill,
    test_sync_uses_ws_algo_event,
    test_sync_guard_cancelled_keeps_lot_and_rearms,
    test_sync_unknown_status_changes_nothing,
    test_local_exit_defers_to_armed_guard,
    test_local_exit_fallback_after_grace,
    test_close_after_guard_filled_books_exchange_fill,
    test_close_while_guard_in_flight_does_not_halt,
    test_restart_after_offline_tp_books_pnl,
    test_tp_failure_keeps_sl_and_rearms_only_tp,
    test_sl_failure_closes_after_deadline,
    test_only_tp_missing_never_force_closes,
    test_lost_guard_rearmed_without_duplicates,
    test_ambiguous_post_is_adopted_not_duplicated,
    test_orphans_cancelled_on_grid_symbol_with_live_lots,
    test_orphans_keep_foreign_and_unmanaged_leg,
    test_orphan_after_exchange_tp_never_hits_new_lot,
    test_startup_orphans_cancelled_instead_of_halt,
]


def main():
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # pragma: no cover - surfaced as failure
            import traceback
            traceback.print_exc()
            check(test.__name__ + " (exception)", False, exc)
    if _MADE_CFG and os.path.exists(_CFG_P):
        os.remove(_CFG_P)
    if _MADE_UNI and os.path.exists(_UNI_P):
        os.remove(_UNI_P)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
