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

import live_binance  # noqa: E402

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


TESTS = [
    test_close_one_of_many_grid_lots,
    test_close_detects_real_partial,
    test_cancel_absent_guard_compares_aggregate,
]


def main():
    made_cfg = not os.path.exists(os.path.join(BASE, "config.json"))
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # pragma: no cover - surfaced as failure
            import traceback
            traceback.print_exc()
            check(test.__name__ + " (exception)", False, exc)
    if made_cfg and os.path.exists(os.path.join(BASE, "config.json")):
        os.remove(os.path.join(BASE, "config.json"))
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
