#!/usr/bin/env python3
"""Test scanner_whatif: cham lai ket qua scan voi nguong khac (chi doc).

Run: python3 test_scanner_whatif.py
"""
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

import scanner as sc  # noqa: E402
import scanner_whatif as sw  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + ("" if cond else "  " + str(detail)))


def ou(n, theta, sigma, seed, drift=0.0, ms=3600000):
    rnd = random.Random(seed)
    x, out, mu = 100.0, [], 100.0
    for i in range(n):
        m = mu * (1 + drift) ** i
        o, path = x, [x]
        for _ in range(4):
            x = x + theta / 4 * (m - x) + rnd.gauss(0, sigma / 2) * m / 100
            path.append(x)
        out.append({"ts": i * ms, "o": o, "h": max(path), "l": min(path),
                    "c": x})
    return out


results = []
rnd = random.Random(7)
for i in range(60):
    theta = rnd.choice([0.0, 0.05, 0.15, 0.3])
    sigma = rnd.choice([0.4, 0.7, 1.0])
    drift = rnd.choice([0.0, 0.0, 0.0005, -0.001])
    results.append(sc.evaluate("C%02dUSDT" % i,
                               ou(500, theta, sigma, i, drift),
                               ou(100, theta, sigma / 2, 1000 + i, drift / 4,
                                  900000)))
n_pass = sum(r["passed"] for r in results)
check("du lieu test co ca dat lan truot", 0 < n_pass < len(results), n_pass)
j = sw.rejudge(results, dict(sc.DEFAULTS))
same = sum(j[r["symbol"]]["passed"] == r["passed"] for r in results)
check("cham lai tu metrics (da lam tron) == ket qua goc",
      same == len(results), (same, len(results)))
diff = [r["symbol"] for r in results if [sw.criterion(x) for x in
                                         r["reasons"]] != j[r["symbol"]]["fails"]]
check("ly do cham lai ~ ly do goc (lech chi o sat nguong do lam tron)",
      len(diff) <= max(1, len(results) // 20), diff)
check("criterion(): moi ly do judge -> ten tieu chi da biet",
      all(sw.criterion(x) in dict(sw.CRITERIA)
          for r in results for x in r["reasons"]))

res = sw.analyse(results, dict(sc.DEFAULTS), {}, {"adx_threshold": 24})
counts = [len(v["passed"]) for v in res["variants"]]
check("muc noi cong don -> so coin dat khong giam", counts == sorted(counts)
      and counts[0] == n_pass, counts)
check("+ranging15m <= dat", all(len(v["grid_ok"]) <= len(v["passed"])
                                for v in res["variants"]))
b = res["blockers"]
only_adx = set(b.get("ADX 1h", {}).get("only", []))
j2 = sw.rejudge(results, dict(sc.DEFAULTS, adx_1h_max=99))
check("'chi truot ADX 1h' -> noi rieng ADX 1h thi cac coin do dat",
      all(j2[s]["passed"] for s in only_adx), only_adx)
res2 = sw.analyse(results, dict(sc.DEFAULTS), {}, {},
                  extra={"adx_1h_max": 99.0})
check("--set them 1 muc rieng", res2["variants"][-1]["name"] == "--set"
      and len(res2["variants"][-1]["passed"]) >= n_pass + len(only_adx))
st = {"regimes": {r["symbol"]: {"regime": "trending"} for r in results}}
res3 = sw.analyse(results, dict(sc.DEFAULTS), st, {})
check("regime state.json trending het -> +ranging15m = 0",
      all(not v["grid_ok"] for v in res3["variants"]))
check("raw_from_metrics: thieu metrics -> None -> 'thiếu dữ liệu'",
      sw.rejudge([{"symbol": "X", "metrics": {}}], {})["X"]["fails"]
      == ["thiếu"])
try:
    sw._parse_set(["khong_co=1"])
    check("--set key sai -> loi", False)
except SystemExit:
    check("--set key sai -> loi", True)

# ---------------------------------------------------------------- CLI
tmp = tempfile.mkdtemp()
try:
    base = os.path.join(tmp, "binance-bot")
    os.makedirs(base)
    os.symlink(os.path.join(BASE, "..", "db"), os.path.join(tmp, "db"))
    json.dump(json.load(open(os.path.join(BASE, "config.example.json"))),
              open(os.path.join(base, "config.json"), "w"))
    json.dump({"ts": time.time(), "results": sc.rank(results)},
              open(os.path.join(base, "scanner_latest.json"), "w"))
    json.dump({"regimes": {}}, open(os.path.join(base, "state.json"), "w"))
    before = {f: open(os.path.join(base, f), "rb").read()
              for f in os.listdir(base)}
    buf = []
    rc = sw.run_cli(["--base", base, "--set", "adx_1h_max=30"],
                    out=buf.append)
    text = "\n".join(buf)
    check("CLI chay OK + bang tieu chi + cac muc", rc == 0
          and "TIEU CHI" in text and "noi vua" in text and "--set" in text,
          text[:400])
    buf2 = []
    sw.run_cli(["--base", base, "--json"], out=buf2.append)
    check("CLI --json", json.loads(buf2[0])["n"] == len(results))
    after = {f: open(os.path.join(base, f), "rb").read()
             for f in os.listdir(base)}
    check("CLI CHI DOC", before == after)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
