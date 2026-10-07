#!/usr/bin/env python3
"""Test scanner di ngang (G2) + luat mo grid theo config runtime (G1).

Offline hoan toan. Run: python3 test_scanner.py
"""
import copy
import json
import os
import random
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
_cfg_p = os.path.join(BASE, "config.json")
_made_cfg = not os.path.exists(_cfg_p)
if _made_cfg:
    import shutil
    shutil.copy(os.path.join(BASE, "config.example.json"), _cfg_p)
_uni_p = os.path.join(BASE, "universe.json")
_made_uni = not os.path.exists(_uni_p)
if _made_uni:
    json.dump([{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}],
              open(_uni_p, "w"))
for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
    os.environ.pop(k, None)

import scanner as sc  # noqa: E402
import binance_bot  # noqa: E402
import live_binance  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (" | " + str(extra) if extra != "" and not cond else ""))


def ou(n, theta=0.15, sigma=0.7, seed=0, mu=100.0, drift=0.0):
    """Gia hoi quy ve trung binh (theta>0 = di ngang, 0 = random walk),
    drift>0 = trend. Nen co h/l tu 4 buoc con."""
    rnd = random.Random(seed)
    x, out = mu, []
    for i in range(n):
        m = mu * (1 + drift) ** i
        o, path = x, [x]
        for _ in range(4):
            x = x + theta / 4 * (m - x) + rnd.gauss(0, sigma / 2) * m / 100
            path.append(x)
        out.append({"ts": i * 3600000, "o": o, "h": max(path),
                    "l": min(path), "c": x})
    return out


# --------------------------------------------------------- chi bao
c = [{"h": 1, "l": 0, "c": 0.5}] * 3
check("closed() bo nen dang hinh thanh", len(sc.closed(c)) == 2
      and len(sc.closed(c[:1])) == 1)
line = [100 + i for i in range(60)]
check("ER di thang = 1", abs(sc.efficiency_ratio(line, 48) - 1) < 1e-9)
zig = [100 + (i % 2) for i in range(60)]
check("ER zigzag ~ 0", sc.efficiency_ratio(zig, 48) < 0.05)
check("ER thieu du lieu -> None", sc.efficiency_ratio(line[:5], 48) is None)
zc = [{"h": 101 + (i % 2), "l": 99 + (i % 2), "c": 100 + (i % 2)}
      for i in range(60)]
tc = [{"h": 101 + i, "l": 99 + i, "c": 100 + i} for i in range(60)]
check("CHOP zigzag > CHOP trend", sc.choppiness(zc, 48) > 60
      > 30 > sc.choppiness(tc, 48), (sc.choppiness(zc, 48),
                                     sc.choppiness(tc, 48)))
rs = sc.range_stats(zc, 48)
check("range_stats: bien/cat giua/vi tri",
      rs["high"] == 102 and rs["low"] == 99 and rs["crosses"] >= 40
      and 0 <= rs["pos"] <= 1, rs)
bbw = sc.bb_width_series([100.0] * 25)
check("BBW gia phang = 0", bbw and max(bbw) == 0)
check("percentile_rank", sc.percentile_rank([1, 2, 3, 4], 2) == 50.0)

# ------------------------------------------ tach loai thi truong (50 seed)
def run(kind):
    out = []
    for seed in range(50):
        if kind == "range":
            h1, m15 = ou(500, seed=seed), ou(100, 0.1, 0.35, seed + 1000)
        elif kind == "rw":
            h1, m15 = (ou(500, 0.0, seed=seed),
                       ou(100, 0.0, 0.35, seed + 1000))
        else:
            h1, m15 = (ou(500, seed=seed, drift=0.002),
                       ou(100, 0.1, 0.35, seed + 1000, drift=0.0005))
        out.append(sc.evaluate("X", h1, m15, {}, now=1.0))
    return out


R, W, T = run("range"), run("rw"), run("trend")
pr = sum(r["passed"] for r in R)
pw = sum(r["passed"] for r in W)
pt = sum(r["passed"] for r in T)
check("trend: 0/50 dat chuan", pt == 0, pt)
check("di ngang dat chuan nhieu hon random walk ro rang",
      pr >= 15 and pr >= 2 * pw, (pr, pw))
avg = lambda xs: sum(r["score"] for r in xs) / len(xs)  # noqa: E731
check("diem: di ngang > random walk > trend",
      avg(R) > avg(W) > avg(T) and avg(R) - avg(T) > 25,
      (avg(R), avg(W), avg(T)))
t0 = T[0]
check("trend co ly do cu the (ADX/ER)",
      any("ADX 1h" in x for x in t0["reasons"]), t0["reasons"])
ok = next(r for r in R if r["passed"])
check("ket qua dat chuan: reasons rong, du metrics",
      ok["reasons"] == [] and all(k in ok["metrics"] for k in
                                  ("adx_1h", "adx_15m", "bbw_pct", "chop",
                                   "er", "range_pct", "pos", "mid_crosses")))
r = sc.evaluate("X", ou(30), ou(100))
check("thieu nen -> khong dat, ly do thieu du lieu",
      not r["passed"] and "thiếu" in r["reasons"][0])
# nen dang hinh thanh khong anh huong: doi nen cuoi khong doi ket qua
h1 = ou(500, seed=3)
a = sc.evaluate("X", h1, ou(100, seed=4), now=1.0)
h1b = copy.deepcopy(h1)
h1b[-1].update(h=500, l=1, c=400)
b = sc.evaluate("X", h1b, ou(100, seed=4), now=1.0)
check("chi dung nen da dong", a == b)
# vi tri trong bien chi doi diem, khong loai
check("vi tri gia khong nam trong ly do loai",
      all("vị trí" not in x for r in R + W + T for x in r["reasons"]))
cfg_loose = {"adx_1h_max": 50, "adx_15m_max": 50, "bbw_min_pct": 0.002,
             "bbw_max_pct": 0.5, "bbw_pctile_max": 100, "range_min_pct": 0.002,
             "range_max_pct": 0.6, "min_mid_crosses": 0, "chop_min": 0,
             "er_max": 1.0}
check("nguong lay tu cfg (noi het -> trend cung dat)",
      sc.evaluate("X", ou(500, drift=0.0005), ou(100), cfg_loose)["passed"])

# ----------------------------------------------- allowed / rank
res = {"A": {"symbol": "A", "ts": 100, "passed": True, "score": 60},
       "B": {"symbol": "B", "ts": 100, "passed": True, "score": 90},
       "C": {"symbol": "C", "ts": 100, "passed": False, "score": 99},
       "D": {"symbol": "D", "ts": 0, "passed": True, "score": 95}}
check("rank: dat chuan truoc roi diem",
      [r["symbol"] for r in sc.rank(list(res.values()))] == ["D", "B", "A",
                                                            "C"])
check("allowed: top K, bo truot + het han",
      sc.allowed_symbols(res, 1, now=150, max_age_seconds=100) == ["B"]
      and sc.allowed_symbols(res, 5, 150, 100) == ["B", "A"])
check("allowed: eligible loc TRUOC khi cat top K",
      sc.allowed_symbols(res, 1, 150, 100,
                         eligible=lambda r: r["symbol"] != "B") == ["A"])

# ----------------------------------------------- ScannerRunner
clock = [10000.0]
calls = []
fail = set()


class Fatal(Exception):
    pass


def fetch(sym, interval, limit):
    calls.append((sym, interval, limit))
    if sym in fail:
        raise RuntimeError("timeout")
    if sym == "BOOM":
        raise Fatal("429")
    return ou(500, seed=hash(sym) % 100) if sym != "TREND" else \
        ou(500, drift=0.002)


tmpd = tempfile.mkdtemp()
path = os.path.join(tmpd, "scan.json")
cfg = {"scanner": {"enabled": True, "mode": "observe", "top_k": 1,
                   "rescan_minutes": 15}}
logs = []
runner = sc.ScannerRunner(cfg, fetch, log=logs.append, path=path,
                          clock=lambda: clock[0], fatal=(Fatal,))
syms = ["AAA", "TREND", "BBB"]
cands = {s: {"15m": ou(100, seed=7)} for s in syms}
d1 = runner.tick(syms, cands)
check("moi luot quet 1 symbol, lay 1h x499",
      len(d1) == 1 and calls[-1][1:] == ("1h", 499))
runner.tick(syms, cands)
runner.tick(syms, cands)
check("xoay vong het universe", set(runner.results) == set(syms))
n = len(calls)
runner.tick(syms, cands)
check("chua den han rescan -> khong goi API", len(calls) == n)
clock[0] += 15 * 60
runner.tick(syms, cands)
check("den han -> quet lai symbol cu nhat", len(calls) == n + 1)
check("ghi file ket qua", json.load(open(path))["results"])
r2 = sc.ScannerRunner(cfg, fetch, path=path, clock=lambda: clock[0])
check("khoi dong lai nap ket qua tu file", set(r2.results) == set(syms))
fail.add("CCC")
cands["CCC"] = {"15m": ou(100)}
runner.tick(["CCC"], cands)
n = len(calls)
runner.tick(["CCC"], cands)
check("loi mang -> cho 120s moi thu lai", len(calls) == n
      and any("CCC" in x for x in logs))
check("roi universe -> bo ket qua cu", set(runner.results) <= {"CCC"})
try:
    runner.tick(["BOOM"], {"BOOM": {"15m": ou(100)}})
    check("BinanceSafetyStop (fatal) duoc nem lai", False)
except Fatal:
    check("BinanceSafetyStop (fatal) duoc nem lai", True)
runner.tick(["DDD"], {"DDD": {}})
check("chua co nen 15m -> bo qua", "DDD" not in runner.results)
cfg["scanner"]["enabled"] = False
n = len(calls)
clock[0] += 3600
check("tat scanner -> khong quet",
      runner.tick(syms, cands) == [] and len(calls) == n)

# allows: observe luon cho; filter fail-closed
cfg["scanner"].update(enabled=True, mode="observe")
fr = sc.ScannerRunner(cfg, fetch, clock=lambda: clock[0])
check("observe: khong chan grid", fr.allows("XYZ", ["XYZ"]))
cfg["scanner"]["mode"] = "filter"
check("filter + chua co du lieu -> chan (fail-closed)",
      not fr.allows("XYZ", ["XYZ"]))
fr.results = {"AAA": {"symbol": "AAA", "ts": clock[0], "passed": True,
                      "score": 70},
              "BBB": {"symbol": "BBB", "ts": clock[0], "passed": True,
                      "score": 80},
              "TREND": {"symbol": "TREND", "ts": clock[0], "passed": False,
                        "score": 20}}
check("filter top_k=1: chi symbol diem cao nhat",
      fr.allows("BBB", syms) and not fr.allows("AAA", syms)
      and not fr.allows("TREND", syms))
check("filter: top K tinh trong universe hien tai",
      fr.allows("AAA", ["AAA", "TREND"]))
clock[0] += fr.max_age() + 1
check("filter: ket qua het han -> chan", not fr.allows("BBB", syms))

# ------------------------------------------- luat mo grid (binance_bot)
class FakeEngine:
    def __init__(self):
        self.opens = []
        self.n = 0

    def open(self, symbol, side, notional, price, sl_pct, tp_pct, tag,
             level=None):
        self.n += 1
        pos = {"id": self.n, "symbol": symbol, "side": side, "entry": price,
               "notional": notional, "tag": tag, "level": level,
               "sl_pct": sl_pct, "tp_pct": tp_pct}
        self.opens.append(pos)
        return pos, None


def grid_state(sym, anchor=100.0, step=0.005, n_pos=()):
    st = {"positions": list(n_pos), "grids": {}}
    st["grids"][sym] = {"anchor": anchor, "taken": {}, "step": step}
    return st


saved = copy.deepcopy(binance_bot.CFG)
saved_scanner = binance_bot.SCANNER
saved_symbols = list(binance_bot.SYMBOLS)
try:
    binance_bot.SCANNER = None
    G = binance_bot.CFG["grid"]
    G.update(tp_pct=0.01, sl_pct=0.025, max_symbols=0)
    binance_bot.CFG.update(order_margin_usdt=100, leverage=10)
    eng = FakeEngine()
    st = grid_state("BTCUSDT")
    binance_bot.manage_grid(eng, st, "BTCUSDT", 99.4)
    o = eng.opens[-1]
    check("grid: TP lay grid.tp_pct (1%), SL lay grid.sl_pct",
          o["tp_pct"] == 0.01 and o["sl_pct"] == 0.025 and o["side"] == "long",
          o)
    check("grid: lot = margin 100 x lev 10 = $1000", o["notional"] == 1000)
    G["tp_pct"] = 0
    eng = FakeEngine()
    binance_bot.manage_grid(eng, grid_state("BTCUSDT"), "BTCUSDT", 99.4)
    check("grid: tp_pct=0 -> TP = 1 step (hanh vi cu)",
          eng.opens[-1]["tp_pct"] == 0.005)
    G["tp_pct"] = 0.01
    # doi config nong: lan mo sau dung gia tri moi
    binance_bot.CFG["order_margin_usdt"] = 150
    eng = FakeEngine()
    binance_bot.manage_grid(eng, grid_state("BTCUSDT"), "BTCUSDT", 99.4)
    check("grid: doi margin tren dashboard -> lot moi $1500",
          eng.opens[-1]["notional"] == 1500)
    binance_bot.CFG["order_margin_usdt"] = 100
    # max_symbols
    G["max_symbols"] = 1
    busy = {"id": 99, "symbol": "ETHUSDT", "tag": "grid", "side": "long",
            "level": "b1"}
    eng = FakeEngine()
    binance_bot.manage_grid(eng, grid_state("BTCUSDT", n_pos=[busy]),
                            "BTCUSDT", 99.4)
    check("max_symbols=1: symbol moi khong vao khi da co 1 symbol grid",
          eng.opens == [])
    eng = FakeEngine()
    st = grid_state("ETHUSDT", n_pos=[busy])
    st["grids"]["ETHUSDT"]["taken"] = {"b1": 99}
    binance_bot.manage_grid(eng, st, "ETHUSDT", 98.9)
    check("max_symbols: symbol dang co lot van them tang duoc",
          len(eng.opens) == 1 and eng.opens[0]["level"] == "b2", eng.opens)
    scalp = {"id": 98, "symbol": "SOLUSDT", "tag": "scalp", "side": "long"}
    eng = FakeEngine()
    binance_bot.manage_grid(eng, grid_state("BTCUSDT", n_pos=[scalp]),
                            "BTCUSDT", 99.4)
    check("max_symbols chi dem lot grid (bo qua scalp)", len(eng.opens) == 1)
    G["max_symbols"] = 0
    eng = FakeEngine()
    binance_bot.manage_grid(eng, grid_state("BTCUSDT", n_pos=[busy]),
                            "BTCUSDT", 99.4)
    check("max_symbols=0: khong gioi han", len(eng.opens) == 1)
    # scanner filter
    fcfg = {"scanner": {"enabled": True, "mode": "filter", "top_k": 1,
                        "rescan_minutes": 15}}
    now = [5000.0]
    binance_bot.SCANNER = sc.ScannerRunner(fcfg, fetch, clock=lambda: now[0])
    binance_bot.SYMBOLS[:] = ["BTCUSDT", "ETHUSDT"]
    binance_bot.SCANNER.results = {
        "BTCUSDT": {"symbol": "BTCUSDT", "ts": now[0], "passed": False,
                    "score": 30},
        "ETHUSDT": {"symbol": "ETHUSDT", "ts": now[0], "passed": True,
                    "score": 70}}
    eng = FakeEngine()
    st = grid_state("BTCUSDT")
    binance_bot.manage_grid(eng, st, "BTCUSDT", 99.4)
    check("filter: symbol truot scanner khong mo grid", eng.opens == []
          and st["grids"]["BTCUSDT"]["anchor"] == 100.0)
    binance_bot.manage_grid(eng, grid_state("ETHUSDT"), "ETHUSDT", 99.4)
    check("filter: symbol dat chuan mo grid", len(eng.opens) == 1)
    fcfg["scanner"]["mode"] = "observe"
    eng = FakeEngine()
    binance_bot.manage_grid(eng, grid_state("BTCUSDT"), "BTCUSDT", 99.4)
    check("observe: scanner khong chan", len(eng.opens) == 1)
finally:
    binance_bot.CFG.clear()
    binance_bot.CFG.update(saved)
    binance_bot.SCANNER = saved_scanner
    binance_bot.SYMBOLS[:] = saved_symbols

# ------------------------------------- leverage dat lai khi doi config
eng = live_binance.BinanceEngine.__new__(live_binance.BinanceEngine)
lv_logs = []
eng.cfg = {"leverage": 10}
eng.dry_run = True
eng.log = lv_logs.append
eng._lev_done = {}
eng._set_leverage("BTCUSDT")
eng._set_leverage("BTCUSDT")
check("leverage: dat 1 lan moi symbol", len(lv_logs) == 1)
eng.cfg["leverage"] = 5
eng._set_leverage("BTCUSDT")
check("leverage: doi tren dashboard -> dat lai tren san",
      len(lv_logs) == 2 and "lev=5" in lv_logs[-1])

if _made_cfg:
    os.remove(_cfg_p)
if _made_uni:
    os.remove(_uni_p)
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
