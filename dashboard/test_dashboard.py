"""Test cac ham thuan cua dashboard/app.py ma khong can streamlit/psycopg.

Tach ham bang ast roi exec trong namespace rieng (app.py import streamlit,
psycopg o top-level nen khong import truc tiep duoc tren may test).
Chay: python dashboard/test_dashboard.py
"""
import ast
import json
import os
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def load(*names, extra=None):
    """Exec cac ham/hang so top-level co ten trong `names`."""
    ns = {"os": os, "json": json, "time": time, "datetime": datetime,
          "timezone": timezone, **(extra or {})}
    body = []
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            body.append(node)
    mod = ast.Module(body=body, type_ignores=[])
    exec(compile(mod, "app.py", "exec"), ns)
    missing = [n for n in names if n not in ns]
    assert not missing, missing
    return ns


def test_sol_wallet():
    print("== Bug 5: vi SOL hien thi = vi live_trader ==")
    ns = load("DEFAULT_SOL_WALLET", "resolve_sol_wallet",
              extra={"LIVE_CFG_P": "/nonexistent/config.live.json"})
    f = ns["resolve_sol_wallet"]
    live = "DxYkrsJA6YdS1cqJ9ocPCYRBacd7Xan3DeYWZva89dLd"
    check("mac dinh = vi live", f(env={}) == live, f(env={}))
    check("khong con vi cu 7jUg6P...", "7jUg6PKSj5xgsTM7dLMGnFFS45yohVfgvhbXPTUKfC8q"
          not in SRC)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "config.live.json")
        json.dump({"wallet_address": "CfgWallet111"}, open(p, "w"))
        check("doc wallet_address tu config.live.json",
              f(env={}, cfg_path=p) == "CfgWallet111")
        check("env SOL_WALLET uu tien hon config",
              f(env={"SOL_WALLET": "EnvW"}, cfg_path=p) == "EnvW")
        open(p, "w").write("{hong")
        check("config hong -> fallback vi live",
              f(env={}, cfg_path=p) == live)
    # live_trader dung cung vi mac dinh
    lt_src = open(os.path.join(HERE, "..", "meme-radar", "live_trader.py"),
                  encoding="utf-8").read()
    check("DEFAULT_SOL_WALLET khop live_trader DEFAULTS",
          f'"wallet_address": "{ns["DEFAULT_SOL_WALLET"]}"' in lt_src)


def test_live_radar_halt_status():
    print("== Trang thai halt that cua live_trader (khong doc file HALT chet) ==")
    ns = load("live_radar_halt_status")
    f = ns["live_radar_halt_status"]
    day = "2026-10-07"
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.live.json")
        st_p = os.path.join(d, "live_state.json")

        def state(**kw):
            json.dump(kw, open(st_p, "w"))

        check("khong co live_state -> canh bao",
              [lv for lv, _ in f(d, cfg, day)] == ["warning"])
        state(daily={"day": day, "realized_usd": -1.0,
                     "day_start_portfolio_usd": 100.0,
                     "risk_unavailable": False})
        check("binh thuong -> rong", f(d, cfg, day) == [], f(d, cfg, day))
        open(os.path.join(d, "HALT"), "w").write("x")
        check("file HALT khong lien quan -> van rong", f(d, cfg, day) == [])
        state(daily={"day": day, "realized_usd": -21.0,
                     "day_start_portfolio_usd": 100.0,
                     "risk_unavailable": False})
        r = f(d, cfg, day)
        check("lo > 20% -> DAILY STOP", len(r) == 1 and r[0][0] == "error"
              and "DAILY STOP" in r[0][1], r)
        json.dump({"daily_stop_pct": 0.30}, open(cfg, "w"))
        check("dung daily_stop_pct tu config", f(d, cfg, day) == [])
        check("daily cua ngay cu -> khong bao", f(d, cfg, "2026-10-08") == [])
        state(daily={"day": day, "realized_usd": 0.0,
                     "day_start_portfolio_usd": 1000.0,
                     "risk_unavailable": True},
              entry_blocked=True, block_reason="unmanaged tokens: 1",
              block_since="2026-10-07 01:00:00")
        r = f(d, cfg, day)
        check("risk_unavailable + entry_blocked deu hien",
              [lv for lv, _ in r] == ["error", "warning"]
              and "unmanaged tokens: 1" in r[1][1], r)
        open(os.path.join(d, "STOP_LIVE"), "w").write("")
        r = f(d, cfg, day)
        check("STOP_LIVE -> loi dau tien, nhac vi the van mo",
              r[0][0] == "error" and "STOP_LIVE" in r[0][1]
              and "VẪN MỞ" in r[0][1], r)
    check("UI khong con doc meme-radar/HALT",
          'meme-radar/HALT"' not in SRC)


def test_config_helpers():
    print("== G1: helper trang cau hinh ==")
    import sys
    from datetime import timedelta
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "db"))
    import bot_config as bc
    ns = load("FEE_TAKER", "FEE_MAKER", "SLIPPAGE", "widget_spec",
              "to_widget", "from_widget", "fmt_param", "estimate_trade",
              "apply_status", "changed_keys", "APPLY_LABEL",
              extra={"timedelta": timedelta})
    bad = []
    for p in bc.PARAMS:
        if p.kind not in ("int", "float"):
            continue
        spec = ns["widget_spec"](p)
        w = ns["to_widget"](p, p.default)
        if not (spec["min_value"] <= w <= spec["max_value"]):
            bad.append((p.key, "default ngoai bien widget"))
        if ns["from_widget"](p, w) != p.default:
            bad.append((p.key, "khong khu hai chieu", w))
        if p.kind == "int" and not all(isinstance(spec[k], int) for k in
                                       ("min_value", "max_value", "step")):
            bad.append((p.key, "int widget phai toan int"))
    check("moi tham so: default trong bien widget + doi % hai chieu", not bad,
          bad)
    tp = bc.PARAM_BY_KEY["grid.tp_pct"]
    check("TP nhap 1.25 (%) -> luu 0.0125",
          ns["from_widget"](tp, 1.25) == 0.0125
          and ns["to_widget"](tp, 0.0125) == 1.25)
    check("fmt % / bool", ns["fmt_param"](tp, 0.01) == "1%"
          and ns["fmt_param"](bc.PARAM_BY_KEY["scanner.enabled"], False)
          == "tắt")
    e = ns["estimate_trade"](100, 10, 0.01, 0.03)
    check("lot $1000 TP 1% vao market: net ~ $8.8",
          e["notional"] == 1000 and abs(e["net_tp"] - 8.8) < 1e-9, e)
    e = ns["estimate_trade"](100, 10, 0.01, 0.03, entry_maker=True)
    check("vao limit maker: net ~ $9.1 (khop bang thiet ke $9.2 +- lam tron)",
          abs(e["net_tp"] - 9.1) < 1e-9 and abs(e["net_sl"] + 30.9) < 1e-9, e)
    f = ns["apply_status"]
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    check("chua co version", f(None, None, now)[0] == "unknown")
    r = f(None, {"version": None, "status": "error", "applied_at": now,
                 "error": "Khong tao duoc version dau tien: X"}, now)
    check("chua co version + bot seed loi -> hien loi",
          r[0] == "error" and "version dau tien" in r[1], r)
    check("bot chua bao cao", f(3, None, now)[0] == "unknown")
    check("bot dang chay version moi nhat",
          f(3, {"version": 3, "status": "ok", "applied_at": now}, now)[0]
          == "ok")
    check("vua luu -> cho ap dung",
          f(4, {"version": 3, "status": "ok", "applied_at": now}, now)[0]
          == "pending")
    old = now - timedelta(minutes=5)
    check("qua 2 phut chua nhan -> canh bao",
          f(4, {"version": 3, "status": "ok", "applied_at": old}, now)[0]
          == "error")
    r = f(4, {"version": 4, "status": "error", "applied_at": now,
              "error": "leverage sai"}, now)
    check("bot tu choi -> hien loi", r[0] == "error" and "leverage" in r[1], r)
    u = load("usd")["usd"]
    check("usd am/duong", u(-31.2) == "-$31.20" and u(8.8) == "$8.80")
    check("changed_keys", ns["changed_keys"]({"a": 1, "b": 2},
                                             {"a": 1, "b": 3}) == ["b"]
          and ns["changed_keys"](None, {"a": 1}) == [])
    main_src = SRC[SRC.index("def main():"):]
    check("main: dang nhap TRUOC moi tab du lieu",
          main_src.index("auth_gate()") < main_src.index("st.tabs("))
    check("tab Quan tri chi cho admin",
          'if user["role"] == "admin":\n        names.append' in main_src)
    check("tao user o UI chi qua bc.create_user (kiem admin o DB)",
          SRC.count("bc.create_user") == 1
          and "INSERT INTO dashboard_users" not in SRC)


def test_scanner_tab_levels():
    print("== Scanner tab: cot Tang/phia + canh bao bien hep ==")
    import sys
    root = os.path.dirname(HERE)
    for sub in ("db", "binance-bot"):
        if os.path.join(root, sub) not in sys.path:
            sys.path.insert(0, os.path.join(root, sub))
    import bot_config as bc
    import range_grid

    class FakeSt:
        def __init__(self):
            self.frames, self.captions = [], []

        def caption(self, t):
            self.captions.append(t)

        def dataframe(self, df, **kw):
            self.frames.append(df)

        def markdown(self, *a, **k):
            pass

        warning = info = markdown

    def scan(sym, w, passed=True):
        return {"symbol": sym, "passed": passed, "score": 70, "reasons": [],
                "ts": 0, "metrics": {"range_low": 100 * (1 - w / 2),
                                     "range_high": 100 * (1 + w / 2),
                                     "atr15_pct": None, "range_pct": w}}
    scans = [scan("WIDE", 0.05), scan("THIN", 0.015), scan("FAIL", 0.05,
                                                           False)]

    def db_call(fn, *a):
        return scans if fn is bc.latest_scans else None
    fst = FakeSt()
    ns = load("_tab_scanner", extra={
        "bc": bc, "range_grid": range_grid, "st": fst, "db_call": db_call,
        "pd": type("PD", (), {"DataFrame": staticmethod(lambda r: r)}),
        "_ts_local": lambda t: t})
    ns["_tab_scanner"]()
    rows = {r["Symbol"]: r for r in fst.frames[0]}
    check("cot Tang/phia (step 1%, 2 tang cau hinh)",
          rows["WIDE"]["Tầng/phía"] == 2 and rows["THIN"]["Tầng/phía"] == 0,
          rows)
    check("dat chuan nhung 0 tang -> canh bao hep",
          rows["THIN"]["Đạt"] == "⚠️ hẹp" and rows["WIDE"]["Đạt"] == "✅"
          and rows["FAIL"]["Đạt"] == "—", rows)
    check("chu thich so tang toi thieu", any("< 1 tầng/phía" in c
                                             for c in fst.captions))


TESTS = [test_sol_wallet, test_live_radar_halt_status, test_config_helpers,
         test_scanner_tab_levels]

if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"{PASSED} passed, {FAILED} failed")
    raise SystemExit(1 if FAILED else 0)
