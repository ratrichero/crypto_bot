"""Test binance_trades tren Postgres THAT (embedded pgserver).

Kiem: schema.sql idempotent tren DB cu, bot insert cot phi/nguon gia,
sync_jsonl khong ghi de du lieu bot, migrate loi -> insert cot cu.
Can: pip install pgserver "psycopg[binary]" (khong co -> SKIP).
Chay tu goc repo: python db/test_binance_trades_pg.py
"""
import os, sys, json, tempfile, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
try:
    import pgserver
except ImportError:
    print("SKIP: chua cai pgserver")
    sys.exit(0)
d = tempfile.mkdtemp()
srv = pgserver.get_server(d, cleanup_mode="stop")
url = srv.get_uri()
os.environ["DATABASE_URL"] = url
import psycopg
ok = []
def check(n, c, x=""):
    ok.append(c); print(("PASS " if c else "FAIL ") + n + ("" if c else " | %s" % (x,)))
with psycopg.connect(url, autocommit=True) as c:
    # DB CU: schema truoc commit (khong co cot moi)
    c.execute("""CREATE TABLE binance_trades (id BIGINT PRIMARY KEY, symbol TEXT NOT NULL,
      side TEXT, tag TEXT, entry DOUBLE PRECISION, exit DOUBLE PRECISION, notional DOUBLE PRECISION,
      pnl DOUBLE PRECISION, reason TEXT, closed_at TIMESTAMPTZ NOT NULL, live BOOLEAN, dry BOOLEAN, close_ord TEXT)""")
    c.execute("INSERT INTO binance_trades (id,symbol,closed_at,pnl) VALUES (1,'OLD',now(),1.0)")
# schema.sql chay lai tren DB cu va DB moi
sql = open("db/schema.sql").read()
with psycopg.connect(url, autocommit=True) as c:
    c.execute(sql); c.execute(sql)
    cols = [r[0] for r in c.execute("select column_name from information_schema.columns where table_name='binance_trades'")]
check("schema.sql idempotent tren DB cu, co du cot moi",
      all(x in cols for x in ("pnl_gross","fee_entry","fee_exit","fee_estimated","estimated","exit_source")), cols)

# --- bot insert
sys.path.insert(0, "binance-bot")
import live_binance
eng = live_binance.BinanceEngine.__new__(live_binance.BinanceEngine)
eng._db_conn = None; eng.log = print
rec = {"id": 7, "symbol": "BTCUSDT", "side": "long", "tag": "grid", "entry": 60000.0, "exit": 60300.0,
       "notional": 600.0, "pnl": 2.4788, "reason": "TP", "closed_at": 1800000000, "live": True, "dry": False,
       "close_ord": "5005", "pnl_gross": 3.0, "fee_entry": 0.24, "fee_exit": 0.2412, "fee_estimated": False,
       "estimated": False, "exit_source": "exchange_algo"}
check("bot insert mo rong OK", eng._db_insert_trade(rec) is True)
# --- sync insert cung id (JSONL) va id moi
sys.path.insert(0, "db")
import sync_jsonl
p = tempfile.mktemp(suffix=".jsonl")
with open(p, "w") as f:
    f.write(json.dumps(dict(rec, pnl=999)) + "\n")
    f.write(json.dumps({"id": 8, "symbol": "ETHUSDT", "side": "short", "tag": "scalp", "entry": 3000, "exit": 2990,
                        "notional": 300, "pnl": 1.0, "reason": "TP", "closed_at": 1800000100, "live": True,
                        "dry": False, "close_ord": "9"}) + "\n")
n = sync_jsonl.sync_binance(p, {})
check("sync insert 2 dong", n == 2, n)
with psycopg.connect(url) as c:
    r7 = c.execute("select pnl, pnl_gross, fee_entry, fee_exit, fee_estimated, estimated, exit_source from binance_trades where id=7").fetchone()
    r8 = c.execute("select pnl, pnl_gross, estimated, exit_source from binance_trades where id=8").fetchone()
check("id 7: bot ghi truoc, sync khong ghi de pnl", r7 == (2.4788, 3.0, 0.24, 0.2412, False, False, "exchange_algo"), r7)
check("id 8: record cu khong co cot moi -> NULL", r8 == (1.0, None, None, None), r8)
# --- migrate loi -> legacy
eng2 = live_binance.BinanceEngine.__new__(live_binance.BinanceEngine)
eng2._db_conn = None; eng2.log = print
orig = live_binance.DB_MIGRATE_SQL
live_binance.DB_MIGRATE_SQL = ["ALTER TABLE khong_ton_tai ADD COLUMN x INT"]
check("bot: migrate loi -> van insert bang cot cu", eng2._db_insert_trade(dict(rec, id=9)) is True)
live_binance.DB_MIGRATE_SQL = orig
with psycopg.connect(url) as c:
    check("id 9 da ghi", c.execute("select count(*) from binance_trades where id=9").fetchone()[0] == 1)
srv.cleanup(); shutil.rmtree(d, ignore_errors=True)
print("%d passed, %d failed" % (sum(ok), len(ok) - sum(ok)))
sys.exit(0 if all(ok) else 1)
