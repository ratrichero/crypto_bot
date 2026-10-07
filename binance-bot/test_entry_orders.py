#!/usr/bin/env python3
"""Test entry LIMIT post-only (G5): vong doi lenh cho tren engine that +
FakeBinance (GTX, huy, khop 1 phan, WS event, timeout mang, khoi dong lai)
va lap ke hoach slot o bot (manage_range_limits). Offline.

Run: python3 test_entry_orders.py
"""
import copy
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "..", "db"))

import test_protection as tp  # noqa: E402  (harness: FakeBinance, CLOCK)
from test_protection import BinanceError, CLOCK, FakeBinance  # noqa: E402

import binance_bot as bb  # noqa: E402
import bot_config  # noqa: E402
import live_binance  # noqa: E402
import scanner as sc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + ("" if cond else "  " + str(detail)))


class FakeLimit(FakeBinance):
    """Them lenh LIMIT GTX vao FakeBinance (Hedge Mode)."""

    def __init__(self, prices=None):
        super().__init__(prices)
        self.limits = {}            # cid -> raw order
        self.post_error = None
        self.limit_timeout = None   # None | "lost" | "accepted"
        self.cancel_error = None
        self.extra_open = []        # lenh "la" tren san

    def fapiPrivatePostOrder(self, params):
        self.calls.append("post_order")
        self.last_post = dict(params)
        if self.post_error is not None:
            exc, self.post_error = self.post_error, None
            raise exc
        mode, self.limit_timeout = self.limit_timeout, None
        if mode == "lost":
            raise BinanceError("binance RequestTimeout: timed out")
        px = float(params["price"])
        mkt = self.prices[params["symbol"]]
        cross = (px >= mkt) if params["side"] == "BUY" else (px <= mkt)
        oid = self._id()
        raw = {"orderId": oid, "clientOrderId": params["newClientOrderId"],
               "symbol": params["symbol"], "side": params["side"],
               "positionSide": params["positionSide"], "type": "LIMIT",
               "timeInForce": params["timeInForce"], "price": params["price"],
               "origQty": params["quantity"], "executedQty": "0",
               "avgPrice": "0", "status": "EXPIRED" if cross else "NEW"}
        self.limits[raw["clientOrderId"]] = raw
        if mode == "accepted":
            raise BinanceError("binance RequestTimeout: timed out")
        return copy.deepcopy(raw)

    def fill_limit(self, cid, qty):
        o = self.limits[cid]
        px = float(o["price"])
        key = (o["symbol"], o["positionSide"].lower())
        self.positions[key] = round(self.positions.get(key, 0.0) + qty, 10)
        done = float(o["executedQty"]) + qty
        o["executedQty"] = str(round(done, 10))
        o["avgPrice"] = str(px)
        o["status"] = ("FILLED" if done >= float(o["origQty"]) - 1e-12
                       else "PARTIALLY_FILLED")
        self.trades.append({
            "id": str(self._id()), "order": str(o["orderId"]),
            "symbol": o["symbol"], "side": o["side"].lower(), "price": px,
            "amount": qty, "timestamp": int(CLOCK.now * 1000),
            "fee": {"cost": qty * px * 0.0002, "currency": "USDT"},
            "info": {"positionSide": o["positionSide"],
                     "orderId": str(o["orderId"]),
                     "commission": str(qty * px * 0.0002),
                     "commissionAsset": "USDT"}})
        return self.event(cid)

    def event(self, cid):
        o = self.limits[cid]
        return {"e": "ORDER_TRADE_UPDATE",
                "o": {"s": o["symbol"], "c": cid, "i": o["orderId"],
                      "X": o["status"], "x": "TRADE", "z": o["executedQty"],
                      "ap": o["avgPrice"], "ps": o["positionSide"]}}

    def fapiPrivateDeleteOrder(self, params):
        self.calls.append("cancel_order")
        if self.cancel_error is not None:
            exc, self.cancel_error = self.cancel_error, None
            raise exc
        o = self.limits.get(params.get("origClientOrderId"))
        if o is None or o["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise BinanceError('binance {"code":-2011,'
                               '"msg":"Unknown order sent."}')
        o["status"] = "CANCELED"
        return copy.deepcopy(o)

    def fapiPrivateGetOrder(self, params):
        o = self.limits.get(params.get("origClientOrderId"))
        if o is not None:
            self.calls.append("get_order")
            return copy.deepcopy(o)
        return super().fapiPrivateGetOrder(params)

    def fetch_open_orders(self, symbol=None, since=None, limit=None,
                          params=None):
        self.calls.append("fetch_open_orders")
        rows = [{"symbol": o["symbol"], "id": str(o["orderId"]),
                 "clientOrderId": o["clientOrderId"], "status": "open",
                 "info": dict(o)}
                for o in self.limits.values()
                if o["status"] in ("NEW", "PARTIALLY_FILLED")]
        return rows + copy.deepcopy(self.extra_open)


def eng_new(fake=None, **cfg):
    fake = fake or FakeLimit({"BTCUSDT": 60000.0})
    eng, st = tp.make_engine(fake, **cfg)
    eng.cfg.setdefault("grid", {}).update(
        entry_ttl_minutes=60, partial_fill_timeout_seconds=60,
        entry_unknown_expire_seconds=60, entry_poll_seconds=20)
    eng._price_ticks["BTCUSDT"] = 0.1
    persisted = []
    eng.persist_cb = lambda: persisted.append(
        [o["status"] for o in st.get("entry_orders", [])])
    eng.persisted = persisted
    return eng, st, fake


def place(eng, side="long", price=59400.0, level="rb1", notional=600.0):
    return eng.place_entry_limit("BTCUSDT", side, notional, price, 0.02, 0.01,
                                 "grid", level=level)


# ----------------------------------------------------------- dat lenh
eng, st, fake = eng_new()
rec, why = place(eng, price=59400.07)
check("place: lenh NEW, ghi state", rec and rec["status"] == "NEW"
      and st["entry_orders"] == [rec], (rec, why))
check("place: persist TRUOC khi gui (SUBMITTING)",
      eng.persisted and eng.persisted[0] == ["SUBMITTING"], eng.persisted)
check("place: LIMIT GTX, positionSide, cid e<SYM>L<n>",
      fake.last_post["type"] == "LIMIT"
      and fake.last_post["timeInForce"] == "GTX"
      and fake.last_post["positionSide"] == "LONG"
      and rec["cid"].startswith("eBTCUSDTL"), fake.last_post)
check("place: gia BUY lam tron XUONG tick", fake.last_post["price"] == "59400",
      fake.last_post["price"])
r2, _ = place(eng, side="short", price=60600.01, level="rs1")
check("place: gia SELL lam tron LEN tick",
      fake.last_post["price"] == "60600.1", fake.last_post["price"])
r3, why = place(eng, price=59300, level="rb1")
check("place: trung tang -> tu choi", r3 is None and why == "duplicate_level")
check("pending: notional + slot tinh vao engine",
      abs(eng.pending_entry_notional() - (rec["notional"] + r2["notional"]))
      < 1e-6)
eng.cfg["max_total_positions"] = 2
pos, why = eng.open("BTCUSDT", "long", 600, 60000, 0.02, 0.01, "scalp")
check("slot: lenh cho chiem max_total_positions (market bi chan)",
      pos is None and why == "max_positions", why)
eng.cfg["max_total_positions"] = 10
eng.cfg["risk"]["max_notional_mult"] = 1.5
pos, why = eng.open("BTCUSDT", "long", 600, 60000, 0.02, 0.01, "scalp")
check("exposure: tinh ca notional lenh cho", pos is None
      and why == "exposure_cap", why)
eng.cfg["risk"]["max_notional_mult"] = 10

# GTX se khop ngay -> EXPIRED
eng, st, fake = eng_new()
rec, why = place(eng, price=60100)
check("GTX cat qua gia -> EXPIRED, bo lenh",
      rec is None and why == "post_only_expired" and not st["entry_orders"],
      why)
rec, why = place(eng, price=59000)
check("GTX bi tu choi -> cooldown tang do", rec is None and "cooldown" in
      str(why), why)
eng, st, fake = eng_new()
fake.post_error = BinanceError('binance {"code":-5022,"msg":"Due to the '
                               'order could not be executed as maker"}')
rec, why = place(eng, price=59000, level="rb2")
check("-5022 -> bo lenh", rec is None and why == "post_only_rejected"
      and not st["entry_orders"], why)
eng, st, fake = eng_new()
fake.post_error = BinanceError('binance {"code":-2019,"msg":"Margin is '
                               'insufficient."}')
rec, why = place(eng, price=59000, level="rb3")
check("loi khac -> bo lenh, khong treo", rec is None
      and not st["entry_orders"], why)

# --------------------------------------------------- loi mang mo ho
eng, st, fake = eng_new()
fake.limit_timeout = "accepted"
rec, why = place(eng)
check("timeout (san da nhan) -> UNKNOWN, giu slot",
      rec is None and why == "entry_unknown"
      and st["entry_orders"][0]["status"] == "UNKNOWN", st["entry_orders"])
CLOCK.sleep(6)
eng.sync_entry_orders()
check("timeout: GET theo cid -> NEW", st["entry_orders"][0]["status"] == "NEW"
      and st["entry_orders"][0]["order_id"], st["entry_orders"])
eng, st, fake = eng_new()
fake.limit_timeout = "lost"
place(eng)
CLOCK.sleep(10)
eng.sync_entry_orders()
check("timeout (san khong nhan): chua het han -> van UNKNOWN",
      st["entry_orders"] and st["entry_orders"][0]["status"] == "UNKNOWN")
CLOCK.sleep(61)
eng.sync_entry_orders()
check("timeout (san khong nhan): -2013 qua 60s -> bo",
      st["entry_orders"] == [] and st["positions"] == [])

# ----------------------------------------------------- khop toan bo (WS)
eng, st, fake = eng_new()
rec, _ = place(eng)
cid, qty = rec["cid"], rec["qty"]
CLOCK.sleep(5)
eng.on_user_event(fake.fill_limit(cid, qty))
new = eng.sync_entry_orders()
p = st["positions"][0] if st["positions"] else {}
check("fill WS: thanh 1 lot grid dung qty/gia/level",
      len(new) == 1 and p.get("qty") == qty and p.get("entry") == 59400.0
      and p.get("level") == "rb1" and p.get("tag") == "grid"
      and p.get("maker_entry"), p)
check("fill WS: SL/TP dat tren san ngay", len(fake.open_algos()) == 2,
      fake.algos)
check("fill WS: SL/TP tinh tu gia khop",
      abs(p["sl"] - 59400 * 0.98) < 1e-6 and abs(p["tp"] - 59400 * 1.01)
      < 1e-6, (p.get("sl"), p.get("tp")))
check("fill WS: phi maker that tu userTrades",
      abs(p["fee_entry"] - qty * 59400 * 0.0002) < 1e-9, p.get("fee_entry"))
check("fill WS: het lenh cho, khong halt",
      st["entry_orders"] == [] and not st.get("halted"))
check("reconcile sau fill: khop", eng.reconcile_positions(force=True)
      and not st.get("halted"), st.get("halt_reason"))

# ------------------------------------------------- khop 1 phan + timeout
eng, st, fake = eng_new()
lot = tp.open_lot(eng, "long", 60000, level="rb0")        # 1 lot co san
rec, _ = place(eng)
cid, qty = rec["cid"], rec["qty"]
half = round(qty / 2, 3)
eng.on_user_event(fake.fill_limit(cid, half))
eng.sync_entry_orders()
check("partial: van la lenh cho, ghi qty da khop",
      st["entry_orders"][0]["status"] == "PARTIALLY_FILLED"
      and st["entry_orders"][0]["filled"] == half, st["entry_orders"])
check("partial: _local_qty cong phan da khop",
      abs(eng._local_qty("BTCUSDT", "long") - (lot["qty"] + half)) < 1e-9)
check("partial: reconcile khong halt (san = lot + phan khop)",
      eng.reconcile_positions(force=True) and not st.get("halted"),
      st.get("halt_reason"))
CLOCK.sleep(120)
eng._last_detect_closed = 0
recs = eng.detect_exchange_closed({"BTCUSDT": 60000})
CLOCK.sleep(15)
eng._last_detect_closed = 0
recs += eng.detect_exchange_closed({"BTCUSDT": 60000})
check("partial: detect_exchange_closed khong ghi nham lot dong",
      recs == [] and len(st["positions"]) == 1, recs)
CLOCK.sleep(61)
eng.sync_entry_orders()        # qua partial timeout -> huy phan con lai
new = eng.sync_entry_orders()
lots = [x for x in st["positions"] if x.get("level") == "rb1"]
check("partial timeout: huy phan con lai, phan khop thanh lot",
      fake.limits[cid]["status"] == "CANCELED" and len(lots) == 1
      and abs(lots[0]["qty"] - half) < 1e-9 and st["entry_orders"] == [],
      (fake.limits[cid]["status"], st["positions"], st["entry_orders"]))
check("partial timeout: lot co SL/TP rieng, reconcile khop",
      len(fake.open_algos()) == 4 and eng.reconcile_positions(force=True)
      and not st.get("halted"), (len(fake.open_algos()), st.get("halt_reason")))

# ------------------------------------------------------------------ TTL
eng, st, fake = eng_new()
rec, _ = place(eng)
CLOCK.sleep(3601)
eng.sync_entry_orders()
eng.sync_entry_orders()
check("TTL: qua 60 phut -> huy, khong lot",
      fake.limits[rec["cid"]]["status"] == "CANCELED"
      and st["entry_orders"] == [] and st["positions"] == [])

# ------------------------------------------- huy gap -2011 (da khop)
eng, st, fake = eng_new()
rec, _ = place(eng)
fake.fill_limit(rec["cid"], rec["qty"])      # khop, WS bi lo
eng.cancel_entry(rec, "test")
new = eng.sync_entry_orders()
check("cancel -2011: GET lai thay FILLED -> nhan lot",
      len(st["positions"]) == 1 and st["entry_orders"] == [], st)
eng, st, fake = eng_new()
rec, _ = place(eng)
fake.cancel_error = BinanceError("binance NetworkError: connection reset")
check("cancel loi mang -> giu lenh, thu lai sau",
      not eng.cancel_entry(rec, "x") and st["entry_orders"])
CLOCK.sleep(6)
check("cancel thu lai -> CANCELED", eng.cancel_entry(rec, "x")
      and fake.limits[rec["cid"]]["status"] == "CANCELED")

# ------------------------------------------------ bui duoi minNotional
eng, st, fake = eng_new()
rec, _ = place(eng)
eng.on_user_event(fake.fill_limit(rec["cid"], 0.001))   # ~59 USDT
eng._filters_for = lambda symbol: (0.001, 0.001, 100.0)
eng.cancel_entry(rec, "test")
eng.sync_entry_orders()
drained = eng.drain_close_records()
check("dust: phan khop < minNotional -> lot dong ngay (ENTRY_DUST)",
      st["positions"] == [] and drained
      and drained[0]["reason"] == "ENTRY_DUST", (st["positions"], drained))

# ------------------------------------------------------- khoi dong lai
eng, st, fake = eng_new()
rec, _ = place(eng)
fake.extra_open = []
check("startup: lenh entry dang quan ly -> khong halt",
      eng._reconcile_startup_open_orders() and not st.get("halted"),
      st.get("halt_reason"))
fake.limits["eBTCUSDTL999"] = dict(fake.limits[rec["cid"]],
                                   clientOrderId="eBTCUSDTL999",
                                   orderId=99999)
check("startup: lenh entry cua bot mat state -> huy, khong halt",
      eng._reconcile_startup_open_orders() and not st.get("halted")
      and fake.limits["eBTCUSDTL999"]["status"] == "CANCELED")
fake.extra_open = [{"symbol": "BTCUSDT", "id": "1", "clientOrderId": "web_x",
                    "status": "open", "info": {}}]
eng._bot_symbols = {"BTCUSDT"}
check("startup: lenh la (khong phai bot) -> van halt nhu cu",
      not eng._reconcile_startup_open_orders()
      and st.get("halt_reason") == "unmanaged open exchange order")

eng, st, fake = eng_new()
rec, _ = place(eng)
fake.fill_limit(rec["cid"], rec["qty"])      # khop luc bot tat
st2 = copy.deepcopy(st)
eng2, _st = tp.make_engine(fake)
eng2.state = st2
eng2._reconcile_startup()
check("startup: lenh khop luc bot tat -> thanh lot, khong halt",
      len(st2["positions"]) == 1 and st2["entry_orders"] == []
      and not st2.get("halted"), (st2["positions"], st2.get("halt_reason")))

# ------------------------------------------------------------- dry-run
cfg = copy.deepcopy(tp.CFG)
cfg["grid"].update(entry_ttl_minutes=60, partial_fill_timeout_seconds=60)
dst = {"equity": 1000.0, "positions": [], "_pid": 0,
       "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0}}
deng = live_binance.BinanceEngine(cfg, dst, dry_run=True, log=lambda m: None)
rec, _ = deng.place_entry_limit("BTCUSDT", "long", 1000, 99.0, 0.03, 0.01,
                                "grid", level="rb1")
deng.sync_entry_orders({"BTCUSDT": 99.5})
check("dry-run: gia chua cham -> cho", dst["entry_orders"]
      and not dst["positions"])
deng.sync_entry_orders({"BTCUSDT": 98.99})
p = dst["positions"][0] if dst["positions"] else {}
check("dry-run: gia cham -> lot, phi maker",
      p.get("entry") == 99.0 and abs(p.get("fee_entry", 0)
                                     - 1000 * 0.0002) < 1e-6, p)

# --------------------------------------------- bot: manage_range_limits
saved = copy.deepcopy(bb.CFG)
saved_sc, saved_sym = bb.SCANNER, list(bb.SYMBOLS)
NOW = [CLOCK.now]
try:
    bb.SYMBOLS[:] = ["BTCUSDT", "ETHUSDT"]
    bb.CFG.update(order_margin_usdt=100, leverage=10, max_total_positions=10)
    G = bb.CFG["grid"]
    G.update(engine="range", entry_mode="limit", step_pct=0.01,
             step_min=0.01, step_max=0.01, tp_pct=0.01, sl_pct=0.03,
             levels_each_side=5, max_positions=7, max_lots_per_symbol=4,
             max_symbols=0, max_new_orders_per_cycle=2,
             limit_min_gap_pct=0.0005, entry_ttl_minutes=60,
             partial_fill_timeout_seconds=60)
    bb.CFG["scanner"] = {"enabled": True, "mode": "observe", "top_k": 2,
                         "rescan_minutes": 15}
    bb.SCANNER = sc.ScannerRunner(bb.CFG, lambda *a: [],
                                  clock=lambda: CLOCK.now)
    for s in ("BTCUSDT", "ETHUSDT"):
        bb.SCANNER.results[s] = {
            "symbol": s, "ts": CLOCK.now, "passed": True,
            "score": 80 if s == "BTCUSDT" else 60, "reasons": [],
            "metrics": {"range_low": 95.0, "range_high": 105.0,
                        "adx_1h": 15, "atr15_pct": None}}
    bst = {"equity": 10000.0, "mark_equity": 10000.0, "positions": [],
           "grids": {}, "_pid": 0,
           "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0}}
    beng = live_binance.BinanceEngine(bb.CFG, bst, dry_run=True,
                                      log=lambda m: None)
    allowed = bb.range_allowed_symbols()
    prices = {"BTCUSDT": 100.0, "ETHUSDT": 100.0}
    for s in prices:
        bb.manage_range_grid(beng, bst, s, prices[s], allowed)
    check("bot limit: manage_range_grid chi dung bien, khong vao market",
          bst["positions"] == [] and all(bst["grids"][s].get("range")
                                         for s in prices))
    bb.manage_range_limits(beng, bst, prices, allowed)
    lv = sorted((o["symbol"], o["level"]) for o in bst["entry_orders"])
    check("bot limit: toi da 2 lenh moi/vong, tang gan gia, symbol diem cao",
          len(lv) == 2 and all(x[0] == "BTCUSDT" for x in lv)
          and {x[1] for x in lv} == {"rb1", "rs1"}, lv)
    for _ in range(5):
        bb.manage_range_limits(beng, bst, prices, allowed)
    per = {}
    for o in bst["entry_orders"]:
        per[o["symbol"]] = per.get(o["symbol"], 0) + 1
    check("bot limit: lenh cho <= max_lots_per_symbol & <= max_positions",
          per.get("BTCUSDT") == 4 and sum(per.values()) == 7, per)
    o_rb1 = [o for o in bst["entry_orders"] if o["symbol"] == "BTCUSDT"
             and o["level"] == "rb1"][0]
    check("bot limit: SL/TP % tinh tu gia lenh (SL bien, TP 1%)",
          abs(o_rb1["tp_pct"] - 0.01) < 1e-9
          and abs(o_rb1["sl_pct"] - min(0.03, 1 - 95 * 0.995
                                        / o_rb1["price"])) < 1e-9, o_rb1)
    beng.sync_entry_orders({"BTCUSDT": 98.9, "ETHUSDT": 100.0})
    lots = [p for p in bst["positions"] if p["symbol"] == "BTCUSDT"]
    check("bot limit: gia cham rb1 -> lot rb1", [p["level"] for p in lots]
          == ["rb1"], lots)
    n_before = len(bst["entry_orders"])
    bb.manage_range_limits(beng, bst, {"BTCUSDT": 98.9, "ETHUSDT": 100.0},
                           allowed)
    tot = len(bst["positions"]) + len(bst["entry_orders"])
    check("bot limit: lot + lenh cho van <= max_positions", tot <= 7,
          (len(bst["positions"]), len(bst["entry_orders"])))
    # bien vo -> huy lenh cho cua symbol
    bb.range_grid_risk(beng, bst, {"BTCUSDT": 94.0})
    bb.manage_range_limits(beng, bst, {"BTCUSDT": 94.0, "ETHUSDT": 100.0},
                           allowed)
    beng.sync_entry_orders({"BTCUSDT": 94.0, "ETHUSDT": 100.0})
    check("bot limit: bien vo -> huy het lenh cho cua symbol",
          not [o for o in bst["entry_orders"] if o["symbol"] == "BTCUSDT"],
          bst["entry_orders"])
    check("bot limit: lot da khop van giu (chay toi TP/SL)",
          [p["level"] for p in bst["positions"]] == ["rb1"])
    # halt -> huy tat ca
    bb.cancel_all_entries(beng, bst, "halt")
    beng.sync_entry_orders({"BTCUSDT": 94.0, "ETHUSDT": 100.0})
    check("bot limit: halt/pause -> huy moi lenh cho",
          bst["entry_orders"] == [])
    check("limit_mode: chi khi engine range + entry_mode limit",
          bb.limit_mode() and not (G.update(engine="classic")
                                   or bb.limit_mode()))
finally:
    bb.CFG.clear()
    bb.CFG.update(saved)
    bb.SCANNER = saved_sc
    bb.SYMBOLS[:] = saved_sym

flat = bot_config.defaults()
flat["grid.entry_mode"] = "limit"
check("config: limit + classic -> loi",
      any("LIMIT" in e for e in bot_config.validate(flat)[1]))
flat["grid.engine"] = "range"
check("config: limit + range hop le", not bot_config.validate(flat)[1],
      bot_config.validate(flat)[1])

for p in (tp._CFG_P, tp._UNI_P):
    if (p == tp._CFG_P and tp._MADE_CFG) or (p == tp._UNI_P and tp._MADE_UNI):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
