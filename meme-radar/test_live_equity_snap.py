#!/usr/bin/env python3
"""Test live_equity_snap (offline; phan DB dung pgserver neu co)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_equity_snap as les  # noqa: E402

PASS = FAIL = 0


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   " + name)
    else:
        FAIL += 1
        print("FAIL " + name + ("  -> %s" % (info,) if info else ""))


SOL = les.SOL_MINT
L = 10 ** 9

# ---- position_tokens / compute_equity
p = {"token": "ART", "tokens_base": 40_000_000_000, "decimals": 6,
     "remaining": 0.5, "entry": 0.000254}
check("token con giu = base x remaining / 10^dec",
      les.position_tokens(p) == 20_000.0, les.position_tokens(p))
check("du lieu rac -> 0", les.position_tokens({"tokens_base": "x"}) == 0.0)
eq, det = les.compute_equity(2 * L, 150.0, [p], {"ART": 0.0003, SOL: 150.0})
check("equity = SOL*gia + token*gia", abs(eq - (300 + 6.0)) < 1e-9, eq)
eq2, det2 = les.compute_equity(2 * L, 150.0, [p], {SOL: 150.0})
check("token thieu gia -> gia vao, dem missing",
      abs(eq2 - (300 + 20_000 * 0.000254)) < 1e-9
      and det2["missing_price"] == 1, (eq2, det2))
eq3, _ = les.compute_equity(L, 100.0, [dict(p, remaining=0)], {})
check("vi the da ban het -> khong tinh", eq3 == 100.0)
check("parse_prices bo gia 0/rac",
      les.parse_prices({"A": {"usdPrice": 1.5}, "B": {"usdPrice": 0},
                        "C": None, "D": {"usdPrice": "x"}},
                       ["A", "B", "C", "D", "E"]) == {"A": 1.5})


# ---- Snapper voi HTTP gia
class Resp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise les.requests.HTTPError("http %d" % self.status_code)


class Sess:
    def __init__(self, key_status=200):
        self.key_status = key_status
        self.gets = []

    def post(self, url, json=None, timeout=None):
        return Resp(200, {"result": {"value": 3 * L}})

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, bool(headers)))
        if headers and self.key_status != 200:
            return Resp(self.key_status, {})
        return Resp(200, {SOL: {"usdPrice": 200.0},
                          "ART": {"usdPrice": 0.0003}})


class Conn:
    def __init__(self, fail=False):
        self.rows, self.fail, self.closed = [], fail, False

    def execute(self, sql, params):
        if self.fail:
            raise RuntimeError("db down")
        self.rows.append(params)

    def close(self):
        self.closed = True


conns = []


def mk_conn():
    c = Conn()
    conns.append(c)
    return c


sess = Sess()
sn = les.Snapper("HK", "WALLET", "JK", session=sess, connect=mk_conn)
eq = sn.once([p])
check("once: ghi 1 snapshot radar_live", eq is not None and conns
      and conns[0].rows == [("radar_live", 600.0 + 6.0)], conns[0].rows)
check("co key -> api.jup.ag + header", sess.gets[0] == (les.JUP_KEYED, True))
sn.once([p])
check("dung lai ket noi DB", len(conns) == 1 and len(conns[0].rows) == 2)

sess401 = Sess(key_status=401)
sn2 = les.Snapper("HK", "WALLET", "BAD", session=sess401, connect=mk_conn)
sn2.once([])
check("Jupiter 401 -> lite-api, tat key 30 phut",
      sess401.gets[-1] == (les.JUP_FREE, False)
      and sn2.keyed_off_until > les.time.time() + 1700, sess401.gets)
sn2.once([])
check("lan sau khong thu key nua", sess401.gets[-1] == (les.JUP_FREE, False)
      and len(sess401.gets) == 3, sess401.gets)

bad = Conn(fail=True)
sn3 = les.Snapper("HK", "W", "", session=Sess(), connect=lambda: bad)
try:
    sn3.once([])
    check("DB loi -> raise", False)
except RuntimeError:
    check("DB loi -> raise, dong ket noi, lan sau ket noi lai",
          bad.closed and sn3._conn is None)

nosol = Sess()
nosol.get = lambda *a, **k: Resp(200, {})
check("khong co gia SOL -> skip, khong ghi",
      les.Snapper("HK", "W", "", session=nosol,
                  connect=mk_conn).once([]) is None)

with tempfile.TemporaryDirectory() as d:
    pp = os.path.join(d, "p.json")
    open(pp, "w").write("[{\"token\": \"A\"}]")
    check("load_positions", les.load_positions(pp) == [{"token": "A"}])
    open(pp, "w").write("{rac")
    check("load_positions file hong -> []", les.load_positions(pp) == [])
    check("load_positions khong co file -> []",
          les.load_positions(os.path.join(d, "x")) == [])

check("jupiter_key tu env", les.jupiter_key({"JUPITER_API_KEY": " k "})
      == "k")

# ---- DB that (pgserver): bang equity_snapshots trong db/schema.sql
try:
    import pgserver
    import psycopg
except ImportError:
    pgserver = None
if pgserver is None:
    print("SKIP DB (khong co pgserver/psycopg)")
else:
    with tempfile.TemporaryDirectory() as d:
        srv = pgserver.get_server(d)
        uri = srv.get_uri()
        here = os.path.dirname(os.path.abspath(__file__))
        with psycopg.connect(uri, autocommit=True) as c:
            c.execute(open(os.path.join(here, "..", "db",
                                        "schema.sql")).read())
        sn4 = les.Snapper("HK", "W", "", db_url=uri, session=Sess())
        sn4.once([p])
        with psycopg.connect(uri) as c:
            r = c.execute("SELECT system, equity FROM equity_snapshots"
                          ).fetchall()
        check("ghi vao Postgres that", r == [("radar_live", 606.0)], r)
        sn4._conn.close()
        srv.cleanup()

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
