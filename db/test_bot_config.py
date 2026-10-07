"""Test config runtime + tai khoan dashboard + scanner tren Postgres THAT.

Can: pip install pgserver "psycopg[binary]" (khong co -> SKIP phan DB).
Chay tu goc repo: python db/test_bot_config.py
"""
import copy
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "db"))
sys.path.insert(0, os.path.join(ROOT, "binance-bot"))
import bot_config as bc  # noqa: E402

ok = []


def check(n, c, x=""):
    ok.append(bool(c))
    print(("PASS " if c else "FAIL ") + n + ("" if c else " | %s" % (x,)))


def raises(fn, exc=Exception):
    try:
        fn()
    except exc as e:
        return str(e) or True
    return False


# ------------------------------------------------ thuan (khong can DB)
d = bc.defaults()
check("default lot = 100 x 10 = $1000, TP 1%",
      d["order_margin_usdt"] * d["leverage"] == 1000 and d["grid.tp_pct"] == 0.01)
check("default trần lỗ tổng grid 10%", d["risk.grid_total_max_loss_pct"] == 0.10)
clean, err = bc.validate(d)
check("default hop le", not err, err)
_, err = bc.validate(dict(d, **{"grid.tp_pct": 0.5}))
check("TP 50% bi chan (ngoai bien)", err and "grid.tp_pct" in err[0], err)
_, err = bc.validate(dict(d, **{"leverage": 2.5}))
check("leverage khong nguyen bi chan", err, err)
_, err = bc.validate(dict(d, **{"leverage": True}))
check("bool khong duoc coi la so", err, err)
_, err = bc.validate(dict(d, **{"grid.step_min": 0.01, "grid.step_max": 0.005}))
check("rang buoc cheo step_min <= step_max", err and "Độ giãn" in err[0], err)
_, err = bc.validate(dict(d, **{"risk.grid_basket_max_loss_pct": 0.2}))
check("basket <= trần tổng grid", err, err)
check("default range_min_levels = 1", d["grid.range_min_levels"] == 1)
_, err = bc.validate(dict(d, **{"grid.range_min_levels": 3}))
check("range_min_levels <= levels_each_side", err and "tầng tối thiểu"
      in err[0], err)
_, err = bc.validate(dict(d, **{"khong.ton.tai": 1}))
check("khoa la bi tu choi", err and "không tồn tại" in err[0], err)
_, err = bc.validate(dict(d, **{"scanner.mode": "yolo"}))
check("enum sai bi chan", err, err)
cfg = {"grid": {"step_pct": 0.005}, "leverage": 5}
changed = bc.apply(cfg, dict(clean))
check("apply ghi tai cho + giu khoa khong quan ly",
      cfg["grid"]["step_pct"] == 0.005 and cfg["grid"]["tp_pct"] == 0.01
      and cfg["leverage"] == 10 and "leverage" in changed)
check("apply lan 2 khong doi gi", bc.apply(cfg, clean) == [])
check("extract thieu khoa -> default schema",
      bc.extract({"leverage": 7})["leverage"] == 7
      and bc.extract({})["grid.tp_pct"] == 0.01)
check("diff liet ke dung khoa doi",
      [k for k, _, _ in bc.diff(d, dict(d, leverage=5))] == ["leverage"])
h = bc.hash_password("matkhau123")
check("scrypt hash + verify", bc.verify_password("matkhau123", h)
      and not bc.verify_password("sai", h) and "matkhau123" not in h)
check("2 hash cung mat khau khac salt", h != bc.hash_password("matkhau123"))
check("credentials: ten/mk ngan bi chan",
      len(bc.check_new_credentials("a", "123")) == 2)
check("credentials: nhap lai khong khop",
      bc.check_new_credentials("admin", "12345678", "x") != [])

# schema.sql dong bo voi DDL
schema = open("db/schema.sql").read()
tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", bc.DDL)
check("schema.sql co du bang cua bot_config.DDL",
      tables and all(("CREATE TABLE IF NOT EXISTS %s" % t) in schema
                     for t in tables), tables)

# --------------------------------------------------------- DB that
try:
    import pgserver
    import psycopg
except ImportError:
    print("SKIP phan DB: chua cai pgserver/psycopg")
    pgserver = None

if pgserver is not None:
    tmp = tempfile.mkdtemp()
    srv = pgserver.get_server(tmp, cleanup_mode="stop")
    url = srv.get_uri()
    conn = psycopg.connect(url, autocommit=True)
    bc.ensure_tables(conn)
    bc.ensure_tables(conn)
    check("ensure_tables idempotent", True)
    with psycopg.connect(url, autocommit=True) as c2:
        c2.execute(schema)
        c2.execute(schema)
    check("schema.sql chay lai duoc (idempotent)", True)

    # ---- tai khoan
    check("ban dau chua co user", bc.user_count(conn) == 0)
    check("tao admin dau tien", bc.create_first_admin(conn, "cuong", "matkhau123"))
    check("lan 2 KHONG tao duoc admin dau tien (da co user)",
          bc.create_first_admin(conn, "hacker", "matkhau123") is False
          and bc.get_user(conn, "hacker") is None)
    u, msg = bc.authenticate(conn, "cuong", "matkhau123")
    check("dang nhap dung", u and u["role"] == "admin"
          and "password_hash" not in u, msg)
    u, msg = bc.authenticate(conn, "khongco", "x")
    check("user khong ton tai -> thong bao chung", u is None
          and "Sai tên" in msg)
    bc.create_user(conn, "cuong", "minh", "matkhau456", "viewer")
    check("admin tao viewer", bc.get_user(conn, "minh")["role"] == "viewer")
    check("viewer tao user -> PermissionError",
          raises(lambda: bc.create_user(conn, "minh", "x1x", "matkhau456"),
                 PermissionError))
    check("trung ten -> loi",
          raises(lambda: bc.create_user(conn, "cuong", "minh", "matkhau456"),
                 ValueError))
    # lockout
    t0 = datetime.now(timezone.utc)
    for i in range(bc.MAX_FAILED):
        bc.authenticate(conn, "minh", "sai-mat-khau", now=t0)
    u, msg = bc.authenticate(conn, "minh", "matkhau456", now=t0)
    check("sai 5 lan -> tam khoa ca khi dung mat khau", u is None
          and "Tạm khoá" in msg, msg)
    u, msg = bc.authenticate(conn, "minh", "matkhau456",
                             now=t0 + timedelta(minutes=16))
    check("het 15 phut -> dang nhap lai duoc", u is not None, msg)
    # khoa/mo, admin cuoi
    check("khong khoa duoc admin cuoi",
          "cuối" in str(raises(lambda: bc.set_active(conn, "cuong", "cuong",
                                                     False), ValueError)))
    check("khong ha quyen admin cuoi",
          raises(lambda: bc.set_role(conn, "cuong", "cuong", "viewer"),
                 ValueError))
    bc.set_active(conn, "cuong", "minh", False)
    u, msg = bc.authenticate(conn, "minh", "matkhau456")
    check("user bi khoa khong dang nhap duoc", u is None and "khoá" in msg)
    bc.set_active(conn, "cuong", "minh", True)
    bc.set_role(conn, "cuong", "minh", "admin")
    bc.set_role(conn, "cuong", "cuong", "viewer")
    check("co 2 admin -> ha quyen 1 admin duoc",
          bc.get_user(conn, "cuong")["role"] == "viewer")
    bc.set_role(conn, "minh", "cuong", "admin")
    bc.reset_password(conn, "minh", "cuong", "matkhaumoi1")
    check("admin reset mat khau", bc.authenticate(conn, "cuong",
                                                  "matkhaumoi1")[0])
    bc.set_role(conn, "cuong", "minh", "viewer")
    check("viewer khong reset mk nguoi khac",
          raises(lambda: bc.reset_password(conn, "minh", "cuong", "12345678x"),
                 PermissionError))
    bc.reset_password(conn, "minh", "minh", "matkhau789")
    check("user tu doi mk cua minh", bc.authenticate(conn, "minh",
                                                     "matkhau789")[0])
    check("list_users khong tra password_hash",
          all("password_hash" not in r for r in bc.list_users(conn)))

    # ---- RuntimeConfig phia bot
    import runtime_config as rc

    logs = []
    cache = os.path.join(tmp, "cache.json")
    file_cfg = {"mode": "live", "leverage": 10, "order_margin_usdt": 100,
                "max_total_positions": 10,
                "grid": {"step_pct": 0.005, "levels_each_side": 5,
                         "max_positions": 7, "step_min": 0.004,
                         "step_max": 0.008, "step_mult": 0.8,
                         "range_steps": 6, "max_entries_per_cycle": 1},
                "risk": {"daily_max_loss_pct": 0.2,
                         "grid_basket_max_loss_pct": 0.02,
                         "grid_total_max_loss_pct": 0.10,
                         "max_notional_mult": 10.0}}
    CFG = copy.deepcopy(file_cfg)
    clock = [1000.0]
    rt = rc.RuntimeConfig(CFG, db=rc.DBLink(url=url, log=logs.append),
                          log=logs.append, cache_path=cache,
                          clock=lambda: clock[0])
    rt.start()
    v1 = bc.latest_version(conn)
    check("DB rong -> bot seed version 1 tu config.json + default",
          v1 == 1 and bc.load_version(conn)["author"] == "bot-seed"
          and rt.version == 1 and rt.source == "db")
    check("seed giu gia tri VPS (daily 20%) + them tp_pct 1%",
          CFG["risk"]["daily_max_loss_pct"] == 0.2
          and CFG["grid"]["tp_pct"] == 0.01 and CFG["mode"] == "live")
    check("ghi cache + bot_config_applied ok",
          json.load(open(cache))["version"] == 1
          and bc.applied(conn)["status"] == "ok"
          and bc.applied(conn)["version"] == 1)

    new = dict(bc.load_version(conn)["config"])
    new["order_margin_usdt"] = 150
    new["grid.tp_pct"] = 0.012
    v2 = bc.save_version(conn, new, author="cuong", note="tang lot")
    check("poll truoc 10s khong hoi DB", rt.poll() is False and rt.version == 1)
    clock[0] += 11
    check("poll sau 10s ap dung version moi", rt.poll() is True
          and rt.version == v2 and CFG["order_margin_usdt"] == 150
          and CFG["grid"]["tp_pct"] == 0.012)
    check("save_version tu choi config sai (khong ghi)",
          raises(lambda: bc.save_version(conn, dict(new, leverage=99),
                                         author="cuong"), ValueError)
          and bc.latest_version(conn) == v2)
    # version sai lot qua (vd ghi tay vao DB) -> bot giu ban cu
    with conn.transaction():
        conn.execute("INSERT INTO bot_config_versions (bot, config, author) "
                     "VALUES ('binance', %s::jsonb, 'tay')",
                     (json.dumps(dict(new, leverage=99)),))
    clock[0] += 11
    check("version sai -> giu ban cu + ghi error",
          rt.poll() is False and CFG["leverage"] == 10
          and rt.version == v2 and bc.applied(conn)["status"] == "error"
          and "leverage" in (bc.applied(conn)["error"] or ""))
    n_logs = len(logs)
    clock[0] += 11
    rt.poll()
    check("khong log lai version da tu choi", len(logs) == n_logs)
    # file reload giu override DB
    rt.reload_file(dict(copy.deepcopy(file_cfg), fee_rate=0.0004))
    check("reload config.json giu override DB",
          CFG["order_margin_usdt"] == 150 and CFG["fee_rate"] == 0.0004
          and CFG["grid"]["tp_pct"] == 0.012)
    v4 = bc.save_version(conn, dict(new, leverage=8), author="cuong")
    clock[0] += 11
    check("version hop le sau version sai -> ap dung", rt.poll()
          and CFG["leverage"] == 8 and rt.version == v4)

    # DB chet luc khoi dong -> cache
    CFG2 = copy.deepcopy(file_cfg)
    rt2 = rc.RuntimeConfig(CFG2, db=rc.DBLink(url="postgresql://x@127.0.0.1:1/x",
                                               log=logs.append),
                           log=logs.append, cache_path=cache)
    rt2.start()
    check("DB loi -> dung cache last-known-good", rt2.source == "cache"
          and rt2.version == v4 and CFG2["leverage"] == 8)
    CFG3 = copy.deepcopy(file_cfg)
    rt3 = rc.RuntimeConfig(CFG3, db=rc.DBLink(url="", log=logs.append),
                           log=logs.append,
                           cache_path=os.path.join(tmp, "khongco.json"))
    rt3.start()
    check("khong DB, khong cache -> config.json + default",
          rt3.source == "file" and CFG3["leverage"] == 10
          and CFG3["grid"]["tp_pct"] == 0.01)
    # seed khong hop le (config.json ngoai bien) -> khong seed, khong vo
    tmp2 = tempfile.mkdtemp()
    srv2 = pgserver.get_server(tmp2, cleanup_mode="stop")
    CFG4 = dict(copy.deepcopy(file_cfg), leverage=50)
    rt4 = rc.RuntimeConfig(CFG4, db=rc.DBLink(url=srv2.get_uri(),
                                               log=logs.append),
                           log=logs.append,
                           cache_path=os.path.join(tmp2, "c.json"))
    rt4.start()
    with psycopg.connect(srv2.get_uri(), autocommit=True) as c4:
        check("config.json ngoai bien -> khong seed, ghi loi, giu file",
              bc.latest_version(c4) is None and CFG4["leverage"] == 50
              and bc.applied(c4)["status"] == "error")

    # ---- scanner DB
    bc.insert_scan(conn, {"symbol": "BTCUSDT", "ts": 1e9, "passed": False,
                          "score": 10, "metrics": {}, "reasons": ["cu"]})
    import time as _t
    bc.insert_scan(conn, {"symbol": "BTCUSDT", "ts": _t.time(), "passed": True,
                          "score": 80, "metrics": {"adx_1h": 12},
                          "reasons": []})
    bc.insert_scan(conn, {"symbol": "ETHUSDT", "ts": _t.time(), "passed": False,
                          "score": 40, "metrics": {}, "reasons": ["ADX"]})
    rows = bc.latest_scans(conn)
    check("latest_scans: moi symbol 1 dong moi nhat, dat chuan truoc",
          [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT"]
          and rows[0]["passed"] and rows[0]["metrics"]["adx_1h"] == 12, rows)
    check("prune_scans xoa ban ghi cu", bc.prune_scans(conn, 14) == 1)
    conn.close()

print("\n%d passed, %d failed" % (sum(ok), len(ok) - sum(ok)))
sys.exit(0 if all(ok) else 1)
