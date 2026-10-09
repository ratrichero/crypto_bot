#!/usr/bin/env python3
"""Test grid_diag (task 42): ly do grid khong mo lot + doi chieu voi
binance_bot.manage_grid that (ngau nhien), scanner.block_reason, CLI chi doc,
grid_diag_tick trong bot. Offline.

Run: python3 test_grid_diag.py
"""
import copy
import io
import json
import os
import random
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "..", "db"))
_cfg_p = os.path.join(BASE, "config.json")
if not os.path.exists(_cfg_p):
    shutil.copy(os.path.join(BASE, "config.example.json"), _cfg_p)
_uni_p = os.path.join(BASE, "universe.json")
if not os.path.exists(_uni_p):
    json.dump([{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}],
              open(_uni_p, "w"))
for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
    os.environ.pop(k, None)

import binance_bot as bb  # noqa: E402
import grid_diag as gd  # noqa: E402
import scanner as sc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + ("" if cond else "  " + str(detail)))


NOW = 1_800_000_000.0


def cfg_base(**grid):
    g = {"engine": "classic", "levels_each_side": 2, "max_positions": 3,
         "max_entries_per_cycle": 1, "max_symbols": 0, "range_steps": 6,
         "step_pct": 0.01, "max_same_side": 2}
    g.update(grid)
    return {"grid": g, "max_total_positions": 4, "disabled_symbols": {}}


def st_base(anchor=100.0):
    return {"positions": [], "regimes": {},
            "grids": {"ETHUSDT": {"anchor": anchor, "taken": {},
                                  "step": 0.01}}}


def one(st, cfg, px, **kw):
    d = gd.explain_classic(st, cfg, ["ETHUSDT"], {"ETHUSDT": px}, now=NOW,
                           **kw)
    return d, d["symbols"][0]


# ---------------------------------------------------------------- pure
d, r = one(st_base(), cfg_base(), 100.4)
check("cho gia: code wait_price + gap 2 phia",
      r["code"] == "wait_price" and abs(r["long_gap_pct"] - 1.394) < 0.01
      and abs(r["short_gap_pct"] - 0.598) < 0.01, r)
check("nearest = phia gan nhat (SHORT 0.6%)",
      d["nearest"] == {"symbol": "ETHUSDT", "side": "short",
                       "gap_pct": r["short_gap_pct"]}, d["nearest"])
check("summary_line co GRID WAIT + gan nhat",
      gd.summary_line(d).startswith("GRID WAIT 1 symbol")
      and "gần nhất ETHUSDT SHORT" in gd.summary_line(d), gd.summary_line(d))
_, r = one(st_base(), cfg_base(), 98.9)
check("cham b1, khong chan -> ready", r["code"] == "ready", r)
check("row co label tieng Viet (dashboard)", r["label"] == "sẵn sàng mở", r)
_, r = one(st_base(), cfg_base(), 98.9,
           trend_block=lambda s, side: "BTC xu hướng giảm (x)"
           if side == "long" else None)
check("cham b1 + trend chan LONG -> side_blocked + ly do",
      r["code"] == "side_blocked" and "BTC xu hướng giảm" in r["detail"], r)
d, r = one(st_base(), cfg_base(), 100.2,
           trend_block=lambda s, side: "BTC xu hướng giảm (x)"
           if side == "long" else None)
check("chua cham + LONG se bi chan -> ghi chu, nearest bo phia LONG",
      r["code"] == "wait_price" and "LONG sẽ bị chặn" in r["detail"]
      and d["nearest"]["side"] == "short", (r, d["nearest"]))
st = st_base()
st["regimes"]["ETHUSDT"] = {"regime": "trending", "adx": 31.2,
                            "candidate": "ranging", "candidate_count": 1}
_, r = one(st, cfg_base(), 98.9)
check("regime trending -> regime + ADX", r["code"] == "regime"
      and "31.2" in r["detail"] and "xác nhận" in r["detail"], r)
st = st_base()
st["grids"]["ETHUSDT"]["risk_halted"] = True
_, r = one(st, cfg_base(), 98.9)
check("risk_halted -> code risk_halted", r["code"] == "risk_halted", r)
_, r = one(st_base(), cfg_base(), 98.9,
           scanner_block=lambda s: "scanner trượt: ADX cao")
check("scanner chan -> scanner + ly do", r["code"] == "scanner"
      and "ADX cao" in r["detail"], r)
st = st_base()
st["positions"] = [{"tag": "grid", "symbol": "SOLUSDT", "side": "long"},
                   {"tag": "grid", "symbol": "XRPUSDT", "side": "short"}]
_, r = one(st, cfg_base(max_symbols=2), 98.9)
check("max_symbols day -> max_symbols", r["code"] == "max_symbols", r)
st["positions"].append({"tag": "grid", "symbol": "ADAUSDT", "side": "long"})
d, r = one(st, cfg_base(), 98.9)
check("du max_positions -> full + ly do toan cuc",
      r["code"] == "full" and any("max_positions" in x for x in d["global"]),
      (r, d["global"]))
d, r = one(st_base(), cfg_base(), 98.9,
           side_cap_block=lambda side: "trần 2 lot grid LONG"
           if side == "long" else None)
check("tran cung chieu -> side_blocked", r["code"] == "side_blocked"
      and "trần 2" in r["detail"], r)
st = st_base()
st["halted"], st["halt_reason"] = True, "daily stop -5%"
d, _ = one(st, cfg_base(), 100.0, paused=True)
check("halt + PAUSE -> ly do toan cuc", any("HALT" in x for x in d["global"])
      and any("PAUSE" in x for x in d["global"]), d["global"])
_, r = one(st_base(anchor=None), cfg_base(), 100.0)
check("anchor None -> bot dat anchor = gia: gap = 1 step",
      r["code"] == "wait_price" and abs(r["long_gap_pct"] - 1.0) < 1e-6, r)
_, r = one(st_base(anchor=110.0), cfg_base(), 100.0)
check("ra khoi bien (khong lot) -> anchor moi, khong frozen",
      r["code"] == "wait_price" and abs(r["short_gap_pct"] - 1.0) < 1e-6, r)
st = st_base(anchor=110.0)
st["positions"] = [{"tag": "grid", "symbol": "ETHUSDT", "side": "long",
                    "level": "b1"}]
_, r = one(st, cfg_base(), 100.0)
check("ra khoi bien con lot -> frozen", r["code"] == "frozen", r)
st = st_base()
st["grids"]["ETHUSDT"]["taken"] = {"b1": 1, "b2": 2, "s1": 3, "s2": 4}
_, r = one(st, cfg_base(), 100.0)
check("lay het tang -> levels_full", r["code"] == "levels_full", r)
st = st_base()
st["grids"]["ETHUSDT"]["taken"] = {"b1": 1}
_, r = one(st, cfg_base(), 98.9)
check("b1 da lay -> tang ke la b2 (gap > 0)",
      r["code"] == "wait_price" and abs(r["long_gap_pct"] - 0.9100) < 0.01, r)
_, r = one(st_base(), cfg_base(), None)
check("khong co gia -> no_data", r["code"] == "no_data", r)
d, r = one(st_base(), dict(cfg_base(), disabled_symbols={"ETHUSDT": NOW + 9}),
           98.9)
check("disabled -> disabled", r["code"] == "disabled", r)
d = gd.explain_classic(st_base(), cfg_base(engine="range"), ["ETHUSDT"],
                       {"ETHUSDT": 98.9}, now=NOW)
check("engine range -> chi ghi chu toan cuc, khong bang",
      d["symbols"] == [] and "range" in d["global"][0], d)
st0 = st_base()
snap = copy.deepcopy(st0)
one(st0, cfg_base(), 98.9)
check("explain_classic KHONG sua state", st0 == snap)

# ------------------------------------------------ scanner.block_reason
cfg_s = {"scanner": {"enabled": True, "mode": "filter", "top_k": 1,
                     "rescan_minutes": 15}}
runner = sc.ScannerRunner(cfg_s, fetch=None, log=lambda *_: None,
                          clock=lambda: NOW)
runner.results = {
    "AAA": {"symbol": "AAA", "passed": True, "score": 9, "ts": NOW - 60},
    "BBB": {"symbol": "BBB", "passed": True, "score": 5, "ts": NOW - 60},
    "CCC": {"symbol": "CCC", "passed": False, "score": 1, "ts": NOW - 60,
            "reasons": ["ADX 1h 31 > 20", "biên hẹp"]},
    "DDD": {"symbol": "DDD", "passed": True, "score": 9, "ts": NOW - 7200}}
uni = ["AAA", "BBB", "CCC", "DDD", "EEE"]
why = {s: runner.block_reason(s, uni) for s in uni}
check("block_reason: top1 -> None", why["AAA"] is None, why)
check("block_reason: ngoai top K co hang",
      why["BBB"] and "hạng 2/2" in why["BBB"], why)
check("block_reason: truot co ly do",
      why["CCC"] and "ADX 1h 31" in why["CCC"], why)
check("block_reason: het han", why["DDD"] and "hết hạn" in why["DDD"], why)
check("block_reason: chua quet", why["EEE"] == "scanner chưa quét", why)
check("allows() == (block_reason is None) moi symbol",
      all(runner.allows(s, uni) == (why[s] is None) for s in uni))
cfg_s["scanner"]["mode"] = "observe"
check("observe -> khong chan", runner.block_reason("CCC", uni) is None
      and runner.allows("CCC", uni))

# ------------------------------- doi chieu voi manage_grid that (random)


class FakeEngine:
    def __init__(self, st):
        self.st, self.opens, self.n = st, [], 100

    def open(self, symbol, side, notional, price, sl_pct, tp_pct, tag,
             level=None):
        self.n += 1
        pos = {"id": self.n, "symbol": symbol, "side": side, "entry": price,
               "qty": notional / price, "tag": tag, "level": level}
        self.opens.append(pos)
        self.st["positions"].append(pos)
        return pos, None


class StubTrend:
    def __init__(self, blocked):
        self.blocked = blocked

    def blocks(self, symbol, side):
        return self.blocked.get(side)


saved = copy.deepcopy(bb.CFG)
saved_tr, saved_sc, saved_log = bb.TREND, bb.SCANNER, bb.log
saved_sym = list(bb.SYMBOLS)
mismatch = []
try:
    bb.log = lambda *_: None
    bb.SYMBOLS[:] = ["ETHUSDT", "SOLUSDT", "XRPUSDT"]
    rnd = random.Random(42)
    for i in range(2000):
        bb._GRID_BLOCK_LOG.clear()
        c = cfg_base(max_symbols=rnd.choice([0, 1, 2]),
                     max_same_side=rnd.choice([0, 1, 2]),
                     levels_each_side=rnd.choice([1, 2]),
                     max_positions=rnd.choice([1, 3]))
        bb.CFG["grid"].update(c["grid"])
        bb.CFG.update(order_margin_usdt=100, leverage=10,
                      max_total_positions=rnd.choice([2, 4]),
                      disabled_symbols={})
        st = {"positions": [], "regimes": {}, "grids": {}}
        g = {"anchor": rnd.choice([None, 100.0, 100.0, 107.0]),
             "taken": {}, "step": 0.01}
        if rnd.random() < 0.15:
            g["risk_halted"] = True
        if rnd.random() < 0.15:
            g["rebuild_pending"] = True
        if rnd.random() < 0.3:
            g["taken"] = {rnd.choice(["b1", "s1", "b2"]): 1}
        st["grids"]["ETHUSDT"] = g
        for _ in range(rnd.choice([0, 0, 1, 2])):
            sym = rnd.choice(["ETHUSDT", "SOLUSDT", "XRPUSDT"])
            st["positions"].append({"id": rnd.randint(1, 99), "tag": "grid",
                                    "symbol": sym, "side": rnd.choice(
                                        ["long", "short"]),
                                    "level": rnd.choice(["b1", "s1"]),
                                    "entry": 100, "qty": 1})
        blocked = {}
        if rnd.random() < 0.4:
            blocked[rnd.choice(["long", "short"])] = "xu hướng (test)"
        bb.TREND = StubTrend(blocked)
        if rnd.random() < 0.4:
            scfg = {"scanner": {"enabled": True, "mode": "filter",
                                "top_k": 1}}
            bb.SCANNER = sc.ScannerRunner(scfg, fetch=None,
                                          log=lambda *_: None)
            bb.SCANNER.results = {
                "ETHUSDT": {"symbol": "ETHUSDT", "passed": rnd.random() < .6,
                            "score": rnd.choice([1, 9]), "ts": time.time()},
                "SOLUSDT": {"symbol": "SOLUSDT", "passed": True, "score": 5,
                            "ts": time.time()}}
        else:
            bb.SCANNER = None
        px = rnd.choice([98.9, 97.9, 99.5, 100.3, 101.1, 102.2, 100.0])
        diag = gd.explain_classic(
            copy.deepcopy(st), bb.CFG, ["ETHUSDT"], {"ETHUSDT": px},
            scanner_block=(lambda s: bb.SCANNER.block_reason(s, bb.SYMBOLS))
            if bb.SCANNER else None,
            trend_block=bb.grid_trend_block,
            side_cap_block=lambda side, st=st: bb.grid_side_cap_block(
                st, side))
        code = diag["symbols"][0]["code"]
        eng = FakeEngine(st)
        bb.manage_grid(eng, st, "ETHUSDT", px)
        opened = bool(eng.opens)
        if opened != (code == "ready"):
            mismatch.append((i, code, opened, px, g, blocked,
                             bb.CFG["grid"]["max_symbols"]))
    check("2000 tinh huong: diag 'ready' <=> manage_grid mo lot",
          not mismatch, mismatch[:3])

    # ---------------------------------------- grid_diag_tick trong bot
    logs = []
    bb.log = logs.append
    bb.SCANNER, bb.TREND = None, None
    bb.CFG["grid"].update(cfg_base()["grid"])
    bb.CFG["max_total_positions"] = 4
    st = {"positions": [], "regimes": {"SOLUSDT": {"regime": "trending",
                                                    "adx": 30}},
          "grids": {"ETHUSDT": {"anchor": 100.0, "taken": {},
                                "step": 0.01}}}
    bb.grid_diag_tick(st, {"ETHUSDT": 100.4, "SOLUSDT": 20.0},
                      {"ETHUSDT": {"15m": [1]}, "SOLUSDT": {"15m": [1]}},
                      False)
    check("bot tick: st['grid_diag'] + 1 dong GRID WAIT",
          st.get("grid_diag", {}).get("counts") == {
              "wait_price": 1, "regime": 1, "no_data": 1}
          and len(logs) == 1 and logs[0].startswith("GRID WAIT 3 symbol"),
          (st.get("grid_diag", {}).get("counts"), logs))
    check("bot tick: state van JSON hoa duoc",
          json.loads(json.dumps(st))["grid_diag"]["engine"] == "classic")
    logs.clear()
    bb.grid_diag_tick({"positions": None}, {}, {}, False)
    check("bot tick: loi -> chi log, khong nem",
          len(logs) == 1 and logs[0].startswith("grid_diag loi"), logs)
finally:
    bb.CFG.clear()
    bb.CFG.update(saved)
    bb.TREND, bb.SCANNER, bb.log = saved_tr, saved_sc, saved_log
    bb.SYMBOLS[:] = saved_sym

# --------------------------------------------------------- CLI chi doc
tmp = tempfile.mkdtemp()
try:
    base = os.path.join(tmp, "binance-bot")
    os.makedirs(base)
    os.symlink(os.path.join(BASE, "..", "db"), os.path.join(tmp, "db"))
    cfg = json.load(open(os.path.join(BASE, "config.example.json")))
    cfg["grid"]["engine"] = "classic"
    json.dump(cfg, open(os.path.join(base, "config.json"), "w"))
    json.dump([{"symbol": "ETHUSDT"}, {"symbol": "SOLUSDT"}],
              open(os.path.join(base, "universe.json"), "w"))
    json.dump({"version": 7, "config": {"scanner.mode": "filter",
                                        "scanner.top_k": 1}},
              open(os.path.join(base, "runtime_config.cache.json"), "w"))
    now = time.time()
    json.dump({"ts": now, "results": [
        {"symbol": "SOLUSDT", "passed": True, "score": 8, "ts": now}]},
        open(os.path.join(base, "scanner_latest.json"), "w"))
    state = {"positions": [], "halted": False,
             "regimes": {"ETHUSDT": {"regime": "ranging"},
                         "SOLUSDT": {"regime": "ranging"}},
             "grids": {"SOLUSDT": {"anchor": 20.0, "taken": {},
                                   "step": 0.01}},
             "trend": {"market": "BTCUSDT", "ts": now, "symbols": {
                 "BTCUSDT": {"bias": "down", "reason": "test", "ts": now,
                             "price": 60000.0},
                 "ETHUSDT": {"bias": "neutral", "ts": now, "price": 3000.0},
                 "SOLUSDT": {"bias": "neutral", "ts": now, "price": 19.7}}}}
    sp = os.path.join(base, "state.json")
    json.dump(state, open(sp, "w"))
    before = {f: open(os.path.join(base, f), "rb").read()
              for f in os.listdir(base)}
    buf = []
    rc = gd.run_cli(["--base", base, "--no-fetch"], out=buf.append)
    text = "\n".join(buf)
    check("CLI: chay OK, doc version cache", rc == 0
          and "config version 7" in text, text[:300])
    check("CLI: ETH bi scanner chan (top_k=1 tu cache)",
          any(l.startswith("ETHUSDT") and "scanner chặn" in l
              for l in buf), text)
    check("CLI: SOL cham b1 nhung BTC giam chan LONG",
          any(l.startswith("SOLUSDT") and "bị chặn chiều" in l
              and "BTC" in l for l in buf), text)
    check("CLI: co dong GRID WAIT", any(l.startswith("GRID WAIT")
                                        for l in buf))
    buf2 = []
    gd.run_cli(["--base", base, "--no-fetch", "--json"], out=buf2.append)
    js = json.loads(buf2[0])
    check("CLI --json hop le", js["counts"].get("scanner") == 1
          and js["counts"].get("side_blocked") == 1, js.get("counts"))
    after = {f: open(os.path.join(base, f), "rb").read()
             for f in os.listdir(base)}
    check("CLI: CHI DOC (khong tao/sua file nao)", before == after,
          set(after) ^ set(before))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
