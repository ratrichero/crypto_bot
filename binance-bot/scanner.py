"""Scanner tim coin dang THUC SU di ngang (G2).

Ham thuan (khong goi mang): nhan nen 1h + 15m da dong, tra ve ket qua danh
gia co diem va LY DO cu the de dashboard hien thi.

Tieu chi (nguong lay tu cfg["scanner"], chinh tren dashboard):
- ADX(14) 1h va 15m thap: khong co trend.
- Do rong Bollinger(20,2) 1h trong [bbw_min, bbw_max]: bien du rong de an
  TP, khong qua rong; percentile BBW so voi ~20 ngay <= bbw_pctile_max (loai
  luc bien dang no manh - dau hieu breakout).
- Bien gia range_hours (high/low) trong [range_min, range_max].
- So lan gia cat duong giua bien >= min_mid_crosses: dao dong qua lai that.
- Choppiness Index tren cua so range_hours >= chop_min (dao dong hoi ve
  giua, loai ca random walk) va Efficiency Ratio <= er_max (loai trend).
Vi tri gia trong bien (pos) khong loai coin, chi cong/tru diem va hien thi.
"""
from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Sequence

from indicators import adx

DEFAULTS = {
    "adx_1h_max": 20.0, "adx_15m_max": 22.0,
    "bbw_min_pct": 0.025, "bbw_max_pct": 0.10, "bbw_pctile_max": 80.0,
    "range_hours": 48, "range_min_pct": 0.025, "range_max_pct": 0.12,
    "min_mid_crosses": 4, "chop_min": 45.0, "er_max": 0.35,
}


def closed(candles: Sequence[dict]) -> List[dict]:
    """Bo nen dang hinh thanh (phan tu cuoi cua REST/WS kline)."""
    return list(candles[:-1]) if len(candles) > 1 else list(candles)


def bb_width_series(closes: Sequence[float], n: int = 20,
                    k: float = 2.0) -> List[float]:
    """(upper - lower) / middle cho moi cua so n."""
    out = []
    for i in range(n, len(closes) + 1):
        win = closes[i - n:i]
        mid = sum(win) / n
        if mid <= 0:
            continue
        var = sum((x - mid) ** 2 for x in win) / n
        out.append(2 * k * math.sqrt(var) / mid)
    return out


def percentile_rank(series: Sequence[float], value: float) -> Optional[float]:
    if not series:
        return None
    below = sum(1 for x in series if x <= value)
    return 100.0 * below / len(series)


def choppiness(candles: Sequence[dict], n: int = 14) -> Optional[float]:
    """CHOP = 100*log10(sum TR / (maxH - minL)) / log10(n). Cao = di ngang."""
    if len(candles) < n + 1:
        return None
    win = candles[-n:]
    trs = []
    for i in range(len(candles) - n, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]),
                       abs(c["l"] - p["c"])))
    hi = max(c["h"] for c in win)
    lo = min(c["l"] for c in win)
    if hi <= lo or sum(trs) <= 0:
        return None
    return 100.0 * math.log10(sum(trs) / (hi - lo)) / math.log10(n)


def efficiency_ratio(closes: Sequence[float], n: int) -> Optional[float]:
    """|thay doi rong| / tong |buoc|. ~0 = di ngang, ~1 = di thang."""
    if len(closes) < n + 1:
        return None
    win = closes[-(n + 1):]
    path = sum(abs(win[i] - win[i - 1]) for i in range(1, len(win)))
    if path <= 0:
        return 0.0
    return abs(win[-1] - win[0]) / path


def range_stats(candles: Sequence[dict], bars: int) -> Optional[dict]:
    win = list(candles[-bars:])
    if len(win) < max(6, bars // 2):
        return None
    hi = max(c["h"] for c in win)
    lo = min(c["l"] for c in win)
    if hi <= lo or lo <= 0:
        return None
    mid = (hi + lo) / 2
    crosses, prev = 0, 0
    for c in win:
        sign = 1 if c["c"] > mid else (-1 if c["c"] < mid else 0)
        if sign and prev and sign != prev:
            crosses += 1
        if sign:
            prev = sign
    last = win[-1]["c"]
    return {"high": hi, "low": lo, "mid": mid,
            "width_pct": (hi - lo) / mid, "crosses": crosses,
            "pos": (last - lo) / (hi - lo)}


def _r(v, nd=4):
    return None if v is None else round(float(v), nd)


def evaluate(symbol: str, candles_1h: Sequence[dict],
             candles_15m: Sequence[dict], cfg: Optional[dict] = None,
             now: Optional[float] = None) -> dict:
    """Danh gia 1 symbol. Tra ve {symbol, ts, passed, score, metrics,
    reasons}. Thieu du lieu -> passed=False voi ly do."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    now = time.time() if now is None else now
    h1 = closed(candles_1h)
    m15 = closed(candles_15m)
    reasons: List[str] = []
    if len(h1) < 60 or len(m15) < 30:
        return {"symbol": symbol, "ts": now, "passed": False, "score": 0.0,
                "metrics": {"bars_1h": len(h1), "bars_15m": len(m15)},
                "reasons": ["thiếu dữ liệu nến"]}
    closes = [x["c"] for x in h1]
    adx1 = adx(h1)
    adx15 = adx(m15)
    bbw_hist = bb_width_series(closes)
    bbw = bbw_hist[-1] if bbw_hist else None
    pctile = percentile_rank(bbw_hist, bbw) if bbw is not None else None
    rng = range_stats(h1, int(c["range_hours"]))
    # CHOP tren ca cua so bien (48h): CHOP(14) 1h qua nhieu, khong tach
    # duoc di ngang hoi quy ve giua bien voi random walk.
    chop = choppiness(h1, min(int(c["range_hours"]), len(h1) - 1))
    er = efficiency_ratio(closes, min(int(c["range_hours"]), len(closes) - 1))
    m = {"adx_1h": _r(adx1, 1), "adx_15m": _r(adx15, 1),
         "bbw_pct": _r(bbw), "bbw_pctile": _r(pctile, 1),
         "range_pct": _r(rng and rng["width_pct"]),
         "range_high": _r(rng and rng["high"], 8),
         "range_low": _r(rng and rng["low"], 8),
         "mid_crosses": rng and rng["crosses"],
         "pos": _r(rng and rng["pos"], 3),
         "chop": _r(chop, 1), "er": _r(er, 3), "last": _r(closes[-1], 8)}

    def need(ok, msg):
        if not ok:
            reasons.append(msg)

    need(adx1 is not None and adx1 <= c["adx_1h_max"],
         "ADX 1h %s > %s" % (m["adx_1h"], c["adx_1h_max"]))
    need(adx15 is not None and adx15 <= c["adx_15m_max"],
         "ADX 15m %s > %s" % (m["adx_15m"], c["adx_15m_max"]))
    need(bbw is not None and bbw >= c["bbw_min_pct"],
         "BB hẹp %.2f%% < %.2f%%" % ((bbw or 0) * 100, c["bbw_min_pct"] * 100))
    need(bbw is not None and bbw <= c["bbw_max_pct"],
         "BB rộng %.2f%% > %.2f%%" % ((bbw or 0) * 100,
                                      c["bbw_max_pct"] * 100))
    need(pctile is not None and pctile <= c["bbw_pctile_max"],
         "BB đang nở (pctile %s > %s)" % (m["bbw_pctile"],
                                          c["bbw_pctile_max"]))
    need(rng is not None and rng["width_pct"] >= c["range_min_pct"],
         "biên hẹp %.2f%% < %.2f%%" % (((rng or {}).get("width_pct") or 0)
                                       * 100, c["range_min_pct"] * 100))
    need(rng is not None and rng["width_pct"] <= c["range_max_pct"],
         "biên rộng %.2f%% > %.2f%%" % (((rng or {}).get("width_pct") or 0)
                                        * 100, c["range_max_pct"] * 100))
    need(rng is not None and rng["crosses"] >= c["min_mid_crosses"],
         "ít dao động: cắt giữa %s < %s lần"
         % ((rng or {}).get("crosses"), c["min_mid_crosses"]))
    need(chop is not None and chop >= c["chop_min"],
         "CHOP %s < %s" % (m["chop"], c["chop_min"]))
    need(er is not None and er <= c["er_max"],
         "ER %s > %s (đi theo hướng)" % (m["er"], c["er_max"]))

    # Diem 0-100: cang di ngang ro cang cao (dung xep hang, ke ca khi truot).
    parts = []
    if adx1 is not None:
        parts.append(max(0.0, 1 - adx1 / 40.0))
    if adx15 is not None:
        parts.append(max(0.0, 1 - adx15 / 40.0))
    if chop is not None:
        parts.append(min(1.0, max(0.0, (chop - 30) / 40.0)))
    if er is not None:
        parts.append(max(0.0, 1 - er))
    if rng is not None:
        parts.append(min(1.0, rng["crosses"] / max(1.0,
                                                   2 * c["min_mid_crosses"])))
        # gia o giua bien -> vao lenh tot hon o sat mep
        parts.append(1 - min(1.0, abs(rng["pos"] - 0.5) * 2))
    if pctile is not None:
        parts.append(1 - pctile / 100.0)
    score = round(100.0 * sum(parts) / len(parts), 1) if parts else 0.0
    return {"symbol": symbol, "ts": now, "passed": not reasons,
            "score": score, "metrics": m, "reasons": reasons}


def rank(results: Sequence[dict]) -> List[dict]:
    """Dat chuan truoc, roi diem giam dan."""
    return sorted(results, key=lambda r: (not r["passed"], -r["score"]))


def allowed_symbols(results: Dict[str, dict], top_k: int, now: float,
                    max_age_seconds: float) -> List[str]:
    """Symbol dat chuan, con moi, top K (che do filter)."""
    fresh = [r for r in results.values()
             if r.get("passed") and now - float(r.get("ts", 0))
             <= max_age_seconds]
    return [r["symbol"] for r in rank(fresh)[:max(0, int(top_k))]]


class ScannerRunner:
    """Chay scanner trong slow loop: moi luot quet toi da `max_per_tick`
    symbol den han (rescan_minutes), luu ket qua vao RAM + file + DB.

    fetch(symbol, interval, limit) -> nen (binance_client.get_klines).
    fatal: cac exception phai nem lai (BinanceSafetyStop), khong nuot.
    """

    def __init__(self, cfg: dict, fetch, log=print, db=None,
                 path: Optional[str] = None, bot: str = "binance",
                 clock=time.time, fatal: tuple = ()):
        self.cfg = cfg
        self.fetch = fetch
        self.log = log
        self.db = db                      # runtime_config.DBLink hoac None
        self.path = path
        self.bot = bot
        self.clock = clock
        self.fatal = tuple(fatal)
        self.results: Dict[str, dict] = {}
        self._retry: Dict[str, float] = {}
        self._last_prune = 0.0
        self._load()

    # ---------------------------------------------------------- config
    def scfg(self) -> dict:
        out = dict(DEFAULTS)
        out.update({"enabled": True, "mode": "observe", "top_k": 5,
                    "rescan_minutes": 15})
        out.update(self.cfg.get("scanner") or {})
        return out

    def period(self) -> float:
        return max(60.0, float(self.scfg()["rescan_minutes"]) * 60.0)

    def max_age(self) -> float:
        """Ket qua cu hon nguong nay coi la het han (che do filter chan)."""
        return max(1800.0, 3 * self.period())

    # ------------------------------------------------------------ io
    def _load(self):
        if not self.path:
            return
        try:
            import json
            with open(self.path) as f:
                data = json.load(f)
            self.results = {r["symbol"]: r for r in data.get("results", [])
                            if isinstance(r, dict) and r.get("symbol")}
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log("SCANNER warning: file ket qua hong (%s)" % e)

    def _save_file(self):
        if not self.path:
            return
        try:
            import json
            import os
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"ts": self.clock(),
                           "results": rank(list(self.results.values()))}, f)
            os.replace(tmp, self.path)
        except Exception as e:
            self.log("SCANNER warning: khong ghi duoc file: %s" % e)

    def _save_db(self, result: dict):
        conn = self.db.get() if self.db is not None else None
        if conn is None:
            return
        try:
            import bot_config
            bot_config.insert_scan(conn, result, self.bot)
            now = self.clock()
            if now - self._last_prune > 86400:
                bot_config.prune_scans(conn, 14, self.bot)
                self._last_prune = now
        except Exception as e:
            self.log("SCANNER warning: ghi DB loi: %s" % str(e)[:200])
            self.db.reset()

    # ------------------------------------------------------------ run
    def tick(self, symbols: Sequence[str], candles: Dict[str, dict],
             max_per_tick: int = 1) -> List[dict]:
        sc = self.scfg()
        if not sc.get("enabled", True):
            return []
        now = self.clock()
        universe = set(symbols)
        for sym in [s for s in self.results if s not in universe]:
            del self.results[sym]        # roi universe -> bo ket qua cu
        due = [s for s in symbols
               if now - float(self.results.get(s, {}).get("ts", 0))
               >= self.period() and now >= self._retry.get(s, 0)
               and (candles.get(s) or {}).get("15m")]
        due.sort(key=lambda s: float(self.results.get(s, {}).get("ts", 0)))
        done = []
        for sym in due[:max(1, int(max_per_tick))]:
            try:
                c1h = self.fetch(sym, "1h", 499)
            except self.fatal:
                raise
            except Exception as e:
                self._retry[sym] = now + 120
                self.log("SCANNER %s lay nen 1h loi: %s" % (sym, e))
                continue
            res = evaluate(sym, c1h, candles[sym]["15m"], sc, now)
            prev = self.results.get(sym)
            self.results[sym] = res
            if prev is None or prev.get("passed") != res["passed"]:
                self.log("SCANNER %s %s score=%s %s" % (
                    sym, "DAT" if res["passed"] else "truot", res["score"],
                    "; ".join(res["reasons"][:3])))
            self._save_db(res)
            done.append(res)
        if done:
            self._save_file()
        return done

    def allows(self, symbol: str, universe: Optional[Sequence[str]] = None
               ) -> bool:
        """Grid co duoc mo lot moi tren symbol? Chi chan o che do filter;
        thieu/het han du lieu -> chan (fail-closed)."""
        sc = self.scfg()
        if not sc.get("enabled", True) or sc.get("mode") != "filter":
            return True
        res = self.results
        if universe is not None:
            uni = set(universe)
            res = {k: v for k, v in res.items() if k in uni}
        return symbol in allowed_symbols(res, int(sc["top_k"]), self.clock(),
                                         self.max_age())
