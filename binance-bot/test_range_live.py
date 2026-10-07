#!/usr/bin/env python3
"""Test range grid live (G4): binance_bot.manage_range_grid / range_grid_risk
/ range_allowed_symbols + rang buoc config. Offline.

Run: python3 test_range_live.py
"""
import copy
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "..", "db"))
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

import binance_bot as bb  # noqa: E402
import bot_config  # noqa: E402
import scanner as sc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + ("" if cond else "  " + str(detail)))


class FakeEngine:
    def __init__(self, st=None):
        self.st = st
        self.opens, self.closes = [], []
        self.n = 100
        self.fail = False

    def open(self, symbol, side, notional, price, sl_pct, tp_pct, tag,
             level=None):
        if self.fail:
            return None, "cooldown"
        self.n += 1
        pos = {"id": self.n, "symbol": symbol, "side": side, "entry": price,
               "qty": notional / price, "notional": notional, "tag": tag,
               "level": level, "sl_pct": sl_pct, "tp_pct": tp_pct}
        self.opens.append(pos)
        if self.st is not None:
            self.st["positions"].append(pos)
        return pos, None

    def close(self, pos, price, reason):
        self.closes.append((pos["id"], price, reason))
        if self.st is not None:
            self.st["positions"].remove(pos)
        return None      # khong ghi trade (tranh _record_close ghi file)


NOW = [100000.0]


def metrics(low=95.0, high=105.0, adx1=15.0, atr=None):
    return {"range_low": low, "range_high": high, "adx_1h": adx1,
            "atr15_pct": atr, "range_pct": high / low - 1}


def scan(sym, ts=None, passed=True, score=70.0, **kw):
    return {"symbol": sym, "ts": NOW[0] if ts is None else ts,
            "passed": passed, "score": score, "metrics": metrics(**kw),
            "reasons": []}


def fresh_state():
    return {"positions": [], "grids": {}}


saved = copy.deepcopy(bb.CFG)
saved_scanner = bb.SCANNER
saved_symbols = list(bb.SYMBOLS)
saved_log = bb.log
logs = []
try:
    bb.log = logs.append
    bb.SYMBOLS[:] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    bb.CFG.update(order_margin_usdt=100, leverage=10, max_total_positions=10)
    G = bb.CFG["grid"]
    G.update(engine="range", step_pct=0.01, step_min=0.01, step_max=0.01,
             tp_pct=0.01, sl_pct=0.03, levels_each_side=5, max_positions=7,
             max_lots_per_symbol=4, max_symbols=0, boundary_sl_buffer=0.005,
             break_buffer=0.003, trend_exit_adx=25, derisk_on_trend=False,
             derisk_loss_pct=0.01, max_entries_per_cycle=1)
    bb.CFG["scanner"] = {"enabled": True, "mode": "observe", "top_k": 2,
                         "rescan_minutes": 15}
    bb.SCANNER = sc.ScannerRunner(bb.CFG, lambda *a: [],
                                  clock=lambda: NOW[0])

    # ------------------------------------------------ allowed symbols
    bb.SCANNER.results = {
        "BTCUSDT": scan("BTCUSDT", score=80),
        "ETHUSDT": scan("ETHUSDT", score=60),
        "SOLUSDT": scan("SOLUSDT", score=90, passed=False),
    }
    check("allowed: dat chuan + top K, ca o che do observe",
          bb.range_allowed_symbols() == {"BTCUSDT", "ETHUSDT"})
    bb.CFG["scanner"]["top_k"] = 1
    check("allowed: top_k=1 -> diem cao nhat",
          bb.range_allowed_symbols() == {"BTCUSDT"})
    bb.CFG["scanner"]["top_k"] = 2
    bb.SCANNER.results["ETHUSDT"]["ts"] = NOW[0] - 4000
    check("allowed: ket qua het han -> loai (fail-closed)",
          bb.range_allowed_symbols() == {"BTCUSDT"})
    bb.SCANNER.results["ETHUSDT"]["ts"] = NOW[0]
    bb.CFG["scanner"]["enabled"] = False
    check("allowed: scanner tat -> rong", bb.range_allowed_symbols() == set())
    bb.CFG["scanner"]["enabled"] = True
    sv = bb.SCANNER
    bb.SCANNER = None
    check("allowed: khong co scanner -> rong",
          bb.range_allowed_symbols() == set())
    bb.SCANNER = sv
    bb.MANAGE_ONLY.add("ETHUSDT")
    check("allowed: symbol MANAGE_ONLY bi loai",
          bb.range_allowed_symbols() == {"BTCUSDT"})
    bb.MANAGE_ONLY.discard("ETHUSDT")

    allowed = {"BTCUSDT", "ETHUSDT"}
    # ------------------------------------------------ build + entries
    st = fresh_state()
    eng = FakeEngine(st)
    bb.manage_range_grid(eng, st, "BTCUSDT", 100.0, allowed)
    g = st["grids"]["BTCUSDT"]
    check("range: flat + dat chuan -> dung bien tu scanner",
          g.get("range") and g["range"]["low"] == 95.0
          and g["range"]["high"] == 105.0 and not g.get("broken"), g)
    check("range: gia giua bien -> chua vao lenh", eng.opens == [])
    bb.manage_range_grid(eng, st, "BTCUSDT", 98.9, allowed)
    o = eng.opens[-1] if eng.opens else {}
    check("range: gia cat tang rb1 -> LONG market tag grid level rb1",
          o.get("side") == "long" and o.get("level") == "rb1"
          and o.get("tag") == "grid" and o.get("notional") == 1000, o)
    check("range: SL bien quy ra %, khong xa hon sl_pct; TP 1%",
          abs(o["sl_pct"] - min(0.03, 1 - 95 * 0.995 / 98.9)) < 1e-9
          and abs(o["tp_pct"] - 0.01) < 1e-9, o)
    check("range: max_entries_per_cycle=1 -> 1 lot/vong", len(eng.opens) == 1)
    bb.manage_range_grid(eng, st, "BTCUSDT", 98.9, allowed)
    check("range: vong sau mo tang ke (rb2 chua cat -> khong)",
          len(eng.opens) == 1, eng.opens)
    bb.manage_range_grid(eng, st, "BTCUSDT", 97.9, allowed)
    check("range: cat rb2 -> them lot rb2",
          [p["level"] for p in eng.opens] == ["rb1", "rb2"])
    bb.manage_range_grid(eng, st, "BTCUSDT", 101.2, allowed)
    check("range: nua tren -> SHORT rs1 (hedge 2 chieu)",
          eng.opens[-1]["side"] == "short"
          and eng.opens[-1]["level"] == "rs1", eng.opens[-1])
    # bien khong doi khi con lot, du co scan moi hon
    NOW[0] += 900
    bb.SCANNER.results["BTCUSDT"] = scan("BTCUSDT", low=90, high=110)
    bb.manage_range_grid(eng, st, "BTCUSDT", 100.0, allowed)
    check("range: con lot -> giu bien cu (khong dung lai)",
          st["grids"]["BTCUSDT"]["range"]["low"] == 95.0)
    # max_lots_per_symbol
    G["max_lots_per_symbol"] = 3
    n = len(eng.opens)
    bb.manage_range_grid(eng, st, "BTCUSDT", 96.9, allowed)
    check("slot: max_lots_per_symbol=3 -> khong mo lot thu 4",
          len(eng.opens) == n, eng.opens[-1])
    G["max_lots_per_symbol"] = 4
    # max_symbols
    G["max_symbols"] = 1
    bb.manage_range_grid(eng, st, "ETHUSDT", 100.0, allowed)
    bb.manage_range_grid(eng, st, "ETHUSDT", 98.9, allowed)
    check("slot: max_symbols=1 -> symbol moi khong vao",
          all(p["symbol"] == "BTCUSDT" for p in eng.opens))
    G["max_symbols"] = 0
    # max_positions
    G["max_positions"] = 3
    bb.manage_range_grid(eng, st, "ETHUSDT", 98.9, allowed)
    check("slot: tran grid.max_positions tinh moi symbol",
          all(p["symbol"] == "BTCUSDT" for p in eng.opens))
    G["max_positions"] = 7
    bb.manage_range_grid(eng, st, "ETHUSDT", 98.9, allowed)
    check("slot: con slot -> symbol thu 2 vao duoc",
          eng.opens[-1]["symbol"] == "ETHUSDT")
    check("range_slot_ok: pending tinh nhu lot",
          bb.range_slot_ok(st, "ETHUSDT")
          and not bb.range_slot_ok(st, "ETHUSDT", {"ETHUSDT": 3}))
    # khong con trong top K -> khong mo moi
    n = len(eng.opens)
    bb.manage_range_grid(eng, st, "ETHUSDT", 97.9, {"BTCUSDT"})
    check("range: roi top K -> khong mo moi", len(eng.opens) == n)
    # engine loi -> dung vong, khong spam
    eng.fail = True
    bb.manage_range_grid(eng, st, "ETHUSDT", 97.9, allowed)
    check("range: open loi -> khong ghi taken",
          "rb2" not in st["grids"]["ETHUSDT"]["taken"])
    eng.fail = False

    # ------------------------------------------------ break / derisk
    st = fresh_state()
    eng = FakeEngine(st)
    NOW[0] += 900
    bb.SCANNER.results["BTCUSDT"] = scan("BTCUSDT")
    bb.manage_range_grid(eng, st, "BTCUSDT", 100.0, allowed)
    bb.manage_range_grid(eng, st, "BTCUSDT", 98.9, allowed)
    bb.manage_range_grid(eng, st, "BTCUSDT", 97.9, allowed)
    bb.range_grid_risk(eng, st, {"BTCUSDT": 96.0})
    check("break: trong bien -> chua vo", not st["grids"]["BTCUSDT"].get(
        "broken"))
    bb.range_grid_risk(eng, st, {"BTCUSDT": 94.5})
    g = st["grids"]["BTCUSDT"]
    check("break: thung day qua buffer -> broken + log",
          g.get("broken") and any("VO BIEN" in x for x in logs))
    check("break: derisk tat -> khong dong lot", eng.closes == [])
    n = len(eng.opens)
    bb.manage_range_grid(eng, st, "BTCUSDT", 101.5, allowed)
    check("break: khong mo moi sau khi vo", len(eng.opens) == n)
    G["derisk_on_trend"] = True
    bb.range_grid_risk(eng, st, {"BTCUSDT": 97.5})
    check("derisk: chi cat lot lo > 1% (rb1 @98.9), giu rb2 @97.9",
          [c[2] for c in eng.closes] == ["GRID_DERISK"]
          and [p["level"] for p in st["positions"]] == ["rb2"], eng.closes)
    G["derisk_on_trend"] = False
    # ADX 1h
    st2 = fresh_state()
    e2 = FakeEngine(st2)
    NOW[0] += 900
    bb.SCANNER.results["ETHUSDT"] = scan("ETHUSDT")
    bb.manage_range_grid(e2, st2, "ETHUSDT", 100.0, allowed)
    bb.SCANNER.results["ETHUSDT"]["metrics"]["adx_1h"] = 31
    bb.range_grid_risk(e2, st2, {"ETHUSDT": 100.0})
    check("break: ADX 1h > trend_exit_adx -> broken",
          st2["grids"]["ETHUSDT"].get("broken"))
    # flat + scan moi -> dung lai bien moi
    NOW[0] += 900
    bb.SCANNER.results["ETHUSDT"] = scan("ETHUSDT", low=96, high=104)
    bb.manage_range_grid(e2, st2, "ETHUSDT", 100.0, allowed)
    g = st2["grids"]["ETHUSDT"]
    check("rebuild: flat + scan moi hon -> bien moi, het broken",
          g["range"]["low"] == 96 and not g.get("broken"))
    NOW[0] += 900
    bb.SCANNER.results["ETHUSDT"] = scan("ETHUSDT", low=101, high=110)
    bb.manage_range_grid(e2, st2, "ETHUSDT", 100.0, allowed)
    check("rebuild: gia da ngoai bien moi -> khong dung (giu bien cu)",
          st2["grids"]["ETHUSDT"]["range"]["low"] == 96)

    # ------------------------------------------------ risk_halted
    st3 = fresh_state()
    e3 = FakeEngine(st3)
    NOW[0] += 900
    bb.SCANNER.results["BTCUSDT"] = scan("BTCUSDT")
    bb.manage_range_grid(e3, st3, "BTCUSDT", 100.0, allowed)
    st3["grids"]["BTCUSDT"].update(risk_halted=True, rebuild_pending=True)
    bb.manage_range_grid(e3, st3, "BTCUSDT", 98.9, allowed)
    g = st3["grids"]["BTCUSDT"]
    check("risk_halted: khong mo; flat + rebuild_pending -> bien cu bo",
          e3.opens == [] and g.get("broken") and not g.get("rebuild_pending"))

    # ------------------------------------------------ doi engine giua chung
    st4 = fresh_state()
    st4["positions"].append({"id": 7, "symbol": "BTCUSDT", "tag": "grid",
                             "side": "long", "level": "b1", "entry": 99,
                             "qty": 10})
    e4 = FakeEngine(st4)
    bb.manage_range_grid(e4, st4, "BTCUSDT", 98.9, allowed)
    check("doi engine: con lot classic -> range khong dung bien/khong mo",
          e4.opens == [] and not st4["grids"]["BTCUSDT"].get("range"))
    st5 = fresh_state()
    st5["positions"].append({"id": 8, "symbol": "BTCUSDT", "tag": "grid",
                             "side": "long", "level": "rb1", "entry": 99,
                             "qty": 10})
    st5["grids"]["BTCUSDT"] = {"anchor": 100.0, "taken": {}, "step": 0.005}
    e5 = FakeEngine(st5)
    bb.SCANNER = None
    bb.manage_grid(e5, st5, "BTCUSDT", 98.0)
    check("doi engine: con lot range -> classic dong bang", e5.opens == [])
    bb.SCANNER = sv

    # ------------------------------------------------ config schema
    flat = bot_config.defaults()
    clean, errs = bot_config.validate(flat)
    check("config: mac dinh hop le, engine classic",
          not errs and clean["grid.engine"] == "classic", errs)
    flat.update({"grid.engine": "range", "scanner.enabled": False})
    check("config: range can scanner bat",
          any("scanner" in e for e in bot_config.validate(flat)[1]))
    flat.update({"scanner.enabled": True, "grid.trend_exit_adx": 15})
    check("config: ADX chuyen trend phai >= ADX 1h scanner",
          any("ADX 1h chuyển trend" in e for e in bot_config.validate(flat)[1]))
    flat["grid.trend_exit_adx"] = 25
    check("config: range hop le", not bot_config.validate(flat)[1])
    check("config: engine la enum",
          any("grid.engine" in e for e in bot_config.validate(
              dict(flat, **{"grid.engine": "foo"}))[1]))
    ex = json.load(open(os.path.join(BASE, "config.example.json")))
    check("config.example: co du khoa Grid v2 & hop le",
          all(k.split(".", 1)[1] in ex["grid"] for k in bot_config.PARAM_BY_KEY
              if bot_config.PARAM_BY_KEY[k].group == "Grid v2")
          and not bot_config.validate(bot_config.extract(ex))[1],
          bot_config.validate(bot_config.extract(ex))[1])
finally:
    bb.CFG.clear()
    bb.CFG.update(saved)
    bb.SCANNER = saved_scanner
    bb.SYMBOLS[:] = saved_symbols
    bb.log = saved_log
    if _made_cfg:
        os.remove(_cfg_p)
    if _made_uni:
        os.remove(_uni_p)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
