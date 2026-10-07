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


TESTS = [test_sol_wallet]

if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"{PASSED} passed, {FAILED} failed")
    raise SystemExit(1 if FAILED else 0)
