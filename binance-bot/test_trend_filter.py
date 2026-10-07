#!/usr/bin/env python3
"""Test loc chieu xu huong grid (task 34): trend_filter.bias / TrendFilter
+ tich hop binance_bot (classic, range market, range limit). Offline.

Run: python3 test_trend_filter.py
"""
import copy
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "..", "db"))
_cfg_p = os.path.join(BASE, "config.json")
if not os.path.exists(_cfg_p):
    import shutil
    shutil.copy(os.path.join(BASE, "config.example.json"), _cfg_p)
_uni_p = os.path.join(BASE, "universe.json")
if not os.path.exists(_uni_p):
    json.dump([{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}],
              open(_uni_p, "w"))
for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
    os.environ.pop(k, None)

import binance_bot as bb  # noqa: E402
import bot_config  # noqa: E402
import scanner as sc  # noqa: E402
import trend_filter as tf  # noqa: E402

PASS, FAIL = [], []
H = 3600 * 1000


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + ("" if cond else "  " + str(detail)))


def series(closes, start_ms=0):
    """Nen 1h tu danh sach gia dong (+1 nen dang chay o cuoi)."""
    out = []
    for i, c in enumerate(closes):
        out.append({"ts": start_ms + i * H, "o": c, "h": c * 1.001,
                    "l": c * 0.999, "c": c})
    return out


def trend(n, start, step_pct):
    out, p = [], start
    for _ in range(n):
        out.append(p)
        p *= 1 + step_pct
    return out


CFG = dict(tf.DEFAULTS)
FLAT = series([100.0 + (0.3 if i % 2 else -0.3) for i in range(99)])
DOWN = series(trend(99, 100.0, -0.002))     # -0.2%/h
UP = series(trend(99, 100.0, 0.002))

# ------------------------------------------------------------- bias()
b = tf.bias(FLAT, 99.0, CFG)
check("bias: di ngang (EMA phang) -> neutral ke ca khi gia vua giam 1%",
      b and b["bias"] == "neutral", b)
b = tf.bias(DOWN, None, CFG)
check("bias: duoi EMA + EMA doc xuong -> down", b and b["bias"] == "down"
      and "EMA" in b["reason"], b)
b = tf.bias(UP, None, CFG)
check("bias: tren EMA + EMA doc len -> up", b and b["bias"] == "up", b)
b = tf.bias(DOWN, 200.0, CFG)
check("bias: EMA doc xuong nhung gia da vuot len tren EMA -> neutral",
      b and b["bias"] == "neutral", b)
check("bias: thieu nen -> None", tf.bias(series([100.0] * 40), 100.0, CFG)
      is None)
weak = dict(CFG, slope_min_atr=50)
b = tf.bias(DOWN, None, weak)
check("bias: doc yeu hon slope_min_atr -> neutral", b["bias"] == "neutral",
      b)
# market: dump nhanh khi EMA con phang
last = FLAT[-2]["c"]
b = tf.bias(FLAT, last * 0.98, CFG, market=True)
check("bias market: giam 2% trong 4h (EMA phang) -> down ngay",
      b["bias"] == "down" and "giảm" in b["reason"]
      and b["move_pct"] < -1.5, b)
b = tf.bias(FLAT, last * 1.02, CFG, market=True)
check("bias market: tang 2% trong 4h -> up", b["bias"] == "up", b)
b = tf.bias(FLAT, last * 0.98, CFG, market=False)
check("bias symbol: KHONG ap luat giam nhanh (grid can gia giam moi vao)",
      b["bias"] == "neutral", b)
b = tf.bias(FLAT, last * 0.98, dict(CFG, market_move_pct=0), market=True)
check("bias market: market_move_pct=0 -> tat luat giam nhanh",
      b["bias"] == "neutral", b)
rows = series([100.0] * 10)
check("closed_bars: bo nen dang chay theo now_ms",
      len(tf.closed_bars(rows, now_ms=rows[-1]["ts"] + 10)) == 9
      and len(tf.closed_bars(rows, now_ms=rows[-1]["ts"] + H)) == 10
      and len(tf.closed_bars(rows)) == 9)
check("blocked_side: down chan long, up chan short",
      tf.blocked_side("down") == "long" and tf.blocked_side("up") == "short"
      and tf.blocked_side("neutral") is None)

# ------------------------------------------------------- TrendFilter
NOW = [1_000_000.0]
DATA = {"BTCUSDT": DOWN, "ETHUSDT": FLAT, "SOLUSDT": FLAT}
calls, logs = [], []


def fetch(sym, interval, limit):
    calls.append((sym, interval, limit))
    if sym == "BOOM":
        raise RuntimeError("timeout")
    return DATA[sym]


cfg = {"trend": {}}
T = tf.TrendFilter(cfg, fetch, log=logs.append, clock=lambda: NOW[0])
check("TrendFilter: chua co du lieu -> chan ca 2 phia (fail-closed)",
      T.blocks("ETHUSDT", "long") and T.blocks("ETHUSDT", "short"))
T.tick(["ETHUSDT", "SOLUSDT"], {}, max_per_tick=2)
check("TrendFilter: lay market symbol truoc, limit 99 (weight 1)",
      calls[0] == ("BTCUSDT", "1h", 99) and len(calls) == 2, calls)
T.tick(["ETHUSDT", "SOLUSDT"], {}, max_per_tick=2)
check("TrendFilter: tick sau lay not symbol con lai, khong lay lai som",
      [c[0] for c in calls] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"], calls)
why = T.blocks("ETHUSDT", "long")
check("A: BTC giam -> chan long tren coin khac", why and "BTC" in why
      and "giảm" in why, why)
check("A: BTC giam -> van cho short", T.blocks("ETHUSDT", "short") is None)
check("log doi bias cua thi truong",
      any("BTCUSDT (thi truong)" in m and "down" in m for m in logs), logs)
cfg["trend"]["market_filter"] = False
check("tat market_filter -> coin di ngang mo ca 2 phia",
      T.blocks("ETHUSDT", "long") is None
      and T.blocks("ETHUSDT", "short") is None)
DATA["SOLUSDT"] = DOWN
NOW[0] += 301
T.tick(["ETHUSDT", "SOLUSDT"], {}, max_per_tick=5)
why = T.blocks("SOLUSDT", "long")
check("C: coin tu giam -> chan long tren coin do", why and "SOLUSDT" in why,
      why)
check("C: coin khac khong bi anh huong", T.blocks("ETHUSDT", "long") is None)
cfg["trend"]["symbol_filter"] = False
check("tat ca 2 lop -> khong chan gi", T.blocks("SOLUSDT", "long") is None)
check("tat ca 2 lop -> khong lay nen", T.needed(["ETHUSDT"]) == [])
cfg["trend"].update(market_filter=True, symbol_filter=True)
NOW[0] += T.max_age() + 1
check("du lieu het han -> chan (fail-closed)",
      "thiếu dữ liệu" in (T.blocks("ETHUSDT", "short") or ""))
DATA["BOOM"] = FLAT
n0 = len(calls)
T.tick(["BOOM"], {}, max_per_tick=5)
check("fetch loi -> log, thu lai sau 60s (khong spam)",
      any("BOOM" in m and "loi" in m for m in logs)
      and T._retry.get("BOOM", 0) > NOW[0], logs[-3:])
T.tick(["BOOM"], {}, max_per_tick=5)
check("fetch loi: chua het 60s -> khong goi lai BOOM",
      [c for c in calls[n0:] if c[0] == "BOOM"].__len__() == 1)


class Stop(Exception):
    pass


def fetch_stop(*a):
    raise Stop()


T2 = tf.TrendFilter({}, fetch_stop, log=logs.append, clock=lambda: NOW[0],
                    fatal=(Stop,))
try:
    T2.tick(["ETHUSDT"], {})
    check("loi safety (fatal) duoc nem len", False)
except Stop:
    check("loi safety (fatal) duoc nem len", True)
# gia market symbol khong co trong universe -> dung gia nen dang chay
T3 = tf.TrendFilter({"trend": {"symbol_filter": False}},
                    lambda *a: FLAT[:-1] + [dict(FLAT[-1], c=FLAT[-2]["c"]
                                                 * 0.97)],
                    log=lambda m: None, clock=lambda: NOW[0])
T3.tick(["ETHUSDT"], {})
check("market symbol ngoai universe: lay gia nen dang chay -> bat dump",
      T3.status["BTCUSDT"]["bias"] == "down", T3.status.get("BTCUSDT"))
snap = T.snapshot()
check("snapshot cho dashboard", snap["market"] == "BTCUSDT"
      and "symbols" in snap and json.dumps(snap))


# --------------------------------------------- tich hop binance_bot
class FakeEngine:
    def __init__(self, st):
        self.st, self.opens, self.n = st, [], 100

    def open(self, symbol, side, notional, price, sl_pct, tp_pct, tag,
             level=None):
        self.n += 1
        pos = {"id": self.n, "symbol": symbol, "side": side, "entry": price,
               "qty": notional / price, "notional": notional, "tag": tag,
               "level": level}
        self.opens.append(pos)
        self.st["positions"].append(pos)
        return pos, None


class StubTrend:
    def __init__(self, blocked):
        self.blocked = blocked           # {(symbol, side): reason}

    def blocks(self, symbol, side):
        return self.blocked.get((symbol, side)) or self.blocked.get(
            ("*", side))


def gstate(symbol, anchor=100.0):
    return {"positions": [], "grids": {symbol: {"anchor": anchor,
                                                "taken": {}, "step": 0.01}},
            "regimes": {}}


saved = copy.deepcopy(bb.CFG)
saved_tr, saved_sc, saved_log = bb.TREND, bb.SCANNER, bb.log
saved_sym = list(bb.SYMBOLS)
blog = []
try:
    bb.log = blog.append
    bb.SCANNER = None
    bb.CFG.update(order_margin_usdt=100, leverage=10, max_total_positions=10)
    bb.CFG["grid"].update(engine="classic", levels_each_side=2,
                          max_positions=5, max_entries_per_cycle=1,
                          max_symbols=0, range_steps=6, step_pct=0.01)
    bb.CFG["grid"]["max_same_side"] = 0
    bb.TREND = StubTrend({("*", "long"): "BTC xu hướng giảm (test)"})
    st = gstate("ETHUSDT")
    eng = FakeEngine(st)
    bb.manage_grid(eng, st, "ETHUSDT", 98.9)          # cham b1
    check("classic: BTC giam -> KHONG mo long b1", eng.opens == [],
          eng.opens)
    check("classic: log ly do chan 1 lan",
          sum("khong mo LONG" in m for m in blog) == 1, blog)
    bb.manage_grid(eng, st, "ETHUSDT", 98.8)
    check("classic: khong log lai khi ly do khong doi",
          sum("khong mo LONG" in m for m in blog) == 1, blog)
    st2 = gstate("ETHUSDT")
    eng2 = FakeEngine(st2)
    bb.manage_grid(eng2, st2, "ETHUSDT", 101.1)       # cham s1
    check("classic: BTC giam -> van mo short s1",
          [p["side"] for p in eng2.opens] == ["short"], eng2.opens)
    bb.TREND = StubTrend({})
    bb.manage_grid(eng, st, "ETHUSDT", 98.9)
    check("classic: het chan -> mo lai long b1 (tang van hop le)",
          [p["level"] for p in eng.opens] == ["b1"], eng.opens)
    check("classic: log mo lai", any("mo lai phia LONG" in m for m in blog))
    bb.TREND = None
    st3 = gstate("ETHUSDT")
    eng3 = FakeEngine(st3)
    bb.manage_grid(eng3, st3, "ETHUSDT", 98.9)
    check("TREND=None (test/khong khoi tao) -> hanh vi cu",
          len(eng3.opens) == 1)

    # range engine, vao market
    NOWR = [2_000_000.0]
    bb.SYMBOLS[:] = ["BTCUSDT", "ETHUSDT"]
    bb.CFG["grid"].update(engine="range", entry_mode="market", step_min=0.01,
                          step_max=0.01, tp_pct=0.01, sl_pct=0.03,
                          max_lots_per_symbol=2, range_min_levels=1,
                          boundary_sl_buffer=0.005, break_buffer=0.003,
                          trend_exit_adx=25.0)
    bb.CFG["scanner"].update(enabled=True, top_k=2, rescan_minutes=15)
    bb.SCANNER = sc.ScannerRunner(bb.CFG, lambda *a: [],
                                  clock=lambda: NOWR[0])
    bb.SCANNER.results["ETHUSDT"] = {
        "symbol": "ETHUSDT", "ts": NOWR[0], "passed": True, "score": 70,
        "reasons": [], "metrics": {"range_low": 95.0, "range_high": 105.0,
                                   "adx_1h": 15, "atr15_pct": None,
                                   "range_pct": 105 / 95 - 1}}
    allowed = bb.range_allowed_symbols()
    bb.TREND = StubTrend({("ETHUSDT", "long"): "ETHUSDT xu hướng giảm"})
    sr = {"positions": [], "grids": {}, "regimes": {}}
    er = FakeEngine(sr)
    bb.manage_range_grid(er, sr, "ETHUSDT", 98.9, allowed)
    check("range market: coin giam -> khong mo long nua duoi", er.opens == [],
          er.opens)
    bb.manage_range_grid(er, sr, "ETHUSDT", 101.1, allowed)
    check("range market: phia short van mo",
          [p["side"] for p in er.opens] == ["short"], er.opens)
finally:
    bb.CFG.clear()
    bb.CFG.update(saved)
    bb.TREND, bb.SCANNER, bb.log = saved_tr, saved_sc, saved_log
    bb.SYMBOLS[:] = saved_sym

# range limit: lenh cho phia bi chan -> huy voi ly do loc xu huong
import live_binance  # noqa: E402

saved = copy.deepcopy(bb.CFG)
saved_tr, saved_sc, saved_log = bb.TREND, bb.SCANNER, bb.log
saved_sym = list(bb.SYMBOLS)
try:
    bb.log = lambda m: None
    NOWL = [3_000_000.0]
    bb.SYMBOLS[:] = ["BTCUSDT", "ETHUSDT"]
    bb.CFG.update(order_margin_usdt=100, leverage=10, max_total_positions=10)
    bb.CFG["grid"].update(engine="range", entry_mode="limit", step_pct=0.01,
                          step_min=0.01, step_max=0.01, tp_pct=0.01,
                          sl_pct=0.03, levels_each_side=3, max_positions=6,
                          max_lots_per_symbol=6, max_symbols=0,
                          max_new_orders_per_cycle=6, range_min_levels=1,
                          limit_min_gap_pct=0.0005, entry_ttl_minutes=60,
                          partial_fill_timeout_seconds=60)
    bb.CFG["grid"]["max_same_side"] = 0
    bb.CFG["scanner"].update(enabled=True, top_k=2, rescan_minutes=15)
    bb.SCANNER = sc.ScannerRunner(bb.CFG, lambda *a: [],
                                  clock=lambda: NOWL[0])
    bb.SCANNER.results["ETHUSDT"] = {
        "symbol": "ETHUSDT", "ts": NOWL[0], "passed": True, "score": 70,
        "reasons": [], "metrics": {"range_low": 95.0, "range_high": 105.0,
                                   "adx_1h": 15, "atr15_pct": None,
                                   "range_pct": 105 / 95 - 1}}
    bb.TREND = StubTrend({})
    lst = {"equity": 10000.0, "mark_equity": 10000.0, "positions": [],
           "grids": {}, "_pid": 0,
           "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0}}
    leng = live_binance.BinanceEngine(bb.CFG, lst, dry_run=True,
                                      log=lambda m: None)
    allowed = bb.range_allowed_symbols()
    prices = {"BTCUSDT": 100.0, "ETHUSDT": 100.0}
    bb.manage_range_grid(leng, lst, "ETHUSDT", 100.0, allowed)
    bb.manage_range_limits(leng, lst, prices, allowed)
    sides = sorted(o["side"] for o in lst["entry_orders"])
    check("range limit: trung tinh -> dat lenh cho ca 2 phia",
          "long" in sides and "short" in sides, sides)
    bb.TREND = StubTrend({("*", "long"): "BTC xu hướng giảm (x)"})
    cancelled = []
    orig = leng.cancel_entry
    leng.cancel_entry = lambda o, why: (cancelled.append((o["side"], why)),
                                        orig(o, why))[1]
    bb.manage_range_limits(leng, lst, prices, allowed)
    leng.sync_entry_orders(prices)
    left = sorted(o["side"] for o in lst["entry_orders"])
    check("range limit: BTC giam -> huy lenh cho LONG, giu short",
          "long" not in left and "short" in left, left)
    check("range limit: ly do huy ghi ro loc xu huong",
          cancelled and all(s == "long" and "xu hướng" in w
                            for s, w in cancelled), cancelled)
finally:
    bb.CFG.clear()
    bb.CFG.update(saved)
    bb.TREND, bb.SCANNER, bb.log = saved_tr, saved_sc, saved_log
    bb.SYMBOLS[:] = saved_sym

# ----------------------------------------------------------- config
flat = bot_config.defaults()
clean, errs = bot_config.validate(flat)
check("config: mac dinh hop le + co nhom Xu hướng",
      not errs and "Xu hướng" in bot_config.GROUPS
      and clean["trend.market_filter"] is True
      and clean["trend.symbol_filter"] is True, errs)
flat2 = dict(flat, **{"trend.ema_period": 90, "trend.slope_bars": 20})
check("config: EMA + so nen doc > 98 -> loi",
      bot_config.validate(flat2)[1])
ex = json.load(open(os.path.join(BASE, "config.example.json")))
check("config.example: muc trend khop schema",
      all(ex["trend"][k.split(".", 1)[1]] == p.default
          for k, p in bot_config.PARAM_BY_KEY.items()
          if k.startswith("trend.")))
check("config.example: muc trend khop trend_filter.DEFAULTS",
      all(ex["trend"][k] == v for k, v in tf.DEFAULTS.items()))
live_cfg = {"trend": {}}
bot_config.apply(live_cfg, {"trend.market_filter": False})
check("config: ap dung nong vao CFG['trend'] (TrendFilter doc truc tiep)",
      tf.tcfg(live_cfg)["market_filter"] is False)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
