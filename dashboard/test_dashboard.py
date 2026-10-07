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


TESTS = [test_sol_wallet, test_live_radar_halt_status]

if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"{PASSED} passed, {FAILED} failed")
    raise SystemExit(1 if FAILED else 0)
