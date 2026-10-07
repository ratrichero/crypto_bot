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
        self.fee_rate = 0.0004       # commission THAT (khac cfg fee_rate)
        self.fee_asset = "USDT"
        self.trades_error = None     # exception cho fetch_my_trades

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
            "fee": {"cost": qty * price * self.fee_rate,
                    "currency": self.fee_asset},
            "info": {"positionSide": position_side, "orderId": str(oid),
                     "commission": str(qty * price * self.fee_rate),
                     "commissionAsset": self.fee_asset},
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

    def _market(self, symbol, side, qty, params):
        params = params or {}
        mode = getattr(self, "timeout_mode", None)   # None|accepted|lost
        self.timeout_mode = None
        if mode == "lost":
            raise BinanceError("binance RequestTimeout: timed out")
        order = self._fill(symbol, side, params["positionSide"], qty)
        order["clientOrderId"] = params.get("newClientOrderId")
        if mode == "accepted":
            # San da nhan + khop, nhung chua tra cuu duoc ngay (do tre).
            order["_hidden_until"] = CLOCK.now + 20
            raise BinanceError("binance RequestTimeout: timed out")
        return order

    def create_market_buy_order(self, symbol, qty, params=None):
        return self._market(symbol, "buy", qty, params)

    def create_market_sell_order(self, symbol, qty, params=None):
        return self._market(symbol, "sell", qty, params)

    def fapiPrivateGetOrder(self, params):
        self.calls.append("get_order")
        for o in self.orders.values():
            if (o.get("clientOrderId") == params.get("origClientOrderId")
                    and CLOCK.now >= o.get("_hidden_until", 0)):
                return {"orderId": o["orderId"], "status": "FILLED",
                        "executedQty": str(o["filled"]),
                        "avgPrice": str(o["average"]),
                        "clientOrderId": o["clientOrderId"],
                        "symbol": o["symbol"], "side": o["side"].upper(),
                        "positionSide": o["info"]["positionSide"]}
        raise BinanceError('binance {"code":-2013,'
                           '"msg":"Order does not exist."}')

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
        if self.trades_error is not None:
            raise self.trades_error
        rows = [t for t in self.trades if t["symbol"] == symbol]
        oid = (params or {}).get("orderId")
        if oid is not None:
            rows = [t for t in rows if t["order"] == str(oid)]
        week = 7 * 24 * 3600 * 1000
        now_ms = int(CLOCK.now * 1000)
        if since is not None:
            # ccxt: since cu hon 7 ngay -> endTime = since + 7d; Binance co
            # startTime -> tra trade CU NHAT tu moc do (limit dau tien).
            end = since + week if now_ms - since >= week else now_ms
            rows = [t for t in rows if since <= t["timestamp"] <= end]
            return copy.deepcopy(rows[:(limit or 500)])
        rows = [t for t in rows if t["timestamp"] >= now_ms - week]
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


def test_startup_lot_without_guard_is_rearmed_not_halted():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.fapiPrivateDeleteAlgoOrder({"algoId": lot["tp_algo_id"]})
    restored = copy.deepcopy(st["positions"])
    restored[0]["tp_algo_id"] = None                  # state lost the id
    restored[0]["tp_client_algo_id"] = None
    eng2, st2 = make_engine(fake)
    st2["positions"] = restored
    eng2._reconcile_startup()
    check("startup thieu guard: khong halt", not st2.get("halted"),
          st2.get("halt_reason"))
    eng2.retry_protection()
    check("startup thieu guard: retry dat lai TP",
          st2["positions"][0]["protection_status"] == "armed"
          and len(fake.open_algos()) == 2, fake.open_algos())


# ===================================================================
# Close dung qty lot (khong de bui) + quet bui cu khi lot cuoi cua leg
# ===================================================================
def test_close_exact_qty_leaves_no_dust():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    st["equity"] = 1e7   # lot lon de tai hien sai so float, tranh exposure_cap
    # 125400 / 60000 = 2.09 dung; dong o 8254.3181: qty_for(2.09*px, px)
    # cu lam tron xuong thanh 2.089 (sai so float) -> bui 0.001.
    a = open_lot(eng, "long", 60000, notional=125400.0, sl_pct=0.9,
                 tp_pct=0.9)
    check("dust: lot mo dung 2.09", a["qty"] == 2.09, a["qty"])
    check("dust: qty_for cu se hut 1 step",
          live_binance.qty_for(2.09 * 8254.3181, 8254.3181, 0.001) == 2.089)
    fake.prices["BTCUSDT"] = 8254.3181
    rec = eng.close(a, 8254.3181, "SL")
    check("dust: close tra record", rec is not None, eng.logs[-3:])
    check("dust: san ve 0, khong con bui",
          fake.positions.get(("BTCUSDT", "long"), 0) == 0, fake.positions)
    # lan 2 tren cung leg: code cu se tich 2 step bui -> reconcile halt
    b = open_lot(eng, "long", 60000, notional=125400.0, sl_pct=0.9,
                 tp_pct=0.9)
    fake.prices["BTCUSDT"] = 8254.3181
    eng.close(b, 8254.3181, "SL")
    ok = eng.reconcile_positions(force=True)
    check("dust: 2 lan dong lien tiep -> reconcile OK, khong halt",
          ok and not st.get("halted"), (st.get("halt_reason"), fake.positions))


def test_close_last_lot_sweeps_old_dust_only():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000)
    fake.positions[("BTCUSDT", "long")] += 0.002   # bui 2 step tu lan cu
    eng.close(a, 60100, "TP")
    check("sweep: lot cuoi dong ca bui <= 2 step",
          fake.positions.get(("BTCUSDT", "long"), 0) == 0, fake.positions)
    check("sweep: khong halt", not st.get("halted"), st.get("halt_reason"))
    b = open_lot(eng, "long", 60000)
    fake.positions[("BTCUSDT", "long")] += 0.5      # vi the tay lon
    eng.close(b, 60100, "TP")
    check("sweep: KHONG dong phan lon (vi the tay)",
          abs(fake.positions.get(("BTCUSDT", "long"), 0) - 0.5) < 1e-9,
          fake.positions)


def test_close_with_sibling_never_sweeps():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000, level="b1")
    open_lot(eng, "long", 59700, level="b2")
    fake.positions[("BTCUSDT", "long")] += 0.001
    fake.calls.clear()
    eng.close(a, 60300, "TP")
    check("sibling: con lot khac -> khong doc leg truoc / khong quet",
          fake.calls.count("fetch_positions") == 1
          and abs(fake.positions[("BTCUSDT", "long")] - 0.011) < 1e-9,
          (fake.calls, fake.positions))


# ===================================================================
# Phi + PnL lay tu userTrades (source of truth) cho moi duong dong
# ===================================================================
def _approx(a, b, eps=1e-6):
    return abs(a - b) < eps


def test_real_fees_bot_close():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    eq0 = st["equity"]
    a = open_lot(eng, "long", 60000)            # qty 0.01
    check("fee: phi mo = commission that (0.04%)",
          _approx(a["fee_entry"], 0.01 * 60000 * 0.0004)
          and "fee_entry_estimated" not in a, a.get("fee_entry"))
    fake.prices["BTCUSDT"] = 60300
    rec = eng.close(a, 60300, "TP")
    fe, fx = 0.24, 0.01 * 60300 * 0.0004
    check("fee: record co phi dong that + phi mo",
          _approx(rec["fee_entry"], fe) and _approx(rec["fee_exit"], fx)
          and rec["fee_estimated"] is False, rec)
    check("fee: pnl = gop - phi mo - phi dong",
          _approx(rec["pnl_gross"], 3.0) and _approx(rec["pnl"], 3.0 - fe - fx),
          rec)
    check("fee: equity/stats khop phi that (khong tru phi mo 2 lan)",
          _approx(st["equity"], eq0 + 3.0 - fe - fx)
          and _approx(st["stats"]["fees"], fe + fx),
          (st["equity"], st["stats"]))
    check("fee: exit_source=bot", rec.get("exit_source") == "bot")


def test_real_fees_exchange_tp():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000, level="b1")
    fake.fire(a["tp_algo_id"], price=60300)
    recs = sync(eng)
    rec = recs[0] if recs else {}
    fx = 0.01 * 60300 * 0.0004
    check("fee: TP san khop -> pnl tru ca 2 phi that",
          len(recs) == 1 and _approx(rec.get("pnl", 0), 3.0 - 0.24 - fx)
          and rec.get("fee_estimated") is False, recs)


def test_fee_fallback_estimated():
    fake = FakeBinance()
    fake.fee_asset = "BNB"
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000)
    check("fee BNB: phi mo danh dau uoc tinh",
          a.get("fee_entry_estimated") is True
          and _approx(a["fee_entry"], 600 * CFG["fee_rate"]), a)
    fake.prices["BTCUSDT"] = 60300
    rec = eng.close(a, 60300, "TP")
    check("fee BNB: record fee_estimated, phi = fee_rate x notional thoat",
          rec["fee_estimated"] is True
          and _approx(rec["fee_exit"], 0.01 * 60300 * CFG["fee_rate"]), rec)
    fake2 = FakeBinance()
    eng2, st2 = make_engine(fake2)
    b = open_lot(eng2, "long", 60000)
    fake2.trades_error = BinanceError("binance -1001 internal error")
    fake2.prices["BTCUSDT"] = 60300
    rec2 = eng2.close(b, 60300, "TP")
    check("fee: userTrades loi khong chan close, danh dau uoc tinh",
          rec2 is not None and rec2["fee_estimated"] is True
          and not st2.get("halted"), (rec2, st2.get("halt_reason")))


def test_real_fees_detect_group_close_prorated():
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    open_lot(eng, "long", 60000, level="b1")
    open_lot(eng, "long", 60000, level="b2")
    CLOCK.sleep(300)
    manual_close(fake, "long", 0.02, 60300)      # 1 lenh dong ca leg
    detect_round(eng, {"BTCUSDT": 60300})
    recs = detect_round(eng, {"BTCUSDT": 60300})
    fx_each = 0.02 * 60300 * 0.0004 / 2
    check("fee detect: 2 lot ghi nhan, phi lenh chung chia theo qty",
          len(recs) == 2 and all(_approx(r["fee_exit"], fx_each)
                                 and r["fee_estimated"] is False
                                 for r in recs), recs)


def test_detect_real_exit_old_lot_and_busy_symbol():
    """Lot mo > 7 ngay va symbol co > 100 trade sau khi mo: van tim duoc
    fill dong that (khong roi ve gia uoc tinh)."""
    fake = FakeBinance()
    eng, st = make_engine(fake, protection=False)
    old = open_lot(eng, "long", 60000, level="b1")
    CLOCK.sleep(8 * 24 * 3600)
    manual_close(fake, "long", 0.01, 60300)
    detect_round(eng, {"BTCUSDT": 60200})
    recs = detect_round(eng, {"BTCUSDT": 60200})
    check("old lot (>7d): gia thoat = fill that 60300",
          len(recs) == 1 and recs[0]["exit"] == 60300
          and recs[0]["estimated"] is False, recs)
    eng2, st2 = make_engine(fake, protection=False)
    lot = open_lot(eng2, "long", 60000, level="b2")
    for i in range(150):                       # short leg ban ron
        fake._fill("BTCUSDT", "sell", "SHORT", 0.001, 60000)
        fake._fill("BTCUSDT", "buy", "SHORT", 0.001, 60000)
    CLOCK.sleep(120)
    manual_close(fake, "long", 0.01, 60450)
    detect_round(eng2, {"BTCUSDT": 60400})
    recs = detect_round(eng2, {"BTCUSDT": 60400})
    check("busy symbol (>100 trade): gia thoat = fill that 60450",
          len(recs) == 1 and recs[0]["exit"] == 60450
          and recs[0]["estimated"] is False, recs)


# ===================================================================
# Reconcile hold: van dat SL/TP thieu, khong dong cuong buc
# ===================================================================
def _hold(st):
    st["halted"] = True
    st["halt_reason"] = "exchange position reconciliation mismatch"


def test_hold_still_rearms_missing_sl():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    fake.post_failures = [BinanceError("binance -1001 internal error")] * 2
    lot = open_lot(eng, "long", 60000, level="b1")
    check("hold: lot mo ra thieu SL", lot["protection_status"] == "retrying"
          and not lot.get("sl_algo_id"), lot)
    _hold(st)
    CLOCK.sleep(11)
    changed = binance_bot.protect_during_hold(eng)
    check("hold: SL/TP duoc dat lai du dang reconcile hold",
          changed and lot.get("sl_algo_id") and lot.get("tp_algo_id")
          and lot["protection_status"] == "armed"
          and len(guards_of(fake, lot)) == 2, (lot, eng.logs[-3:]))


def test_hold_never_force_closes_without_sl():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    fake.post_failures = [BinanceError("binance -1001 internal error")] * 60
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.post_failures = [BinanceError("binance -1001 internal error")] * 60
    _hold(st)
    for _ in range(15):
        CLOCK.sleep(10)
        binance_bot.protect_during_hold(eng)
    crit = [m for m in eng.logs if "reconcile hold" in m and "CRITICAL" in m]
    check("hold: qua deadline KHONG dong lot (state lech san)",
          len(st["positions"]) == 1
          and fake.positions.get(("BTCUSDT", "long")) == 0.01
          and not eng.drain_close_records(), st["positions"])
    check("hold: CRITICAL log 1 lan, van tiep tuc thu dat SL",
          len(crit) == 1 and fake.calls.count("post_algo") > 5,
          (crit, fake.calls.count("post_algo")))
    # het hold -> hanh vi cu: dong lot khong SL qua deadline
    st["halted"] = False
    st["halt_reason"] = ""
    retry(eng, 10)
    recs = eng.drain_close_records()
    check("het hold: lot khong SL qua deadline bi dong nhu cu",
          st["positions"] == [] and len(recs) == 1, recs)


# ===================================================================
# Startup: algo tay khong halt; halt doi chieu duoc kiem tra lai
# ===================================================================
def _restart(fake, st):
    eng2, st2 = make_engine(fake)
    st2["positions"] = copy.deepcopy(st["positions"])
    return eng2, st2


def test_startup_foreign_algo_not_halt():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    open_lot(eng, "long", 60000, level="b1")
    manual = add_algo(fake, "web_aF3kManualStop")         # nguoi dung tu dat
    eng2, st2 = _restart(fake, st)
    eng2._reconcile_startup()
    check("startup: algo tay tren symbol bot -> khong halt",
          not st2.get("halted"), (st2.get("halt_reason"), eng2.logs[-4:]))
    check("startup: algo tay duoc giu nguyen + co WARNING",
          fake.algos[manual]["algoStatus"] == "NEW"
          and any("khong do bot tao" in m for m in eng2.logs))


def test_startup_protection_halt_rechecked():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    lot = open_lot(eng, "long", 60000, level="b1")
    fake.algos[lot["sl_algo_id"]]["quantity"] = "0.02"    # guard lech qty
    eng2, st2 = _restart(fake, st)
    eng2._reconcile_startup()
    check("recheck: guard lech -> halt luc start",
          st2.get("halt_reason") == "exchange protection reconciliation "
          "mismatch", st2.get("halt_reason"))
    fake.fapiPrivateDeleteAlgoOrder({"algoId": lot["sl_algo_id"]})  # don tay
    check("recheck: chua toi 60s -> chua kiem tra lai",
          eng2.recheck_startup_holds() is False and st2.get("halted"))
    CLOCK.sleep(61)
    ok = eng2.recheck_startup_holds()
    check("recheck: san da sach -> tu unhalt khong can restart",
          ok and not st2.get("halted"), (st2.get("halt_reason"),
                                         eng2.logs[-4:]))
    sync(eng2)
    retry(eng2, 1)
    check("recheck: SL bi huy tay duoc dat lai",
          len(guards_of(fake, st2["positions"][0])) == 2,
          fake.open_algos())


def test_startup_open_order_halt_rechecked():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    open_lot(eng, "long", 60000, level="b1")
    working = [{"id": "77", "symbol": "BTCUSDT", "status": "open",
                "clientOrderId": "web_limit",
                "info": {"symbol": "BTCUSDT", "orderId": "77"}}]
    fake.fetch_open_orders = lambda *a, **k: list(working)
    eng2, st2 = _restart(fake, st)
    eng2._reconcile_startup()
    check("recheck: lenh LIMIT treo -> halt luc start",
          st2.get("halt_reason") == "unmanaged open exchange order",
          st2.get("halt_reason"))
    CLOCK.sleep(61)
    eng2.recheck_startup_holds()
    check("recheck: lenh van con -> van halt", st2.get("halted") is True)
    working.clear()
    CLOCK.sleep(61)
    check("recheck: lenh da huy -> tu unhalt",
          eng2.recheck_startup_holds() and not st2.get("halted"),
          st2.get("halt_reason"))
    st2["halted"], st2["halt_reason"] = True, "daily stop -10.00%"
    CLOCK.sleep(61)
    check("recheck: khong dung vao halt khac (daily stop)",
          eng2.recheck_startup_holds() is False and st2["halted"])


# ===================================================================
# Leg giam mot phan (dong tay 1 trong nhieu lot grid)
# ===================================================================
def _three_lots(eng):
    lots = [open_lot(eng, "long", 60000, level="b%d" % i) for i in (1, 2, 3)]
    CLOCK.sleep(120)
    return lots


def test_partial_manual_close_books_lot_lifo():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    l1, l2, l3 = _three_lots(eng)
    manual_close(fake, "long", 0.01, 60500)          # nguoi dung dong 0.01
    first = detect_round(eng, {"BTCUSDT": 60450})
    recs = detect_round(eng, {"BTCUSDT": 60450})
    check("partial: lan quet 1 chi cho xac nhan", first == [])
    check("partial: ghi nhan 1 lot (LIFO = b3) voi fill that 60500",
          len(recs) == 1 and recs[0]["id"] == l3["id"]
          and recs[0]["exit"] == 60500 and recs[0]["estimated"] is False
          and recs[0]["exit_source"] == "exchange_detect_partial", recs)
    check("partial: guard cua lot da dong bi huy, lot con lai giu guard",
          not guards_of(fake, l3) and len(guards_of(fake, l1)) == 2
          and len(guards_of(fake, l2)) == 2, fake.open_algos())
    ok = eng.reconcile_positions(force=True)
    check("partial: state khop san, khong halt",
          ok and not st.get("halted") and len(st["positions"]) == 2,
          (st.get("halt_reason"), st["positions"]))


def test_partial_guard_fill_left_to_sync():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    l1, l2, l3 = _three_lots(eng)
    fake.fire(l1["tp_algo_id"], price=60300)          # guard cua b1 khop
    detect_round(eng, {"BTCUSDT": 60300})
    recs = detect_round(eng, {"BTCUSDT": 60300})
    check("partial: guard bot vua khop -> detect KHONG doan lot",
          recs == [] and len(st["positions"]) == 3, recs)
    booked = sync(eng)
    check("partial: sync ghi dung lot b1 (khong phai b3)",
          len(booked) == 1 and booked[0]["id"] == l1["id"]
          and {p["id"] for p in st["positions"]} == {l2["id"], l3["id"]},
          (booked, st["positions"]))


def test_partial_unmatched_reduction_still_halts():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    _three_lots(eng)
    manual_close(fake, "long", 0.005, 60500)          # nua lot
    detect_round(eng)
    recs = detect_round(eng)
    ok = eng.reconcile_positions(force=True)
    check("partial: giam khong khop lot nao -> khong doan, reconcile halt",
          recs == [] and not ok and st.get("halted")
          and len(st["positions"]) == 3, (recs, st.get("halt_reason")))


# ===================================================================
# Daily stop: chay ca khi dang halt ly do khac, thu lai lot chua dong
# ===================================================================
def _risk_day(st, dp, day="2026-10-07"):
    bot_state(st)
    st.update({"_risk_initialized": True, "day": day,
               "day_start_equity": 1000.0, "daily_drawdown_pct": dp,
               "mark_equity": 1000.0 * (1 + dp)})


def test_daily_stop_runs_while_halted_other_reason():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    open_lot(eng, "long", 60000, level="b1")
    open_lot(eng, "short", 60000, level="s1")
    _risk_day(st, -0.12)
    st["halted"], st["halt_reason"] = True, "unmanaged open exchange order"
    changed = binance_bot.enforce_daily_stop(eng, st, {"BTCUSDT": 59000},
                                             "2026-10-07")
    check("daily: dang halt ly do khac van dong het vi the",
          changed and st["positions"] == []
          and not any(fake.positions.values()), (st["positions"],
                                                 fake.positions))
    check("daily: giu halt_reason persistent, danh dau daily_stop_day",
          st["halt_reason"] == "unmanaged open exchange order"
          and st["daily_stop_day"] == "2026-10-07", st.get("halt_reason"))


def test_daily_stop_retries_unclosed_lots():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    a = open_lot(eng, "long", 60000, level="b1")
    _risk_day(st, -0.15)
    real_close = eng.close
    calls = []

    def flaky_close(pos, price, reason):
        calls.append(pos["id"])
        if len(calls) == 1:
            return None                     # cooldown / guard dang khop
        return real_close(pos, price, reason)
    eng.close = flaky_close
    binance_bot.enforce_daily_stop(eng, st, {"BTCUSDT": 59000}, "2026-10-07")
    check("daily: lan 1 dong khong duoc -> lot con, da halt",
          len(st["positions"]) == 1 and st["halted"]
          and st["halt_reason"].startswith("daily stop"))
    binance_bot.enforce_daily_stop(eng, st, {"BTCUSDT": 59000}, "2026-10-07")
    check("daily: vong sau thu lai va dong duoc",
          st["positions"] == [] and calls == [a["id"], a["id"]], calls)


def test_daily_stop_no_stale_or_manual_resume_closes():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    open_lot(eng, "long", 60000, level="b1")
    _risk_day(st, -0.12, day="2026-10-06")          # baseline hom qua
    binance_bot.enforce_daily_stop(eng, st, {"BTCUSDT": 59000}, "2026-10-07")
    check("daily: baseline chua sang ngay moi -> khong kich hoat",
          len(st["positions"]) == 1 and not st.get("halted")
          and "daily_stop_day" not in st)
    _risk_day(st, -0.12)
    st["daily_stop_day"] = "2026-10-07"             # da stop hom nay
    st["halted"], st["halt_reason"] = False, ""     # van hanh unhalt tay
    binance_bot.enforce_daily_stop(eng, st, {"BTCUSDT": 59000}, "2026-10-07")
    check("daily: da unhalt tay -> khong dong lot moi, khong kich lai",
          len(st["positions"]) == 1 and not st.get("halted"))


def test_basket_stop_retries_unclosed_lots():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    bot_state(st)
    st["mark_equity"] = 1000.0
    a = open_lot(eng, "long", 60000, level="b1")
    b = open_lot(eng, "long", 59700, level="b2")
    real_close = eng.close
    blocked = {b["id"]}

    def flaky_close(pos, price, reason):
        if pos["id"] in blocked:
            blocked.discard(pos["id"])
            return None                     # cooldown 1 lan
        return real_close(pos, price, reason)
    eng.close = flaky_close
    marks = {"BTCUSDT": 57000}              # lo basket > 2% equity
    binance_bot.manage_grid_risk(eng, st, marks)
    grid = st["grids"]["BTCUSDT"]
    check("basket: lan 1 con 1 lot, danh dau dang stop",
          [p["id"] for p in st["positions"]] == [b["id"]]
          and grid.get("basket_stopping") and grid.get("risk_halted"),
          (st["positions"], grid))
    binance_bot.manage_grid_risk(eng, st, marks)
    check("basket: vong sau thu lai, dong het, bo co",
          st["positions"] == [] and not grid.get("basket_stopping")
          and grid.get("risk_halted"), (st["positions"], grid))


def test_fill_price_falls_back_to_user_trades():
    """Lenh MARKET da khop nhung response/fetch_order thieu avgPrice: lay gia
    tu userTrades thay vi halt va bo vi the tran (khong SL/TP) tren san."""
    fake = FakeBinance()
    eng, st = make_engine(fake)
    real_fill = fake._fill

    def fill_no_avg(*a, **k):
        order = real_fill(*a, **k)
        order["average"] = None
        order["status"] = "open"            # response chua bao FILLED
        return order
    fake._fill = fill_no_avg
    fake.fetch_order = lambda *a, **k: (_ for _ in ()).throw(
        BinanceError("binance -1001 internal error"))
    eng._order_average = lambda order: None
    eng._validate_order_result = lambda *a, **k: None
    pos, why = eng.open("BTCUSDT", "long", 600.0, 60000, 0.03, 0.005, "grid")
    check("fill fallback: lot duoc ghi voi gia tu userTrades, khong halt",
          pos is not None and pos["entry"] == 60000.0
          and not st.get("halted"), (why, st.get("halt_reason")))
    check("fill fallback: lot co du SL/TP tren san",
          pos is not None and len(guards_of(fake, pos)) == 2)


# ===================================================================
# Lenh mo timeout mo ho: tra lai theo clientOrderId, nhan lot neu da khop
# ===================================================================
def test_ambiguous_open_filled_is_adopted_with_guards():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    fake.timeout_mode = "accepted"
    pos, why = eng.open("BTCUSDT", "long", 600.0, 60000, 0.03, 0.005, "grid",
                        level="b1")
    check("ambiguous: open tra None, halt, luu lenh can doi chieu",
          pos is None and st.get("halt_reason", "").startswith("ambiguous")
          and len(st.get("ambiguous_orders", [])) == 1
          and fake.positions.get(("BTCUSDT", "long")) == 0.01,
          (why, st.get("halt_reason"), st.get("ambiguous_orders")))
    eng.resolve_ambiguous_orders(force=True)
    check("ambiguous: chua tra cuu duoc -> van giu, van halt",
          st["halted"] and len(st["ambiguous_orders"]) == 1)
    CLOCK.sleep(31)
    changed = eng.resolve_ambiguous_orders()
    lot = st["positions"][0] if st["positions"] else {}
    check("ambiguous: lenh da khop -> nhan lot qty/gia that, level giu nguyen",
          changed and lot.get("qty") == 0.01 and lot.get("entry") == 60000.0
          and lot.get("level") == "b1" and lot.get("tag") == "grid", lot)
    check("ambiguous: lot nhan ve co du SL/TP tren san",
          lot and len(guards_of(fake, lot)) == 2)
    ok = eng.reconcile_positions(force=True)
    check("ambiguous: unhalt + san khop state",
          ok and not st.get("halted") and st["ambiguous_orders"] == [],
          st.get("halt_reason"))


def test_ambiguous_open_never_accepted_expires():
    fake = FakeBinance()
    eng, st = make_engine(fake)
    fake.timeout_mode = "lost"
    pos, _ = eng.open("BTCUSDT", "long", 600.0, 60000, 0.03, 0.005, "grid")
    CLOCK.sleep(31)
    eng.resolve_ambiguous_orders()
    check("ambiguous lost: truoc han van giu + halt",
          st["halted"] and len(st["ambiguous_orders"]) == 1)
    CLOCK.sleep(300)
    eng.resolve_ambiguous_orders()
    check("ambiguous lost: qua han khong ton tai -> bo, unhalt, khong lot",
          not st.get("halted") and st["ambiguous_orders"] == []
          and st["positions"] == [], (st.get("halt_reason"),
                                      st.get("ambiguous_orders")))


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
    test_startup_lot_without_guard_is_rearmed_not_halted,
    test_close_exact_qty_leaves_no_dust,
    test_close_last_lot_sweeps_old_dust_only,
    test_close_with_sibling_never_sweeps,
    test_real_fees_bot_close,
    test_real_fees_exchange_tp,
    test_fee_fallback_estimated,
    test_real_fees_detect_group_close_prorated,
    test_detect_real_exit_old_lot_and_busy_symbol,
    test_hold_still_rearms_missing_sl,
    test_hold_never_force_closes_without_sl,
    test_startup_foreign_algo_not_halt,
    test_startup_protection_halt_rechecked,
    test_startup_open_order_halt_rechecked,
    test_partial_manual_close_books_lot_lifo,
    test_partial_guard_fill_left_to_sync,
    test_partial_unmatched_reduction_still_halts,
    test_daily_stop_runs_while_halted_other_reason,
    test_daily_stop_retries_unclosed_lots,
    test_daily_stop_no_stale_or_manual_resume_closes,
    test_basket_stop_retries_unclosed_lots,
    test_fill_price_falls_back_to_user_trades,
    test_ambiguous_open_filled_is_adopted_with_guards,
    test_ambiguous_open_never_accepted_expires,
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
