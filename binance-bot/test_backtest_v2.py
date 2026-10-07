#!/usr/bin/env python3
"""Test range_grid (logic thuan G4) + backtest_v2 (G3). Offline.

Run: python3 test_backtest_v2.py
"""
import json
import math
import os
import random
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import backtest_v2 as bt  # noqa: E402
import range_grid as rg  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + ("" if cond else "  " + str(detail)))


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


# ------------------------------------------------------------ range_grid
G = {"step_pct": 0.01, "tp_pct": 0.01, "sl_pct": 0.03, "levels_each_side": 5,
     "max_positions": 7, "max_lots_per_symbol": 4, "boundary_sl_buffer": 0.005,
     "break_buffer": 0.003, "trend_exit_adx": 25}
M = {"range_low": 95.0, "range_high": 105.0, "atr15_pct": None}
rng = rg.build_range(M, G, 0)
keys = [lv["key"] for lv in rng["levels"]]
check("build_range: long nua duoi, short nua tren",
      all((lv["side"] == "long") == (lv["price"] < rng["mid"])
          for lv in rng["levels"]))
check("build_range: moi tang nam trong bien",
      all(95 < lv["price"] < 105 for lv in rng["levels"]))
check("build_range: 4 tang moi phia (k=5 cham bien bi bo)",
      sorted(keys) == ["rb1", "rb2", "rb3", "rb4", "rs1", "rs2", "rs3", "rs4"],
      keys)
check("build_range: SL bien", close(rng["sl_long"], 95 * 0.995)
      and close(rng["sl_short"], 105 * 1.005))
check("build_range: bien hong -> None",
      rg.build_range({"range_low": 105, "range_high": 95}, G, 0) is None
      and rg.build_range({}, G, 0) is None)
check("grid_step: clamp theo ATR",
      rg.grid_step(0.001, {"step_mult": 1, "step_min": 0.004,
                           "step_max": 0.008}) == 0.004
      and rg.grid_step(0.02, {"step_mult": 1, "step_min": 0.004,
                              "step_max": 0.008}) == 0.008
      and rg.grid_step(None, {"step_pct": 0.006}) == 0.006)

sl, tp = rg.lot_exits(rng, "long", 99.0, G)
check("lot_exits long: SL bien (gan hon 3%), TP 1%",
      close(sl, max(95 * 0.995, 99 * 0.97)) and close(tp, 99.99))
wide = rg.build_range({"range_low": 80, "range_high": 120}, G, 0)
sl, tp = rg.lot_exits(wide, "short", 110.0, G)
check("lot_exits short: bien xa -> SL bi chan o sl_pct",
      close(sl, 110 * 1.03) and close(tp, 108.9))
check("lot_exits: tp_pct=0 -> 1 step",
      close(rg.lot_exits(rng, "long", 99.0, dict(G, tp_pct=0))[1], 99.99))

check("check_break: trong bien -> None",
      rg.check_break(rng, 100, {"adx_1h": 15}, G) is None)
check("check_break: thung day qua buffer",
      "đáy" in (rg.check_break(rng, 94.6, None, G) or ""))
check("check_break: trong buffer chua tinh vo",
      rg.check_break(rng, 94.8, None, G) is None)
check("check_break: vuot dinh", "đỉnh" in (rg.check_break(rng, 105.5, None,
                                                          G) or ""))
check("check_break: ADX 1h > nguong",
      "ADX" in (rg.check_break(rng, 100, {"adx_1h": 30}, G) or ""))

lc = rg.limit_candidates(rng, 100.0, {"rb1"})
check("limit_candidates: bo tang da co lot, gan gia truoc",
      [x["key"] for x in lc][:3] == ["rs1", "rb2", "rs2"], lc[:3])
check("limit_candidates: long phai duoi gia (GTX)",
      all(x["key"] != "rb1" for x in rg.limit_candidates(rng, 98.99, set())))
mt = rg.market_triggers(rng, 97.5, set())
check("market_triggers: tang bi cat qua, gan nhat truoc",
      [x["key"] for x in mt] == ["rb2", "rb1"], mt)
check("market_triggers: duoi SL bien -> khong vao",
      rg.market_triggers(rng, 94.0, set()) == [])


def cand(*pairs):
    return [{"key": k, "dist": d} for k, d in pairs]


plan = rg.plan_slots([
    {"symbol": "A", "score": 50, "lots": 0,
     "candidates": cand(("rb1", .01), ("rs1", .01), ("rb2", .02)),
     "pending": set()},
    {"symbol": "B", "score": 90, "lots": 0,
     "candidates": cand(("rb1", .005), ("rs1", .02)), "pending": set()},
], dict(G, max_positions=3), 0)
check("plan_slots: tran tong 3", len(plan) == 3, plan)
check("plan_slots: tang gan gia nhat truoc", plan[0] == ("B", "rb1"), plan)
plan = rg.plan_slots([
    {"symbol": "A", "score": 50, "lots": 1, "candidates": cand(("rb2", .02)),
     "pending": set()},
    {"symbol": "B", "score": 90, "lots": 0,
     "candidates": cand(("rb1", .001)), "pending": set()},
], dict(G, max_symbols=1), 1)
check("plan_slots: max_symbols -> symbol moi bi chan khi da du",
      plan == [("A", "rb2")], plan)
plan = rg.plan_slots([
    {"symbol": "A", "score": 50, "lots": 3,
     "candidates": cand(("rb2", .02), ("rs1", .01)), "pending": set()},
], G, 3)
check("plan_slots: max_lots_per_symbol tinh ca lot dang mo",
      plan == [("A", "rs1")], plan)
plan = rg.plan_slots([
    {"symbol": "A", "score": 50, "lots": 0,
     "candidates": cand(("rb1", .001), ("rb3", .03)), "pending": {"rb3"}},
], dict(G, max_positions=1), 0)
check("plan_slots: uu tien giu lenh cho dang co (tranh huy/dat lai)",
      plan == [("A", "rb3")], plan)
check("plan_slots: dat tran -> rong (huy het lenh cho)",
      rg.plan_slots([{"symbol": "A", "score": 1, "lots": 0,
                      "candidates": cand(("rb1", .01)), "pending": {"rb1"}}],
                    G, 7) == [])
lots = [{"side": "long", "entry": 100}, {"side": "short", "entry": 100},
        {"side": "long", "entry": 97.5}]
check("derisk: tat mac dinh", rg.derisk_targets(lots, 97, G) == [])
dr = rg.derisk_targets(lots, 97, dict(G, derisk_on_trend=True))
check("derisk: chi lot lo > 1%", dr == [lots[0]], dr)

# ------------------------------------------------------------- Agg/scan
bars = [{"ts": i * bt.BAR_MS, "o": 100, "h": 101, "l": 99, "c": 100,
         "v": 1} for i in range(30)]
a = bt.Agg(bars, bt.H1_MS)
check("Agg: nen 1h chi tinh dong khi sang gio moi",
      a.completed_count[11] == 0 and a.completed_count[12] == 1
      and a.completed_count[24] == 2)


def ou_bars(n, seed, theta=0.01, sigma=0.0018, drift=0.0, start=100.0):
    r = random.Random(seed)
    x = 0.0
    out = []
    p = start
    for i in range(n):
        o = p
        x += -theta * x + drift + r.gauss(0, sigma)
        p = start * math.exp(x)
        hi = max(o, p) * (1 + abs(r.gauss(0, 0.0006)))
        lo = min(o, p) * (1 - abs(r.gauss(0, 0.0006)))
        out.append({"ts": i * bt.BAR_MS, "o": o, "h": hi, "l": lo, "c": p,
                    "v": 1})
    return out


DAYS = 12
N = DAYS * 288
ou = ou_bars(N, 1)
scans = bt.compute_scans(ou, 60, 48, 100)
check("compute_scans: co moc quet sau khi du 100 nen 1h",
      scans and min(scans) >= 100 * bt.H1_MS
      and all(t % bt.H1_MS == 0 for t in scans))
t0 = sorted(scans)[5]
mut = [dict(b) for b in ou]
for b in mut:
    if b["ts"] >= t0:
        b["c"] = b["o"] = b["h"] = b["l"] = 500.0
check("compute_scans: khong lookahead (sua nen tu t tro di khong doi scan t)",
      bt.compute_scans(mut, 60, 48, 100)[t0] == scans[t0])

with tempfile.TemporaryDirectory() as d:
    cache = os.path.join(d, "scan.json")
    s1 = bt.scan_all({"X": ou}, 60, 48, 100, cache, log=lambda *_: None)
    orig = bt.compute_scans
    bt.compute_scans = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("khong duoc tinh lai"))
    try:
        s2 = bt.scan_all({"X": ou}, 60, 48, 100, cache, log=lambda *_: None)
        ok = s2 == s1
    except AssertionError:
        ok = False
    finally:
        bt.compute_scans = orig
    check("scan_all: doc lai cache khi du lieu khong doi", ok)

# -------------------------------------------------------------- sim exact
CFG = {"fee_rate": 0.0005, "fee_maker": 0.0002, "slippage": 0.0001,
       "order_margin_usdt": 100, "leverage": 10, "max_total_positions": 10,
       "start_equity": 1000.0, "grid": dict(G),
       "risk": {"grid_basket_max_loss_pct": 0.02,
                "grid_total_max_loss_pct": 0.10, "daily_max_loss_pct": 0.10,
                "max_notional_mult": 10},
       "scanner": {"top_k": 5}}


def mk(rows, t0=0):
    return [{"ts": t0 + i * bt.BAR_MS, "o": o, "h": h, "l": l, "c": c,
             "v": 1} for i, (o, h, l, c) in enumerate(rows)]


def sim(rows, cfg=None, mode="limit", syms=("A",)):
    data = {s: mk(rows) for s in syms}
    fixed = {s: rg.build_range(M, (cfg or CFG)["grid"], 0) for s in syms}
    return bt.PortfolioSim(data, {}, cfg or CFG, mode, fixed=fixed)


s = sim([(100, 100, 98.9, 99.2), (99.2, 100.2, 99.2, 100.1)])
r = s.run()
tp = [t for t in r and s.trades if t["reason"] == "TP"]
qty = 1000 / 99
exp = (99.99 * 0.9999 - 99) * qty - 99.99 * 0.9999 * qty * 0.0005 - 0.2
check("sim limit: khop maker rb1 roi TP 1% (net ~ $9.2)",
      len(tp) == 1 and close(tp[0]["pnl"], exp, 1e-6) and tp[0]["maker_entry"],
      (tp, exp))
check("sim limit: lenh cho = 4 tang gan gia (max_lots_per_symbol)",
      s.counters["orders_placed"] >= 4 and s.counters["orders_filled"] == 1,
      s.counters)
check("sim limit: dong END + huy cho, equity = initial + sum pnl",
      close(r["final_equity"], 1000 + sum(t["pnl"] for t in s.trades)),
      r["final_equity"])

s = sim([(100, 100, 99.0, 99.2)])
s.run()
check("sim limit: cham dung gia (khong xuyen fill_through) -> khong khop",
      s.counters["orders_filled"] == 0, s.counters)

s = sim([(100, 100, 98.9, 99.2), (99.2, 100.2, 99.2, 100.1)], mode="market")
r = s.run()
tp = [t for t in s.trades if t["reason"] == "TP"]
e = 99 * 1.0001
q = 1000 / e
exp = (e * 1.01 * 0.9999 - e) * q - e * 1.01 * 0.9999 * q * 0.0005 - 0.5
check("sim market: vao taker + truot, TP",
      len(tp) == 1 and close(tp[0]["pnl"], exp, 1e-6)
      and not tp[0]["maker_entry"], (tp, exp))
check("sim market: limit lai hon market cung duong gia",
      exp < (99.99 * 0.9999 - 99) * (1000 / 99)
      - 99.99 * 0.9999 * (1000 / 99) * 0.0005 - 0.2)

s = sim([(100, 100, 98.5, 98.6), (98.6, 98.6, 95.6, 95.7),
         (95.7, 100, 95.7, 99.9)])
r = s.run()
bk = [t for t in s.trades if t["reason"] == "GRID_BASKET_STOP"]
q1, q2 = 1000 / 99, 1000 / 98
# nguong = 2% mark equity luc mo nen 2 (rb1 dang lo, da tru phi maker)
lim = 0.02 * (1000 - 0.2 + (98.6 - 99) * q1)
pstar = (1000 + 1000 - lim) / (q1 + q2)
check("sim basket: dong dung tai gia cham -2% equity",
      len(bk) == 2 and close(bk[0]["exit"], pstar * 0.9999, 1e-6),
      (bk, pstar))
check("sim basket: symbol risk_halted, khong vao lai trong ngay",
      s.sym["A"]["risk_halted"] and s.counters["orders_filled"] == 2
      and not any(t["reason"] == "TP" for t in s.trades), s.counters)

nob = json.loads(json.dumps(CFG))
nob["risk"]["grid_basket_max_loss_pct"] = 0
s = sim([(100, 100, 98.5, 98.6), (98.6, 98.6, 94.0, 94.2),
         (94.2, 100.5, 94.2, 100.4)], cfg=nob)
r = s.run()
reasons = sorted(t["reason"] for t in s.trades)
check("sim break: SL bien cua 2 lot, bien vo -> huy cho, khong vao lai",
      reasons == ["SL", "SL"] and s.counters["breaks"] == 1
      and s.sym["A"]["broken"] and s.counters["orders_filled"] == 2,
      (reasons, s.counters))
sl_px = sorted(t["exit"] for t in s.trades)
check("sim break: SL = max(bien, entry x (1-3%))",
      close(sl_px[0], 98 * 0.97 * 0.9999) and close(sl_px[1],
                                                    99 * 0.97 * 0.9999),
      sl_px)

der = json.loads(json.dumps(nob))
der["grid"]["derisk_on_trend"] = True
der["grid"]["sl_pct"] = 0.10
der["grid"]["boundary_sl_buffer"] = 0.05
s = sim([(100, 100, 98.5, 98.6), (98.6, 98.6, 94.6, 94.7)], cfg=der)
s.run()
check("sim derisk: bien vo -> cat lot lo > 1%",
      sorted(t["reason"] for t in s.trades) == ["GRID_DERISK", "GRID_DERISK"],
      [t["reason"] for t in s.trades])

slot = json.loads(json.dumps(CFG))
slot["grid"]["max_positions"] = 3
slot["grid"]["max_symbols"] = 1
s = sim([(100, 100.5, 99.5, 100)], cfg=slot, syms=("A", "B"))
s.base_equity = 1000
for x in ("A", "B"):
    s.sym[x]["last"] = 100.0
s._plan_limits(0, {"A": 100.0, "B": 100.0})
pend = {x: len(s.sym[x]["pending"]) for x in ("A", "B")}
check("sim slot: tran 3 lenh, max_symbols=1 -> 1 symbol",
      sorted(pend.values()) == [0, 3], pend)

# ----------------------------------------------- study / walk-forward / cli
data = {"OU1": ou_bars(N, 11), "OU2": ou_bars(N, 12),
        "TR": ou_bars(N, 13, theta=0.0, drift=0.00025)}
sc_all = {k: bt.compute_scans(v, 60, 48, 100) for k, v in data.items()}
cfg = json.loads(json.dumps(CFG))
cfg["scanner"].update({"bbw_min_pct": 0.0, "range_min_pct": 0.0})
st = bt.scanner_study(data, sc_all, cfg, horizon_hours=24, every_hours=12)
check("study: co mau + nhom dat/truot + bucket + ket luan",
      st["samples"] > 0 and "n" in st["passed"] and "n" in st["failed"]
      and len(st["score_buckets"]) == 5 and st["verdict"], st["samples"])
tr = [r for r in st["rows"] if r["symbol"] == "TR"]
ou_rows = [r for r in st["rows"] if r["symbol"] != "TR"]
check("study: mau trend lo hon mau di ngang (TB)",
      sum(r["pnl"] for r in tr) / max(1, len(tr))
      < sum(r["pnl"] for r in ou_rows) / max(1, len(ou_rows)),
      (len(tr), len(ou_rows)))
check("study: trend hiem khi dat scanner",
      sum(r["passed"] for r in tr) <= sum(r["passed"] for r in ou_rows))

wf = bt.walk_forward(data, sc_all, cfg, train_days=3, test_days=2,
                     space={"grid.tp_pct": (0.008, 0.012),
                            "grid.step_mult": (1.0,)}, log=lambda *_: None)
check("walk_forward: co fold, tham so chon nam trong khong gian",
      wf["folds"] and all(f["chosen"]["grid.tp_pct"] in (0.008, 0.012)
                          for f in wf["folds"]), len(wf["folds"]))
check("candidate_cfgs: tich Descartes",
      len(bt.candidate_cfgs(CFG, bt.DEFAULT_SPACE)) == 9)

r_lim = bt.PortfolioSim(data, sc_all, cfg, "limit").run()
r_mkt = bt.PortfolioSim(data, sc_all, cfg, "market").run()
check("portfolio: chay du, tran lot khong vuot max_positions",
      r_lim["trades"] >= 0 and r_mkt["trades"] >= 0
      and r_lim["counters"]["ranges_built"] > 0, r_lim["counters"])
check("portfolio: phi limit/lenh thap hon market",
      (r_lim["fees"] / max(1, r_lim["trades"]))
      < (r_mkt["fees"] / max(1, r_mkt["trades"])),
      (r_lim["fees"], r_lim["trades"], r_mkt["fees"], r_mkt["trades"]))


class Peak(bt.PortfolioSim):
    peak = 0

    def _open(self, *a, **k):
        p = super()._open(*a, **k)
        Peak.peak = max(Peak.peak, len(self.positions)
                        + sum(len(x["pending"]) for x in self.sym.values()))
        return p


Peak(data, sc_all, cfg, "limit").run()
check("portfolio: lot + lenh cho khong vuot max_positions",
      0 < Peak.peak <= CFG["grid"]["max_positions"], Peak.peak)

with tempfile.TemporaryDirectory() as d:
    for k, v in data.items():
        with open(bt.data_path(d, k), "w") as f:
            for b in v:
                f.write(json.dumps(b) + "\n")
    out = os.path.join(d, "r.json")
    rc = bt.main(["run", "--data-dir", d, "--min-h1", "100",
                  "--set", "scanner.bbw_min_pct=0", "--set",
                  "grid.tp_pct=0.012", "--scan-cache",
                  os.path.join(d, "c.json"), "--json-out", out])
    res = json.load(open(out))
    check("cli run: ghi report JSON", rc == 0 and "net_per_trade" in res)
    rc = bt.main(["study", "--data-dir", d, "--min-h1", "100",
                  "--scan-cache", os.path.join(d, "c.json"),
                  "--horizon-hours", "12", "--every-hours", "24",
                  "--json-out", out])
    check("cli study: dung lai cache, ghi report",
          rc == 0 and "verdict" in json.load(open(out)))

# ------------------------------------------- range_min_levels (phuong an C)
G1 = dict(G, levels_each_side=2, step_min=0.01, step_max=0.015,
          step_mult=0.8)


def mm(w, atr=None):
    return {"range_low": 100 * (1 - w / 2), "range_high": 100 * (1 + w / 2),
            "atr15_pct": atr}


check("levels_per_side: bien 2.5%, step 1% -> 1 tang",
      rg.levels_per_side(mm(0.025), G1) == 1)
check("levels_per_side: bien 2.5%, step 1.5% (ATR 2%) -> 0 tang",
      rg.levels_per_side(mm(0.025, 0.02), G1) == 0)
check("levels_per_side: bien 4.0% step 1% -> 1 (dung bien chua co tang 2)",
      rg.levels_per_side(mm(0.040), G1) == 1)
check("levels_per_side: bien 4.5% step 1% -> 2",
      rg.levels_per_side(mm(0.045), G1) == 2)
check("levels_per_side: thieu bien -> 0", rg.levels_per_side({}, G1) == 0)
check("range_tradable mac dinh (1): chi loai 0 tang",
      rg.range_tradable(mm(0.025), G1)
      and not rg.range_tradable(mm(0.025, 0.02), G1))
check("range_min_levels=2: bien 4% bi loai, 4.5% dat",
      not rg.range_tradable(mm(0.04), dict(G1, range_min_levels=2))
      and rg.range_tradable(mm(0.045), dict(G1, range_min_levels=2)))
check("range_min_levels > levels_each_side -> chan ve levels_each_side",
      rg.min_levels_required(dict(G1, range_min_levels=9)) == 2)

_judge = bt.scanner.judge
try:
    # judge gia: raw = (passed, score, metrics, reasons)
    bt.scanner.judge = lambda raw, sc: raw
    scans = {"A": {0: (True, 90, mm(0.025, 0.02), [])},   # 0 tang
             "B": {0: (True, 80, mm(0.05), [])},
             "C": {0: (True, 70, mm(0.03), [])},
             "D": {0: (False, 99, mm(0.05), [])}}
    cfg = dict(CFG, grid=dict(G1), scanner={"top_k": 2})
    ps = bt.PortfolioSim({k: mk([(100, 100, 100, 100)]) for k in scans},
                         scans, cfg, "market")
    ps._update_scans(0)
    check("backtest top K: bo symbol 0 tang, nhuong cho symbol xep sau",
          ps.allowed == ["B", "C"], ps.allowed)
    cfg["grid"]["range_min_levels"] = 2
    ps = bt.PortfolioSim({k: mk([(100, 100, 100, 100)]) for k in scans},
                         scans, cfg, "market")
    ps._update_scans(0)
    check("backtest top K: range_min_levels=2 chi giu bien du 2 tang",
          ps.allowed == ["B"], ps.allowed)
finally:
    bt.scanner.judge = _judge

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
