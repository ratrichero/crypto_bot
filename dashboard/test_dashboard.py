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

        warning = info = divider = markdown

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
    ns = load("_tab_scanner", "_trend_section", "trend_table", "TREND_LABEL",
              "load_state", extra={
        "bc": bc, "range_grid": range_grid, "st": fst, "db_call": db_call,
        "pd": type("PD", (), {"DataFrame": staticmethod(lambda r: r)}),
        "_ts_local": lambda t: t, "BINANCE_STATE": "/nonexistent/state.json"})
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


def test_trend_section():
    print("== Scanner tab: muc Loc xu huong grid (task 34) ==")
    import sys
    import tempfile as _tf
    root = os.path.dirname(HERE)
    if os.path.join(root, "db") not in sys.path:
        sys.path.insert(0, os.path.join(root, "db"))
    import bot_config as bc

    class FakeSt:
        def __init__(self):
            self.frames, self.warnings, self.infos, self.md = [], [], [], []

        def dataframe(self, df, **kw):
            self.frames.append(df)

        def warning(self, t):
            self.warnings.append(t)

        def info(self, t):
            self.infos.append(t)

        def markdown(self, t, **k):
            self.md.append(t)

        def caption(self, *a, **k):
            pass

        divider = caption

    with _tf.TemporaryDirectory() as d:
        sp = os.path.join(d, "state.json")
        snap = {"market": "BTCUSDT", "ts": 1.7e9, "symbols": {
            "ETHUSDT": {"bias": "neutral", "slope_atr": 0.1, "reason": "",
                        "ts": 1.7e9, "move_pct": None},
            "BTCUSDT": {"bias": "down", "slope_atr": -0.9, "move_pct": -1.8,
                        "reason": "giảm 1.80% trong ~4h", "ts": 1.7e9},
            "SOLUSDT": {"bias": "up", "slope_atr": 0.8, "reason": "x",
                        "ts": 1.7e9}}}
        json.dump({"trend": snap}, open(sp, "w"))
        fst = FakeSt()
        ns = load("_trend_section", "trend_table", "TREND_LABEL", "load_state",
                  extra={"st": fst, "BINANCE_STATE": sp,
                         "pd": type("PD", (), {
                             "DataFrame": staticmethod(lambda r: r)}),
                         "_ts_local": lambda t: t.strftime("%H:%M")})
        cfg = bc.defaults()
        ns["_trend_section"](cfg)
        rows = fst.frames[0]
        check("BTC (thi truong) dung dau, cot chan mo dung",
              rows[0]["Symbol"] == "BTCUSDT (thị trường)"
              and rows[0]["Chặn mở"] == "LONG"
              and rows[0]["BTC biến động %"] == -1.8
              and {r["Symbol"]: r["Chặn mở"] for r in rows[1:]}
              == {"ETHUSDT": "—", "SOLUSDT": "SHORT"}, rows)
        check("BTC giam + market_filter bat -> canh bao chan long",
              any("BTC đang giảm" in w for w in fst.warnings), fst.warnings)
        check("tieu de: lop bat + tran cung chieu",
              "xu hướng BTC + xu hướng từng coin" in fst.md[0]
              and "trần cùng chiều: 2" in fst.md[0], fst.md)
        fst2 = FakeSt()
        ns["st"] = fst2
        ns["_trend_section"](dict(cfg, **{"trend.market_filter": False,
                                          "trend.symbol_filter": False,
                                          "grid.max_same_side": 0}))
        check("tat loc -> tieu de TAT, khong canh bao BTC",
              "TẮT" in fst2.md[0] and "không giới hạn" in fst2.md[0]
              and not fst2.warnings, (fst2.md, fst2.warnings))
        fst3 = FakeSt()
        ns["st"] = fst3
        ns["BINANCE_STATE"] = os.path.join(d, "nope.json")
        ns["_trend_section"](cfg)
        check("chua co state -> info, khong loi", fst3.infos
              and not fst3.frames)
    check("config tab hien canh bao rui ro + notional cung chieu",
          "bc.risk_warnings(clean)" in SRC
          and "bc.same_side_exposure(clean)" in SRC)


def test_bot_runtime_note():
    print("== Cau hinh: chan doan bot <-> DB tu state.json (task 35) ==")
    ns = load("bot_runtime_note")
    f = ns["bot_runtime_note"]
    check("khong co runtime_config -> None", f({}) is None and f(None) is None)
    lv, msg = f({"runtime_config": {"version": 3, "source": "cache",
                                     "db": "lỗi: connection refused",
                                     "error": None, "ts": 1000}}, now=1010)
    check("DB loi -> error + hien loi DB + version cache",
          lv == "error" and "connection refused" in msg and "version 3" in msg
          and "cache" in msg, (lv, msg))
    lv, msg = f({"runtime_config": {"version": 7, "source": "db", "db": "ok",
                                     "ts": 1000}}, now=1005)
    check("DB ok -> ok", lv == "ok" and "version 7" in msg, (lv, msg))
    lv, msg = f({"runtime_config": {"version": None, "source": "file",
                                     "db": "ok", "ts": 0,
                                     "error": "seed DB loi: x"}}, now=5000)
    check("state cu > 5 phut -> warning bot co the da dung, kem loi seed",
          lv == "warning" and "đã dừng" in msg and "seed DB loi" in msg,
          (lv, msg))
    check("tab Cau hinh goi bot_runtime_note khi chua ok",
          "bot_runtime_note(load_state(BINANCE_STATE))" in SRC)


def test_session_cookie():
    print("== Phien dang nhap: cookie ==")
    import re as _re
    ns = load("SESSION_COOKIE", "_TOKEN_RE", "cookie_js",
              extra={"re": _re, "SESSION_DAYS": 7})
    tok = "A" * 43
    js = ns["cookie_js"](tok, 7)
    check("cookie: ghi tren trang cha, 7 ngay, SameSite=Strict, Path=/",
          "window.parent" in js and "mb_session=%s; Max-Age=604800" % tok in js
          and "SameSite=Strict" in js and "Path=/" in js, js)
    check("cookie: HTTPS thi them Secure", "'; Secure'" in js)
    check("cookie: xoa -> Max-Age=0",
          "mb_session=; Max-Age=0" in ns["cookie_js"](None))
    try:
        ns["cookie_js"]("x'; alert(1);//" + "a" * 20)
        bad = False
    except ValueError:
        bad = True
    check("cookie: token la (chen JS) bi tu choi", bad)
    check("auth: F5 doc cookie qua st.context.cookies",
          "st.context.cookies" in SRC and "bc.session_user" in SRC)



def test_monitor_pm2():
    print("== Monitor: pm2 / systemd ==")
    ns = load("PM2_BIN", "process_manager", "parse_pm2_jlist", "tail_lines",
              "service_states", "service_logs")
    pm = ns["process_manager"]
    check("env PROCESS_MANAGER ghi de", pm({"PROCESS_MANAGER": "systemd",
                                           "DEPLOY_APP": "x"}) == "systemd")
    check("chay duoi deploy/pm2 -> pm2", pm({"DEPLOY_APP": "muse-dashboard"})
          == "pm2" and pm({"pm_id": "3"}) == "pm2")
    check("mac dinh systemd", pm({}) == "systemd")

    with tempfile.TemporaryDirectory() as d:
        out_log = os.path.join(d, "out.log")
        err_log = os.path.join(d, "err.log")
        with open(out_log, "w") as f:
            f.write("".join("dong %d\n" % i for i in range(30)))
        with open(err_log, "w") as f:
            f.write("Traceback loi\n")
        data = [
            {"name": "pm2-logrotate", "pid": 9,
             "pm2_env": {"pmx_module": True, "status": "online"}},
            {"name": "muse-binance", "pid": 11, "pm2_env": {
                "status": "online", "restart_time": 2,
                "pm_out_log_path": out_log, "pm_err_log_path": err_log}},
            {"name": "muse-radar", "pid": 0, "pm2_env": {
                "status": "waiting restart", "exit_code": 0,
                "stop_exit_codes": [0, 78]}},
            {"name": "muse-live-trader", "pid": 0, "pm2_env": {
                "status": "errored", "exit_code": 1}},
        ]
        raw = "[PM2][WARN] In-memory PM2 is out-of-date\n" + json.dumps(data)
        procs = ns["parse_pm2_jlist"](raw)
        check("jlist: bo canh bao + module", set(procs) == {
            "muse-binance", "muse-radar", "muse-live-trader"}, procs)
        check("jlist: exit 0 'waiting restart' -> stopped",
              procs["muse-radar"]["status"] == "stopped")
        check("jlist: output rac -> {}", ns["parse_pm2_jlist"]("loi") == {})

        class R:
            def __init__(self, out):
                self.stdout = out

        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return R(raw)
        names = ["muse-binance", "muse-radar", "muse-live-trader",
                 "muse-dashboard"]
        st = ns["service_states"](names, "pm2", runner=runner)
        check("pm2: online", st["muse-binance"][0] is True)
        check("pm2: stopped/errored/khong co -> khong chay + ly do",
              st["muse-radar"] == (False, "pm2 stopped (exit 0)")
              and "errored" in st["muse-live-trader"][1]
              and st["muse-dashboard"] == (False, "chua co trong pm2"), st)
        check("pm2: goi jlist 1 lan cho moi service", len(calls) == 1)

        def boom(cmd, **kw):
            raise OSError("khong co pm2")
        st = ns["service_states"](["muse-binance"], "pm2", runner=boom)
        check("pm2 loi -> khong chay, khong crash",
              st["muse-binance"][0] is False and "pm2" in
              st["muse-binance"][1])

        logs = ns["service_logs"]("muse-binance", "pm2", 20, runner=runner)
        check("pm2 log: 20 dong cuoi stdout + stderr",
              logs.startswith("dong 10") and "dong 29" in logs
              and "--- stderr ---\nTraceback loi" in logs, logs[:80])

        def sysd(cmd, **kw):
            if cmd[0] == "systemctl":
                return R("active\n" if cmd[2] == "muse-binance"
                         else "inactive\n")
            return R("Oct 06 22:23:14 ip-1 python[123]: [x] hello\n")
        st = ns["service_states"](["muse-binance", "muse-radar"], "systemd",
                                  runner=sysd)
        check("systemd van chay nhu cu", st["muse-binance"][0] is True
              and st["muse-radar"] == (False, "systemd inactive"))
        check("systemd log: cat tien to journal",
              ns["service_logs"]("muse-binance", "systemd", runner=sysd)
              == "[x] hello")


def test_helius_key():
    print("[dashboard: tim key Helius theo goc repo]")
    ns = load("resolve_helius_key", "first_existing")
    f = ns["resolve_helius_key"]
    with tempfile.TemporaryDirectory() as repo:
        os.makedirs(os.path.join(repo, "meme-radar"))
        home = os.path.join(repo, "home")
        os.makedirs(home)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = home             # khong cham ~/workspace that
        try:
            check("khong co key o dau -> rong", f(env={}, repo=repo) == ("", ""))
            kp = os.path.join(repo, "meme-radar", ".helius_key")
            with open(kp, "w") as fh:
                fh.write("  HKEY-RADAR \n")
            check("loi VPS: key o <repo>/meme-radar/.helius_key -> tim thay",
                  f(env={}, repo=repo) == ("HKEY-RADAR", kp))
            other = os.path.join(repo, "khac.key")
            with open(other, "w") as fh:
                fh.write("HKEY-FILE")
            check("env HELIUS_KEY_FILE uu tien hon file trong repo",
                  f(env={"HELIUS_KEY_FILE": other}, repo=repo)[0] == "HKEY-FILE")
            check("env HELIUS_KEY_FILE tro file khong ton tai -> tim tiep",
                  f(env={"HELIUS_KEY_FILE": "/khong/co"}, repo=repo)[0]
                  == "HKEY-RADAR")
            check("env HELIUS_API_KEY uu tien nhat (giong radar.py)",
                  f(env={"HELIUS_API_KEY": "HENV", "HELIUS_KEY_FILE": other},
                    repo=repo) == ("HENV", "env HELIUS_API_KEY"))
            with open(kp, "w") as fh:
                fh.write("\n")
            with open(os.path.join(repo, ".helius_key"), "w") as fh:
                fh.write("HKEY-ROOT")
            check("file rong -> bo qua, sang <repo>/.helius_key",
                  f(env={}, repo=repo)[0] == "HKEY-ROOT")
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
    code = "\n".join(l for l in SRC.splitlines()
                     if not l.lstrip().startswith("#"))
    check("app.py khong con duong dan viet cung /home/ubuntu/muse_bot",
          "/home/ubuntu/muse_bot" not in code)


def test_live_fee():
    print("== Live radar: phi mang / truoc phi (doi chieu app vi) ==")
    ns = load("live_trade_fee", "live_fee_summary")
    f, summ = ns["live_trade_fee"], ns["live_fee_summary"]
    check("lenh cu (khong fee) -> None", f({"realized_usd": 1}) == (None, None))
    check("fee_known False -> None",
          f({"realized_usd": 1, "fee_usd": 0.1, "fee_known": False})
          == (None, None))
    fee, gross = f({"realized_usd": -0.38, "fee_usd": 0.09, "fee_known": True})
    check("truoc phi = rong + phi", abs(fee - 0.09) < 1e-9
          and abs(gross - (-0.29)) < 1e-9, (fee, gross))
    check("fee rac -> None", f({"fee_usd": "x", "fee_known": True})
          == (None, None))
    n, fe, net, gr = summ([
        {"realized_usd": -0.38, "fee_usd": 0.09, "fee_known": True},
        {"realized_usd": 7.25, "fee_usd": 0.11, "fee_known": True},
        {"realized_usd": 1.0}])
    check("tong chi tinh lenh co phi", n == 2 and abs(fe - 0.2) < 1e-9
          and abs(net - 6.87) < 1e-9 and abs(gr - 7.07) < 1e-9,
          (n, fe, net, gr))


TESTS = [test_session_cookie, test_helius_key, test_sol_wallet, test_live_radar_halt_status, test_config_helpers,
         test_scanner_tab_levels, test_trend_section, test_bot_runtime_note,
         test_monitor_pm2, test_live_fee]


if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"{PASSED} passed, {FAILED} failed")
    raise SystemExit(1 if FAILED else 0)
