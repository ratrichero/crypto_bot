"""Loc chieu xu huong cho grid (task 34).

Grid ve ban chat MUA khi gia giam / BAN khi gia tang (nguoc xu huong). Khi ca
thi truong troi xuong, moi symbol deu cham tang long -> bot om nhieu long
tuong quan cao cung luc. Module nay chan PHIA nguoc xu huong:

- bias "down" -> khong mo lot grid LONG moi (van cho short);
- bias "up"   -> khong mo lot grid SHORT moi (van cho long);
- "neutral"   -> ca 2 phia.

Hai lop (bat/tat rieng tren dashboard):
- A. market_filter: xu huong chung theo `market_symbol` (mac dinh BTCUSDT)
  -> ap cho MOI symbol (altcoin chay theo BTC).
- C. symbol_filter: xu huong rieng cua tung symbol.

Quy tac tren nen 1h DA DONG (+ gia hien tai):
- EMA(ema_period) va do doc CHUAN HOA THEO ATR:
  slope_atr = (EMA_now - EMA_{slope_bars nen truoc}) / ATR(14) 1h
  (coin bien dong manh / it bien dong dung chung 1 nguong).
- down: gia < EMA va slope_atr <= -slope_min_atr; up: gia > EMA va
  slope_atr >= slope_min_atr. Di ngang -> EMA phang -> neutral (grid chay
  binh thuong, ke ca khi gia vua giam 1-2 step trong bien). Mo phong: 0.5
  ATR -> di ngang chi ~10-18% thoi gian bi chan 1 phia, troi giam
  0.1-0.2%/h bi chan long 57-79% thoi gian (+ luat BTC giam nhanh).
- Rieng market symbol: gia thay doi >= market_move_pct so voi
  market_move_hours gio truoc -> down/up ngay (EMA cham, bat cu dump nhanh).
  KHONG ap cho tung symbol vi grid can gia giam 1-2 step moi vao lenh.

Thieu / het han du lieu (fetch loi) -> chan mo moi (fail-closed), giong
scanner che do filter. Chi chan MO MOI; lot dang mo van chay toi TP/SL/basket.
Ham `bias()` thuan (khong goi mang) de test va backtest dung chung.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence

from indicators import atr, ema

DEFAULTS = {
    "market_filter": True,
    "symbol_filter": True,
    "market_symbol": "BTCUSDT",
    "ema_period": 50,
    "slope_bars": 6,
    "slope_min_atr": 0.5,
    "market_move_hours": 4,
    "market_move_pct": 0.015,
    "refresh_minutes": 5,
}

KLINE_LIMIT = 99        # < 100 -> weight 1 (Binance /fapi/v1/klines)
H1_MS = 3600 * 1000


def tcfg(cfg: Optional[dict]) -> dict:
    """Muc `trend` cua CFG tron voi DEFAULTS."""
    out = dict(DEFAULTS)
    out.update((cfg or {}).get("trend") or {})
    return out


def closed_bars(candles: Sequence[dict], now_ms: Optional[float] = None
                ) -> List[dict]:
    """Bo nen 1h dang hinh thanh (REST tra kem nen hien tai o cuoi)."""
    rows = list(candles or [])
    if not rows:
        return rows
    if now_ms is None:
        return rows[:-1]
    if float(rows[-1].get("ts", 0)) + H1_MS > float(now_ms):
        return rows[:-1]
    return rows


def bias(candles_1h: Sequence[dict], price: Optional[float], cfg: dict,
         market: bool = False, now_ms: Optional[float] = None
         ) -> Optional[dict]:
    """-> {bias, reason, ema, slope_pct, move_pct, price} hoac None (thieu
    du lieu). cfg = muc trend (da tron DEFAULTS)."""
    bars = closed_bars(candles_1h, now_ms)
    closes = [float(b["c"]) for b in bars]
    n = int(cfg["ema_period"])
    k = max(1, int(cfg["slope_bars"]))
    if len(closes) < n + k:
        return None
    e_now = ema(closes, n)
    e_prev = ema(closes[:-k], n)
    a = atr(bars, 14)
    if not e_now or not e_prev or not a:
        return None
    px = float(price) if price else closes[-1]
    slope = (e_now - e_prev) / a
    smin = float(cfg["slope_min_atr"])
    move = None
    out = {"bias": "neutral", "reason": "", "ema": round(e_now, 8),
           "slope_atr": round(slope, 3),
           "slope_pct": round((e_now / e_prev - 1) * 100, 3),
           "move_pct": None, "price": px}
    if market:
        h = max(1, int(cfg["market_move_hours"]))
        if len(closes) > h:
            ref = closes[-1 - h]
            move = px / ref - 1.0 if ref else None
            out["move_pct"] = None if move is None else round(move * 100, 3)
        mp = float(cfg["market_move_pct"])
        if move is not None and mp > 0:
            if move <= -mp:
                out.update(bias="down", reason="giảm %.2f%% trong ~%dh"
                           % (-move * 100, h))
                return out
            if move >= mp:
                out.update(bias="up", reason="tăng %.2f%% trong ~%dh"
                           % (move * 100, h))
                return out
    if px < e_now and slope <= -smin:
        out.update(bias="down", reason="dưới EMA%d 1h, EMA dốc xuống "
                   "%.2f ATR/%dh" % (n, -slope, k))
    elif px > e_now and slope >= smin:
        out.update(bias="up", reason="trên EMA%d 1h, EMA dốc lên "
                   "%.2f ATR/%dh" % (n, slope, k))
    return out


def blocked_side(b: Optional[str]) -> Optional[str]:
    """bias -> phia bi chan."""
    return {"down": "long", "up": "short"}.get(b or "")


class TrendFilter:
    """Lay nen 1h dinh ky (moi slow tick toi da `max_per_tick` lan goi, weight
    1/lan) + giu bias moi nhat. Dung chung dict CFG voi bot (sua tren
    dashboard ap dung ngay)."""

    def __init__(self, cfg: dict, fetch: Callable, log=print,
                 clock=time.time, fatal=()):
        self.cfg = cfg
        self.fetch = fetch
        self.log = log
        self.clock = clock
        self.fatal = fatal
        self.candles: Dict[str, dict] = {}   # sym -> {"ts", "rows"}
        self.status: Dict[str, dict] = {}    # sym -> bias dict + ts
        self._retry: Dict[str, float] = {}

    def tcfg(self) -> dict:
        return tcfg(self.cfg)

    def market_symbol(self) -> str:
        return str(self.tcfg().get("market_symbol") or "BTCUSDT").upper()

    def period(self) -> float:
        return max(60.0, float(self.tcfg()["refresh_minutes"]) * 60.0)

    def max_age(self) -> float:
        """Du lieu cu hon nguong nay -> coi nhu thieu (chan mo moi)."""
        return max(1800.0, 3 * self.period())

    def needed(self, symbols: Sequence[str]) -> List[str]:
        t = self.tcfg()
        out: List[str] = []
        if t["market_filter"]:
            out.append(self.market_symbol())
        if t["symbol_filter"]:
            out.extend(s for s in symbols if s not in out)
        return out

    def tick(self, symbols: Sequence[str], prices: Dict[str, float],
             max_per_tick: int = 2) -> bool:
        """Lay nen cho symbol den han (market truoc) + tinh lai bias voi gia
        moi. Tra ve True neu co bias doi."""
        now = self.clock()
        need = self.needed(symbols)
        mkt = self.market_symbol()
        due = [s for s in need
               if now - float(self.candles.get(s, {}).get("ts", 0))
               >= self.period() and now >= self._retry.get(s, 0)]
        due.sort(key=lambda s: (s != mkt,
                                float(self.candles.get(s, {}).get("ts", 0))))
        for sym in due[:max(1, int(max_per_tick))]:
            try:
                rows = self.fetch(sym, "1h", KLINE_LIMIT)
            except self.fatal:
                raise
            except Exception as e:
                self._retry[sym] = now + 60
                self.log("TREND %s lay nen 1h loi: %s" % (sym, e))
                continue
            self.candles[sym] = {"ts": now, "rows": rows}
        for sym in [s for s in self.candles if s not in need]:
            self.candles.pop(sym, None)       # roi universe / tat filter
            self.status.pop(sym, None)
        return self.refresh(prices)

    def refresh(self, prices: Dict[str, float]) -> bool:
        t = self.tcfg()
        mkt = self.market_symbol()
        changed = False
        now = self.clock()
        for sym, rec in self.candles.items():
            px = prices.get(sym)
            if not px and rec["rows"]:
                # market symbol co the khong nam trong universe (khong co gia
                # WS): dung gia dong nen dang chay luc lay (<= refresh phut).
                px = rec["rows"][-1].get("c")
            res = bias(rec["rows"], px, t, market=(sym == mkt),
                       now_ms=now * 1000)
            prev = self.status.get(sym)
            if res is None:
                if prev is not None:
                    self.status.pop(sym, None)
                continue
            res["ts"] = rec["ts"]
            self.status[sym] = res
            old = prev.get("bias") if prev else None
            if old != res["bias"]:
                changed = True
                if old is not None or res["bias"] != "neutral":
                    self.log("TREND %s%s: %s -> %s %s" % (
                        sym, " (thi truong)" if sym == mkt else "", old,
                        res["bias"], res["reason"]))
        return changed

    def fresh(self, symbol: str) -> Optional[dict]:
        rec = self.status.get(symbol)
        if not rec or self.clock() - float(rec.get("ts", 0)) > self.max_age():
            return None
        return rec

    def blocks(self, symbol: str, side: str) -> Optional[str]:
        """Ly do chan mo lot grid `side` tren `symbol` (None = duoc phep)."""
        t = self.tcfg()
        layers = []
        if t["market_filter"]:
            layers.append((self.market_symbol(), "BTC" if self.market_symbol()
                           == "BTCUSDT" else self.market_symbol()))
        if t["symbol_filter"]:
            layers.append((symbol, symbol))
        for sym, name in layers:
            rec = self.fresh(sym)
            if rec is None:
                return "thiếu dữ liệu xu hướng %s" % name
            if blocked_side(rec["bias"]) == side:
                return "%s xu hướng %s (%s)" % (
                    name, "giảm" if rec["bias"] == "down" else "tăng",
                    rec["reason"])
        return None

    def snapshot(self) -> dict:
        """Cho state.json -> dashboard."""
        return {"market": self.market_symbol(), "ts": self.clock(),
                "symbols": {s: {k: v for k, v in r.items()}
                            for s, r in self.status.items()}}
