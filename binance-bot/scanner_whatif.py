"""Scanner: NEU noi nguong thi bao nhieu coin dat? (CHI DOC, khong goi mang)

Cham lai ket qua scan moi nhat (scanner_latest.json - da luu du chi so cua
tung coin) voi bo nguong khac, de quyet dinh noi tieu chi nao dua tren so
lieu thay vi doan.

    .venv/bin/python binance-bot/scanner_whatif.py
    .venv/bin/python binance-bot/scanner_whatif.py --set adx_1h_max=25 \\
        --set min_mid_crosses=3

In ra:
- Tieu chi nao loai nhieu coin nhat, va bao nhieu coin CHI truot dung 1
  tieu chi do (noi rieng tieu chi do se co them chung ay coin).
- So coin dat voi nguong hien tai / cac muc noi goi y / --set.
- Cot "ranging 15m": coin con phai qua regime 15m cua grid classic
  (adx_threshold, lay tu state.json) - giao 2 lop moi la so coin grid duoc.
Gioi han: range_hours khong cham lai duoc (bien 48h da tinh san); chi so
luu da lam tron (ADX/CHOP 1 so le) -> coin SAT nguong co the lech 1 tieu chi.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scanner  # noqa: E402

# Cac muc noi goi y (cong don). Moi muc chi sua vai nguong.
PRESETS = [
    ("noi nhe", {"adx_1h_max": 23.0, "adx_15m_max": 24.0}),
    ("noi vua", {"adx_1h_max": 25.0, "adx_15m_max": 25.0,
                 "min_mid_crosses": 3, "er_max": 0.40}),
    ("noi manh", {"adx_1h_max": 28.0, "adx_15m_max": 28.0,
                  "min_mid_crosses": 3, "er_max": 0.45, "chop_min": 40.0,
                  "bbw_pctile_max": 90.0}),
]

# ten ngan cua tung tieu chi (theo tien to cua ly do trong scanner.judge)
CRITERIA = [("ADX 1h", "adx_1h_max"), ("ADX 15m", "adx_15m_max"),
            ("BB hẹp", "bbw_min_pct"), ("BB rộng", "bbw_max_pct"),
            ("BB đang nở", "bbw_pctile_max"), ("biên hẹp", "range_min_pct"),
            ("biên rộng", "range_max_pct"), ("ít dao động", "min_mid_crosses"),
            ("CHOP", "chop_min"), ("ER", "er_max")]


def raw_from_metrics(m: dict) -> Optional[dict]:
    """metrics hien thi (scanner.judge) -> raw cho judge() lai."""
    if not m or m.get("adx_1h") is None and m.get("range_pct") is None:
        return None
    rng = None
    if m.get("range_pct") is not None:
        rng = {"width_pct": m["range_pct"], "crosses": m.get("mid_crosses")
               or 0, "pos": m.get("pos") if m.get("pos") is not None else .5,
               "high": m.get("range_high"), "low": m.get("range_low")}
    return {"adx_1h": m.get("adx_1h"), "adx_15m": m.get("adx_15m"),
            "bbw_pct": m.get("bbw_pct"), "bbw_pctile": m.get("bbw_pctile"),
            "range": rng, "chop": m.get("chop"), "er": m.get("er"),
            "last": m.get("last"), "atr15_pct": m.get("atr15_pct")}


def criterion(reason: str) -> str:
    for name, _key in CRITERIA:
        if reason.startswith(name):
            return name
    return reason.split(" ")[0]


def rejudge(results: List[dict], cfg: dict) -> Dict[str, dict]:
    out = {}
    for r in results:
        raw = raw_from_metrics(r.get("metrics") or {})
        passed, score, _m, reasons = scanner.judge(raw, cfg)
        out[r["symbol"]] = {"passed": passed, "score": score,
                            "fails": [criterion(x) for x in reasons]}
    return out


def ranging_15m(st: dict, cfg_root: dict, symbol: str, adx15) -> bool:
    """Regime 15m cua grid classic (state.json neu co, khong thi uoc
    luong bang ADX 15m < adx_threshold)."""
    rec = (st.get("regimes") or {}).get(symbol)
    if rec and rec.get("regime"):
        return rec["regime"] == "ranging"
    thr = float(cfg_root.get("adx_threshold") or 25)
    return adx15 is not None and adx15 < thr


def analyse(results: List[dict], scfg: dict, st: dict, cfg_root: dict,
            extra: Optional[dict] = None) -> dict:
    base = rejudge(results, scfg)
    blockers: Dict[str, dict] = {}
    for sym, j in base.items():
        for f in j["fails"]:
            b = blockers.setdefault(f, {"fail": 0, "only": []})
            b["fail"] += 1
            if len(j["fails"]) == 1:
                b["only"].append(sym)
    adx15 = {r["symbol"]: (r.get("metrics") or {}).get("adx_15m")
             for r in results}
    variants = [("hien tai", {})]
    acc: dict = {}
    for name, ov in PRESETS:
        acc = dict(acc, **ov)
        variants.append((name, dict(acc)))
    if extra:
        variants.append(("--set", dict(extra)))
    rows = []
    for name, ov in variants:
        j = rejudge(results, dict(scfg, **ov))
        ok = sorted((s for s in j if j[s]["passed"]),
                    key=lambda s: -j[s]["score"])
        grid_ok = [s for s in ok if ranging_15m(st, cfg_root, s, adx15[s])]
        rows.append({"name": name, "changes": ov, "passed": ok,
                     "grid_ok": grid_ok})
    return {"n": len(results), "blockers": blockers, "variants": rows}


def _parse_set(items) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        k = k.strip()
        if k not in scanner.DEFAULTS:
            raise SystemExit("khong co nguong scanner '%s' (co: %s)" % (
                k, ", ".join(scanner.DEFAULTS)))
        out[k] = float(v)
    return out


def run_cli(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=HERE)
    ap.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="nguong thu them, vd adx_1h_max=25")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    base = os.path.abspath(a.base)
    import grid_diag
    cfg, version = grid_diag.effective_config(base)
    data = grid_diag._load_json(os.path.join(base, "scanner_latest.json"))
    if not data or not data.get("results"):
        raise SystemExit("chua co scanner_latest.json (bot chua quet?)")
    st = grid_diag._load_json(os.path.join(base, "state.json"), {}) or {}
    runner = scanner.ScannerRunner(cfg, fetch=None, log=lambda *_: None)
    scfg = runner.scfg()
    res = analyse(data["results"], scfg, st, cfg, _parse_set(a.set))
    if a.json:
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    age = (time.time() - float(data.get("ts") or 0)) / 60
    out("== SCANNER WHAT-IF (chi doc) · %d coin · scan %d phut truoc · "
        "config version %s · mode %s top_k %s ==" % (
            res["n"], age, version, scfg.get("mode"), scfg.get("top_k")))
    out("Nguong hien tai: " + ", ".join(
        "%s=%s" % (k, scfg[k]) for _n, k in CRITERIA))
    out("")
    out("%-14s %10s %12s  %s" % ("TIEU CHI", "LOAI", "CHI TRUOT", "(coin chi"
                                 " truot tieu chi nay)"))
    for name, b in sorted(res["blockers"].items(),
                          key=lambda kv: -kv[1]["fail"]):
        out("%-14s %10d %12d  %s" % (name, b["fail"], len(b["only"]),
                                     ", ".join(b["only"][:8])))
    out("")
    out("%-10s %5s %12s  %s" % ("MUC", "DAT", "+ranging15m", "THAY DOI / "
                                "COIN DAT (diem cao truoc)"))
    for v in res["variants"]:
        ch = ", ".join("%s=%s" % kv for kv in v["changes"].items()) or "-"
        out("%-10s %5d %12d  %s" % (v["name"], len(v["passed"]),
                                    len(v["grid_ok"]), ch))
        if v["passed"]:
            out("%-10s %5s %12s  %s" % ("", "", "", ", ".join(v["passed"][:12])))
    out("")
    out("DAT = qua scanner; +ranging15m = con qua ca regime 15m (adx_threshold"
        " %s) = so coin grid classic thuc su mo duoc. top_k chi cat SAU khi "
        "dat." % cfg.get("adx_threshold"))
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
