#!/usr/bin/env python3
"""Backtest grid v2 (G3): kiem chung scanner + range grid 2 chieu (G4)
+ entry LIMIT maker (G5) tren du lieu 5m cong khai cua Binance.

Dung CHUNG logic voi bot live:
- scanner.compute_metrics / scanner.judge (cung nguong `scanner.*`);
- range_grid.build_range / lot_exits / check_break / plan_slots.

Lenh:
  download       tai nen 5m nhieu symbol vao --data-dir (SYMBOL-5m.jsonl)
  scan           tinh truoc chi bao scanner -> --scan-cache (dung lai)
  study          scanner co tach duoc symbol di ngang khong? Moi `every`
                 gio, moi symbol: chay grid CO LAP `horizon` gio tu thoi
                 diem do, so PnL nhom DAT vs TRUOT va theo bucket diem.
  run            mo phong portfolio day du (top-K, slot, basket, tong,
                 daily) voi 1 config.
  walk-forward   chon tp_pct x step_mult tren train, do tren test.

Gia dinh (ghi ro trong report):
- LIMIT entry = maker (fee_maker, 0.02%), khop khi gia XUYEN qua gia lenh
  `fill_through` (mac dinh 0.02%) - cham dung gia khong tinh khop.
- Market entry / TP / SL / basket / dong cuong buc = taker (fee_rate)
  + slippage moi phia. TP that cua bot la TAKE_PROFIT_MARKET.
- Duong di trong nen 5m: nen tang O->L->H->C, nen giam O->H->L->C;
  cung gia thi SL truoc. Basket stop dong dung tai gia cham nguong.
- Tran tong / daily stop: danh gia tai cac diem su kien cua tung symbol
  voi gia gan nhat cua symbol khac (xap xi).
- Scanner: moi `rescan_minutes` (mac dinh 60, live 15), chi dung nen 1h
  / 15m DA DONG truoc thoi diem quet; can du `min_h1` nen 1h (mac dinh
  498 = nhu live) -> ~21 ngay dau chi de lam nong chi bao.
- Khong tinh funding. Khong goi API xac thuc.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import backtest as bt1  # noqa: E402
import range_grid  # noqa: E402
import scanner  # noqa: E402

BAR_MS = 5 * 60 * 1000
M15_MS = 15 * 60 * 1000
H1_MS = 60 * 60 * 1000
DAY_MS = 24 * H1_MS

DEFAULT_SPACE = {"grid.tp_pct": (0.008, 0.010, 0.012),
                 "grid.step_mult": (0.8, 1.2, 1.6)}


# ----------------------------------------------------------------- data
class Agg:
    """Gop nen 5m -> nen lon hon. completed_count[i] = so nen lon DA DONG
    truoc khi nen 5m thu i bat dau (chi chua bar < i -> khong lookahead)."""

    def __init__(self, bars: Sequence[dict], group_ms: int):
        self.completed: List[dict] = []
        self.completed_count: List[int] = []
        rows: List[dict] = []
        key = None
        for bar in bars:
            k = (bar["ts"] // group_ms) * group_ms
            if rows and k != key:
                self.completed.append(_agg(rows, key))
                rows = []
            key = k
            self.completed_count.append(len(self.completed))
            rows.append(bar)

    def closed_before(self, index: int, limit: int) -> List[dict]:
        c = self.completed_count[index]
        return self.completed[max(0, c - limit):c]


def _agg(rows: Sequence[dict], key: int) -> dict:
    return {"ts": key, "o": rows[0]["o"], "h": max(r["h"] for r in rows),
            "l": min(r["l"] for r in rows), "c": rows[-1]["c"],
            "v": sum(r.get("v", 0) for r in rows)}


def data_path(data_dir: str, symbol: str) -> str:
    return os.path.join(data_dir, "%s-5m.jsonl" % symbol.upper())


def list_symbols(data_dir: str) -> List[str]:
    return sorted(f[:-len("-5m.jsonl")] for f in os.listdir(data_dir)
                  if f.endswith("-5m.jsonl"))


def load_data(data_dir: str, symbols: Optional[Sequence[str]] = None,
              start_ms: Optional[int] = None,
              end_ms: Optional[int] = None) -> Dict[str, List[dict]]:
    out = {}
    for sym in symbols or list_symbols(data_dir):
        bars = bt1.load_bars(data_path(data_dir, sym), start_ms, end_ms)
        if bars:
            out[sym] = bars
    return out


# ---------------------------------------------------------------- scans
def compute_scans(bars: Sequence[dict], rescan_minutes: int = 60,
                  range_hours: int = 48, min_h1: int = 498
                  ) -> Dict[int, dict]:
    """{ts: raw metrics} tai cac moc quet. Chi dung nen DA DONG truoc ts:
    cung so nen nhu live (1h: 499 REST -> 498 dong; 15m: 100 -> 99)."""
    period = max(5, int(rescan_minutes)) * 60 * 1000
    h1 = Agg(bars, H1_MS)
    m15 = Agg(bars, M15_MS)
    out: Dict[int, dict] = {}
    for i, bar in enumerate(bars):
        if bar["ts"] % period:
            continue
        c1 = h1.closed_before(i, 498)
        if len(c1) < max(60, int(min_h1)):
            continue
        c15 = m15.closed_before(i, 99)
        # them 1 nen gia lam "dang hinh thanh" vi compute_metrics bo nen cuoi
        raw = scanner.compute_metrics(c1 + [c1[-1]], c15 + [c15[-1]],
                                      range_hours) if c15 else None
        if raw is not None:
            out[bar["ts"]] = raw
    return out


def scan_all(data: Dict[str, List[dict]], rescan_minutes: int,
             range_hours: int, min_h1: int, cache: Optional[str] = None,
             log=print) -> Dict[str, Dict[int, dict]]:
    meta = {"rescan_minutes": int(rescan_minutes),
            "range_hours": int(range_hours), "min_h1": int(min_h1)}
    stored: Dict[str, dict] = {}
    if cache and os.path.exists(cache):
        try:
            with open(cache) as f:
                payload = json.load(f)
            if payload.get("meta") == meta:
                stored = payload.get("symbols", {})
        except (OSError, ValueError):
            stored = {}
    out: Dict[str, Dict[int, dict]] = {}
    dirty = False
    for sym, bars in data.items():
        ent = stored.get(sym)
        sig = [bars[0]["ts"], bars[-1]["ts"], len(bars)]
        if ent and ent.get("sig") == sig:
            out[sym] = {int(k): v for k, v in ent["scans"].items()}
            continue
        t0 = time.time()
        out[sym] = compute_scans(bars, rescan_minutes, range_hours, min_h1)
        log("scan %s: %d moc (%.1fs)" % (sym, len(out[sym]),
                                         time.time() - t0))
        stored[sym] = {"sig": sig,
                       "scans": {str(k): v for k, v in out[sym].items()}}
        dirty = True
    if cache and dirty:
        tmp = cache + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"meta": meta, "symbols": stored}, f)
        os.replace(tmp, cache)
    return out


# ------------------------------------------------------------ simulator
def _day(ts: int) -> int:
    return ts // DAY_MS


class PortfolioSim:
    """Mo phong range grid nhieu symbol.

    fixed: {symbol: range} -> bo qua scanner (study): range co dinh,
    khong dung lai sau khi vo; scanner chi dung de lay ADX 1h cho break.
    """

    def __init__(self, data: Dict[str, List[dict]],
                 scans: Dict[str, Dict[int, dict]], cfg: dict,
                 entry_mode: str = "limit", start_ms: Optional[int] = None,
                 end_ms: Optional[int] = None,
                 initial: Optional[float] = None,
                 fixed: Optional[Dict[str, dict]] = None,
                 fill_through: float = 0.0002,
                 rescan_minutes: int = 60):
        if entry_mode not in ("limit", "market"):
            raise ValueError("entry_mode phai la limit|market")
        self.data, self.scans, self.cfg = data, scans, cfg
        self.mode = entry_mode
        self.g = range_grid.gcfg(cfg.get("grid"))
        self.g["max_positions"] = min(int(self.g["max_positions"]),
                                      int(cfg.get("max_total_positions")
                                          or 10 ** 6))
        self.sc = dict(scanner.DEFAULTS)
        self.sc.update({"top_k": 5})
        self.sc.update(cfg.get("scanner") or {})
        self.risk = cfg.get("risk") or {}
        self.fee_taker = float(cfg.get("fee_rate", 0.0005))
        self.fee_maker = float(cfg.get("fee_maker", 0.0002))
        self.slip = float(cfg.get("slippage", 0.0001))
        self.notional = (float(cfg.get("order_margin_usdt", 100))
                         * float(cfg.get("leverage", 10)))
        self.max_notional_mult = float(self.risk.get("max_notional_mult",
                                                     10.0))
        self.fill_through = float(fill_through)
        self.max_age_ms = max(1800, 3 * max(60, int(rescan_minutes) * 60)) \
            * 1000
        self.fixed = fixed
        self.start_ms, self.end_ms = start_ms, end_ms
        self.initial = float(initial if initial is not None
                             else cfg.get("start_equity", 1000.0))
        self.equity = self.initial
        self.positions: List[dict] = []
        self.trades: List[dict] = []
        self.fees = 0.0
        self.slippage_paid = 0.0
        self.curve: List[float] = []
        self.exposure: List[dict] = []
        self.counters = {"orders_placed": 0, "orders_filled": 0,
                         "orders_cancelled": 0, "market_entries": 0,
                         "basket_stops": 0, "total_stops": 0,
                         "daily_stops": 0, "breaks": 0, "derisk": 0,
                         "ranges_built": 0, "slot_denied": 0}
        self.sym: Dict[str, dict] = {
            s: {"rng": None, "broken": False, "risk_halted": False,
                "scan": None, "pending": {}, "last": None}
            for s in data}
        if fixed:
            for s, rng in fixed.items():
                if s in self.sym and rng:
                    self.sym[s]["rng"] = rng
                    self.counters["ranges_built"] += 1
        self.allowed: List[str] = list(fixed) if fixed else []
        self.halted_day: Optional[int] = None
        self.day: Optional[int] = None
        self.day_start_equity = self.initial
        self.base_equity = self.initial
        self._id = 0

    # --------------------------------------------------------- helpers
    def _lots(self, sym: Optional[str] = None) -> List[dict]:
        return [p for p in self.positions if sym is None or p["sym"] == sym]

    def _price(self, sym: str) -> Optional[float]:
        return self.sym[sym]["last"]

    def mark_equity(self) -> float:
        eq = self.equity
        for p in self.positions:
            px = self._price(p["sym"]) or p["entry"]
            eq += (px - p["entry"]) * p["qty"] * (1 if p["side"] == "long"
                                                  else -1)
        return eq

    def _pending_notional(self) -> float:
        return sum(o["notional"] for s in self.sym.values()
                   for o in s["pending"].values())

    def _exposure_ok(self, extra: float) -> bool:
        cur = sum(p["qty"] * (self._price(p["sym"]) or p["entry"])
                  for p in self.positions)
        return (cur + self._pending_notional() + extra
                <= self.max_notional_mult * max(self.mark_equity(), 0) + 1e-9)

    def _blocked(self, sym: str, ts: int) -> bool:
        s = self.sym[sym]
        return (s["risk_halted"] or s["broken"] or s["rng"] is None
                or self.halted_day == _day(ts) or sym not in self.allowed)

    # ------------------------------------------------------ open/close
    def _open(self, sym: str, side: str, key: str, price: float, ts: int,
              maker: bool) -> dict:
        s = self.sym[sym]
        if maker:
            entry = price
            fee = self.notional * self.fee_maker
        else:
            entry = price * (1 + self.slip if side == "long" else 1 - self.slip)
            fee = self.notional * self.fee_taker
            self.slippage_paid += abs(entry - price) * self.notional / entry
        qty = self.notional / entry
        sl, tp = range_grid.lot_exits(s["rng"], side, entry, self.g)
        self._id += 1
        pos = {"id": self._id, "sym": sym, "side": side, "key": key,
               "entry": entry, "qty": qty, "sl": sl, "tp": tp,
               "open_ts": ts, "entry_fee": fee, "maker": maker}
        self.equity -= fee
        self.fees += fee
        self.positions.append(pos)
        return pos

    def _close(self, pos: dict, price: float, reason: str, ts: int) -> None:
        sign = 1 if pos["side"] == "long" else -1
        exit_px = price * (1 - self.slip * sign)
        self.slippage_paid += abs(exit_px - price) * pos["qty"]
        gross = (exit_px - pos["entry"]) * pos["qty"] * sign
        fee = exit_px * pos["qty"] * self.fee_taker
        self.equity += gross - fee
        self.fees += fee
        self.positions.remove(pos)
        self.trades.append({
            "symbol": pos["sym"], "side": pos["side"], "key": pos["key"],
            "entry": round(pos["entry"], 10), "exit": round(exit_px, 10),
            "qty": pos["qty"], "pnl": gross - fee - pos["entry_fee"],
            "reason": reason, "maker_entry": pos["maker"],
            "open_ts": pos["open_ts"], "close_ts": ts})

    def _cancel_pending(self, sym: str) -> None:
        n = len(self.sym[sym]["pending"])
        if n:
            self.counters["orders_cancelled"] += n
            self.sym[sym]["pending"] = {}

    def _stop_symbol(self, sym: str, price: float, reason: str, ts: int):
        for p in list(self._lots(sym)):
            self._close(p, price, reason, ts)
        s = self.sym[sym]
        s["risk_halted"] = True
        self._cancel_pending(sym)

    # ------------------------------------------------------ management
    def _update_scans(self, ts: int) -> None:
        for sym, s in self.sym.items():
            raw = self.scans.get(sym, {}).get(ts)
            if raw is None:
                continue
            passed, score, metrics, _ = scanner.judge(raw, self.sc)
            s["scan"] = {"ts": ts, "passed": passed, "score": score,
                         "metrics": metrics}
        if self.fixed:
            return
        fresh = [(sym, s["scan"]) for sym, s in self.sym.items()
                 if s["scan"] and s["scan"]["passed"]
                 and ts - s["scan"]["ts"] <= self.max_age_ms]
        fresh.sort(key=lambda x: -x[1]["score"])
        self.allowed = [sym for sym, _ in fresh[:int(self.sc["top_k"])]]

    def _new_day(self, ts: int) -> None:
        d = _day(ts)
        if self.day == d:
            return
        self.day = d
        self.day_start_equity = self.mark_equity()
        for sym, s in self.sym.items():
            if not self._lots(sym):
                s["risk_halted"] = False

    def _manage_symbol(self, sym: str, price: float, ts: int) -> None:
        s = self.sym[sym]
        flat = not self._lots(sym) and not s["pending"]
        scan = s["scan"]
        if (not self.fixed and flat and not s["risk_halted"]
                and self.halted_day != _day(ts) and sym in self.allowed
                and scan and (s["rng"] is None or s["rng"]["ts"] < scan["ts"])):
            rng = range_grid.build_range(scan["metrics"], self.g, scan["ts"])
            if rng:
                s["rng"], s["broken"] = rng, False
                self.counters["ranges_built"] += 1
        if s["rng"] and not s["broken"]:
            fresh = scan if scan and ts - scan["ts"] <= self.max_age_ms \
                else None
            why = range_grid.check_break(s["rng"], price,
                                         fresh and fresh["metrics"], self.g)
            if why:
                s["broken"] = True
                self.counters["breaks"] += 1
                self._cancel_pending(sym)
        if s["broken"]:
            for p in range_grid.derisk_targets(self._lots(sym), price, self.g):
                self._close(p, price, "GRID_DERISK", ts)
                self.counters["derisk"] += 1
        if self._blocked(sym, ts):
            self._cancel_pending(sym)

    def _plan_limits(self, ts: int, opens: Dict[str, float]) -> None:
        rows = []
        for sym, price in opens.items():
            s = self.sym[sym]
            if self._blocked(sym, ts):
                continue
            taken = {p["key"] for p in self._lots(sym)}
            cands = range_grid.limit_candidates(
                s["rng"], price, taken,
                float(self.g.get("limit_min_gap_pct", 0.0005)))
            have = {lv["key"] for lv in cands}
            for key, o in s["pending"].items():
                if key not in have:
                    cands.append({"key": key, "side": o["side"],
                                  "price": o["price"],
                                  "dist": abs(o["price"] / price - 1)})
            rows.append({"symbol": sym, "score": (s["scan"] or {}).get(
                "score", 0), "lots": len(self._lots(sym)),
                "candidates": cands, "pending": set(s["pending"])})
        # symbol khong lap ke hoach (khong co gia / bi chan) van giu lot
        total = len(self.positions) + sum(
            len(self.sym[x]["pending"]) for x in self.sym
            if x not in {r["symbol"] for r in rows})
        chosen = set(range_grid.plan_slots(rows, self.g, total))
        for r in rows:
            sym = r["symbol"]
            s = self.sym[sym]
            for key in list(s["pending"]):
                if (sym, key) not in chosen:
                    del s["pending"][key]
                    self.counters["orders_cancelled"] += 1
            lv_by_key = {lv["key"]: lv for lv in s["rng"]["levels"]}
            for (csym, key) in sorted(chosen):
                if csym != sym or key in s["pending"]:
                    continue
                lv = lv_by_key.get(key)
                if lv is None or not self._exposure_ok(self.notional):
                    self.counters["slot_denied"] += 1
                    continue
                s["pending"][key] = {"side": lv["side"], "price": lv["price"],
                                     "notional": self.notional, "ts": ts}
                self.counters["orders_placed"] += 1

    def _market_slot_ok(self, sym: str) -> bool:
        lots = self._lots(sym)
        if len(self.positions) >= int(self.g["max_positions"]):
            return False
        if len(lots) >= int(self.g.get("max_lots_per_symbol") or 10 ** 6):
            return False
        busy = {p["sym"] for p in self.positions}
        lim = int(self.g.get("max_symbols") or 0)
        if lim and sym not in busy and len(busy) >= lim:
            return False
        return self._exposure_ok(self.notional)

    def _market_entries_at(self, sym: str, price: float, ts: int,
                           denied: set) -> None:
        if self._blocked(sym, ts):
            return
        s = self.sym[sym]
        taken = {p["key"] for p in self._lots(sym)} | denied
        for lv in range_grid.market_triggers(s["rng"], price, taken):
            if not self._market_slot_ok(sym):
                denied.add(lv["key"])
                self.counters["slot_denied"] += 1
                continue
            self._open(sym, lv["side"], lv["key"], price, ts, maker=False)
            self.counters["market_entries"] += 1

    # ------------------------------------------------------- intrabar
    def _basket_limit(self) -> float:
        return float(self.risk.get("grid_basket_max_loss_pct", 0.0)) \
            * self.base_equity

    def _next_event(self, sym: str, cursor: float, end: float, down: bool,
                    ts: int, denied: set):
        lo, hi = (end, cursor) if down else (cursor, end)
        cands = []

        def add(price, prio, kind, obj):
            if lo - 1e-12 <= price <= hi + 1e-12:
                cands.append((price, prio, kind, obj))

        lots = self._lots(sym)
        for p in lots:
            if p["side"] == "long":
                add(p["sl"], 0, "sl", p) if down else add(p["tp"], 2, "tp", p)
            else:
                add(p["tp"], 2, "tp", p) if down else add(p["sl"], 0, "sl", p)
        s = self.sym[sym]
        rng = s["rng"]
        if rng and not s["broken"]:
            bb = float(self.g["break_buffer"])
            if down:
                add(rng["low"] * (1 - bb), -2, "break", None)
            else:
                add(rng["high"] * (1 + bb), -2, "break", None)
        for key, o in s["pending"].items():
            if down and o["side"] == "long":
                add(o["price"] * (1 - self.fill_through), 1, "fill", key)
            elif not down and o["side"] == "short":
                add(o["price"] * (1 + self.fill_through), 1, "fill", key)
        if self.mode == "market" and not self._blocked(sym, ts):
            taken = {p["key"] for p in lots} | denied
            for lv in s["rng"]["levels"]:
                if lv["key"] in taken:
                    continue
                if down and lv["side"] == "long" and \
                        lv["price"] > s["rng"]["sl_long"]:
                    add(lv["price"], 1, "mkt", lv)
                elif not down and lv["side"] == "short" and \
                        lv["price"] < s["rng"]["sl_short"]:
                    add(lv["price"], 1, "mkt", lv)
        limit = self._basket_limit()
        if limit > 0 and lots:
            a = sum(p["qty"] * (1 if p["side"] == "long" else -1)
                    for p in lots)
            b = sum(p["qty"] * p["entry"] * (1 if p["side"] == "long" else -1)
                    for p in lots)
            if a * cursor - b <= -limit:
                cands.append((cursor, -1, "basket", None))
            elif a != 0 and ((down and a > 0) or (not down and a < 0)):
                add((b - limit) / a, -1, "basket", None)
        if not cands:
            return None
        cands.sort(key=lambda c: ((-c[0] if down else c[0]), c[1]))
        return cands[0]

    def _segment(self, sym: str, a: float, b: float, ts: int,
                 denied: set) -> None:
        if a == b:
            return
        down = b < a
        cursor = a
        for _ in range(10000):
            ev = self._next_event(sym, cursor, b, down, ts, denied)
            if ev is None:
                break
            price, _prio, kind, obj = ev
            cursor = price
            self.sym[sym]["last"] = price
            s = self.sym[sym]
            if kind == "sl":
                self._close(obj, price, "SL", ts)
            elif kind == "tp":
                self._close(obj, price, "TP", ts)
            elif kind == "fill":
                o = s["pending"].pop(obj)
                self._open(sym, o["side"], obj, o["price"], ts, maker=True)
                self.counters["orders_filled"] += 1
            elif kind == "mkt":
                if self._market_slot_ok(sym):
                    self._open(sym, obj["side"], obj["key"], price, ts,
                               maker=False)
                    self.counters["market_entries"] += 1
                else:
                    denied.add(obj["key"])
                    self.counters["slot_denied"] += 1
            elif kind == "break":
                s["broken"] = True
                self.counters["breaks"] += 1
                self._cancel_pending(sym)
                for p in range_grid.derisk_targets(self._lots(sym), price,
                                                   self.g):
                    self._close(p, price, "GRID_DERISK", ts)
                    self.counters["derisk"] += 1
            elif kind == "basket":
                self.counters["basket_stops"] += 1
                self._stop_symbol(sym, price, "GRID_BASKET_STOP", ts)
            self._portfolio_checks(ts)
        self.sym[sym]["last"] = b
        self._portfolio_checks(ts)

    def _portfolio_checks(self, ts: int) -> None:
        if not self.positions:
            return
        tot = float(self.risk.get("grid_total_max_loss_pct", 0.10))
        if tot > 0:
            pnl = self.mark_equity() - self.equity
            if pnl <= -tot * self.base_equity:
                self.counters["total_stops"] += 1
                for sym in sorted({p["sym"] for p in self.positions}):
                    self._stop_symbol(sym, self._price(sym), "GRID_TOTAL_STOP",
                                      ts)
        daily = float(self.risk.get("daily_max_loss_pct", 0.10))
        if (daily > 0 and self.day_start_equity > 0
                and self.halted_day != _day(ts)):
            dd = self.mark_equity() / self.day_start_equity - 1
            if dd <= -daily:
                self.counters["daily_stops"] += 1
                self.halted_day = _day(ts)
                for sym in sorted({p["sym"] for p in self.positions}):
                    for p in list(self._lots(sym)):
                        self._close(p, self._price(sym), "DAILY_STOP", ts)
                for sym in self.sym:
                    self._cancel_pending(sym)

    # ------------------------------------------------------------ run
    def run(self) -> dict:
        stamps = sorted({b["ts"] for bars in self.data.values() for b in bars
                         if (self.start_ms is None or b["ts"] >= self.start_ms)
                         and (self.end_ms is None or b["ts"] < self.end_ms)})
        by_ts = {s: {b["ts"]: b for b in bars} for s, bars in self.data.items()}
        last_ts = None
        for ts in stamps:
            last_ts = ts
            self._update_scans(ts)
            self._new_day(ts)
            bars = {s: by_ts[s][ts] for s in self.data if ts in by_ts[s]}
            for s, bar in bars.items():
                self.sym[s]["last"] = bar["o"]
            self.base_equity = self.mark_equity()
            for s, bar in bars.items():
                self._manage_symbol(s, bar["o"], ts)
            if self.mode == "limit":
                self._plan_limits(ts, {s: b["o"] for s, b in bars.items()})
            for s, bar in bars.items():
                denied: set = set()
                if self.mode == "market":
                    self._market_entries_at(s, bar["o"], ts, denied)
                pts = ([bar["o"], bar["l"], bar["h"], bar["c"]]
                       if bar["c"] >= bar["o"]
                       else [bar["o"], bar["h"], bar["l"], bar["c"]])
                for a, b in zip(pts, pts[1:]):
                    self._segment(s, a, b, ts, denied)
            eq = self.mark_equity()
            self.curve.append(eq)
            notional = sum(p["qty"] * (self._price(p["sym"]) or p["entry"])
                           for p in self.positions)
            self.exposure.append({"notional": notional,
                                  "pct": notional / eq * 100 if eq > 0 else 0})
        for p in list(self.positions):
            self._close(p, self._price(p["sym"]), "END", last_ts or 0)
        for s in self.sym:
            self._cancel_pending(s)
        return self.result()

    def result(self) -> dict:
        res = bt1.metrics(
            self.initial, self.equity, self.trades, self.curve, self.fees,
            0.0, False, "", slippage_paid=self.slippage_paid,
            exposure_curve=self.exposure,
            daily_stop_events=self.counters["daily_stops"],
            grid_basket_stop_events=self.counters["basket_stops"])
        res.pop("intrabar_policy", None)
        reasons: Dict[str, int] = {}
        by_sym: Dict[str, float] = {}
        for t in self.trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
            by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0.0) + t["pnl"]
        holds = [(t["close_ts"] - t["open_ts"]) / H1_MS for t in self.trades]
        res.update({
            "entry_mode": self.mode,
            "net_per_trade": round(res["net_pnl"] / len(self.trades), 6)
            if self.trades else 0.0,
            "exit_reasons": reasons,
            "pnl_by_symbol": {k: round(v, 4) for k, v in
                              sorted(by_sym.items(), key=lambda x: x[1])},
            "avg_hold_hours": round(sum(holds) / len(holds), 3)
            if holds else 0.0,
            "counters": dict(self.counters),
            "assumptions": {
                "fee_maker": self.fee_maker, "fee_taker": self.fee_taker,
                "slippage": self.slip, "fill_through": self.fill_through,
                "lot_notional": self.notional,
                "intrabar": "bullish O-L-H-C, bearish O-H-L-C, SL first",
                "funding": "not_modeled"},
        })
        return res


# ---------------------------------------------------------------- study
def _summary(vals: Sequence[dict]) -> dict:
    pnls = [v["pnl"] for v in vals]
    if not pnls:
        return {"n": 0}
    srt = sorted(pnls)
    return {"n": len(pnls), "mean_pnl": round(statistics.mean(pnls), 4),
            "median_pnl": round(statistics.median(pnls), 4),
            "win_rate_pct": round(sum(1 for p in pnls if p > 0)
                                  / len(pnls) * 100, 2),
            "p10_pnl": round(srt[int(0.1 * (len(srt) - 1))], 4),
            "p90_pnl": round(srt[int(0.9 * (len(srt) - 1))], 4),
            "mean_trades": round(statistics.mean(v["trades"] for v in vals),
                                 3),
            "break_rate_pct": round(sum(1 for v in vals if v["broke"])
                                    / len(vals) * 100, 2),
            "stop_rate_pct": round(sum(1 for v in vals if v["stopped"])
                                   / len(vals) * 100, 2)}


SCORE_BUCKETS = ((0, 40), (40, 55), (55, 70), (70, 85), (85, 101))


def scanner_study(data: Dict[str, List[dict]],
                  scans: Dict[str, Dict[int, dict]], cfg: dict,
                  horizon_hours: float = 24, every_hours: float = 4,
                  entry_mode: str = "limit", fill_through: float = 0.0002,
                  start_ms: Optional[int] = None,
                  end_ms: Optional[int] = None,
                  initial: Optional[float] = None) -> dict:
    """Moi mau (symbol, t): judge scanner tai t, dung range tu scan do,
    chay grid co lap trong horizon gio -> PnL. So DAT vs TRUOT."""
    sc = dict(scanner.DEFAULTS)
    sc.update(cfg.get("scanner") or {})
    g = range_grid.gcfg(cfg.get("grid"))
    hz = int(horizon_hours * H1_MS)
    every = int(every_hours * H1_MS)
    samples = []
    for sym, bars in data.items():
        last = bars[-1]["ts"]
        for ts in sorted(scans.get(sym, {})):
            if ts % every or ts + hz > last + BAR_MS:
                continue
            if (start_ms is not None and ts < start_ms) or \
                    (end_ms is not None and ts + hz > end_ms):
                continue
            passed, score, metrics, reasons = scanner.judge(
                scans[sym][ts], sc)
            rng = range_grid.build_range(metrics, g, ts)
            if rng is None:
                continue
            sim = PortfolioSim({sym: bars}, {sym: scans[sym]}, cfg,
                               entry_mode, ts, ts + hz, initial,
                               fixed={sym: rng}, fill_through=fill_through)
            r = sim.run()
            c = sim.counters
            samples.append({
                "symbol": sym, "ts": ts, "passed": passed, "score": score,
                "pnl": r["net_pnl"], "trades": r["trades"],
                "broke": c["breaks"] > 0,
                "stopped": (c["basket_stops"] + c["total_stops"]
                            + c["daily_stops"]) > 0,
                "reasons": reasons[:3]})
    passed = [s for s in samples if s["passed"]]
    failed = [s for s in samples if not s["passed"]]
    buckets = {}
    for lo, hi in SCORE_BUCKETS:
        buckets["%d-%d" % (lo, min(hi, 100))] = _summary(
            [s for s in samples if lo <= s["score"] < hi])
    sp, sf = _summary(passed), _summary(failed)
    edge = None
    if sp.get("n") and sf.get("n"):
        edge = round(sp["mean_pnl"] - sf["mean_pnl"], 4)
    if not sp.get("n"):
        verdict = "Không có mẫu ĐẠT -> không kết luận được (nới ngưỡng?)."
    elif edge is not None and edge > 0 and sp["mean_pnl"] > 0:
        verdict = ("Scanner CÓ ích: nhóm ĐẠT lãi TB %.2f$ > nhóm TRƯỢT %.2f$ "
                   "/ cửa sổ %gh." % (sp["mean_pnl"], sf["mean_pnl"],
                                      horizon_hours))
    elif edge is not None and edge > 0:
        verdict = ("Scanner lọc bớt lỗ (ĐẠT %.2f$ > TRƯỢT %.2f$) nhưng nhóm "
                   "ĐẠT vẫn âm -> chưa nên chạy tiền thật với cấu hình này."
                   % (sp["mean_pnl"], sf["mean_pnl"]))
    else:
        verdict = ("Scanner KHÔNG tách được: ĐẠT %.2f$ <= TRƯỢT %s$ -> chỉnh "
                   "ngưỡng trước khi bật filter." % (
                       sp["mean_pnl"], sf.get("mean_pnl")))
    return {"horizon_hours": horizon_hours, "every_hours": every_hours,
            "entry_mode": entry_mode, "samples": len(samples),
            "passed": sp, "failed": sf, "edge_mean_pnl": edge,
            "score_buckets": buckets, "verdict": verdict,
            "by_symbol": {sym: {"passed": _summary([s for s in passed
                                                    if s["symbol"] == sym]),
                                "failed": _summary([s for s in failed
                                                    if s["symbol"] == sym])}
                          for sym in sorted(data)},
            "rows": samples}


# --------------------------------------------------------- walk-forward
def candidate_cfgs(base: dict, space: Dict[str, Sequence[float]]
                   ) -> List[Tuple[dict, dict]]:
    keys = list(space)
    out = []

    def rec(i, cur):
        if i == len(keys):
            cfg = copy.deepcopy(base)
            for k, v in cur.items():
                bt1._set_path(cfg, k, v)
            out.append((dict(cur), cfg))
            return
        for v in space[keys[i]]:
            cur[keys[i]] = v
            rec(i + 1, cur)
        cur.pop(keys[i], None)

    rec(0, {})
    return out


def walk_forward(data: Dict[str, List[dict]],
                 scans: Dict[str, Dict[int, dict]], base: dict,
                 train_days: float, test_days: float,
                 step_days: Optional[float] = None,
                 space: Optional[Dict[str, Sequence[float]]] = None,
                 entry_mode: str = "limit", fill_through: float = 0.0002,
                 start_ms: Optional[int] = None,
                 rescan_minutes: int = 60, log=print) -> dict:
    space = space or DEFAULT_SPACE
    cands = candidate_cfgs(base, space)
    first_scan = min((min(v) for v in scans.values() if v), default=None)
    if first_scan is None:
        raise ValueError("khong co moc scan nao (du lieu qua ngan?)")
    t = max(first_scan, start_ms or 0)
    last = max(b[-1]["ts"] for b in data.values()) + BAR_MS
    tr, te = int(train_days * DAY_MS), int(test_days * DAY_MS)
    st = int((step_days or test_days) * DAY_MS)
    folds = []
    kw = dict(entry_mode=entry_mode, fill_through=fill_through,
              rescan_minutes=rescan_minutes)
    while t + tr + te <= last:
        best = None
        for params, cfg in cands:
            r = PortfolioSim(data, scans, cfg, start_ms=t, end_ms=t + tr,
                             **kw).run()
            sc = bt1.score_train(r)
            if best is None or sc > best[0]:
                best = (sc, params, cfg, r)
        test = PortfolioSim(data, scans, best[2], start_ms=t + tr,
                            end_ms=t + tr + te, **kw).run()
        baseline = PortfolioSim(data, scans, base, start_ms=t + tr,
                                end_ms=t + tr + te, **kw).run()
        fold = {"train_start": fmt(t), "test_start": fmt(t + tr),
                "test_end": fmt(t + tr + te), "chosen": best[1],
                "train": _brief(best[3]), "test": _brief(test),
                "baseline_test": _brief(baseline)}
        log("fold %s: chon %s train %+.2f test %+.2f (baseline %+.2f)" % (
            fold["test_start"], best[1], best[3]["net_pnl"],
            test["net_pnl"], baseline["net_pnl"]))
        folds.append(fold)
        t += st

    def tot(key):
        return round(sum(f[key]["net_pnl"] for f in folds), 4)

    return {"folds": folds, "space": {k: list(v) for k, v in space.items()},
            "entry_mode": entry_mode,
            "test_net_pnl": tot("test") if folds else 0.0,
            "baseline_test_net_pnl": tot("baseline_test") if folds else 0.0,
            "test_trades": sum(f["test"]["trades"] for f in folds),
            "worst_test_drawdown_pct": max(
                (f["test"]["max_drawdown_pct"] for f in folds), default=0.0)}


def _brief(r: dict) -> dict:
    return {k: r[k] for k in ("net_pnl", "return_pct", "trades",
                              "win_rate_pct", "net_per_trade",
                              "max_drawdown_pct", "fees", "exit_reasons")}


def fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ cli
def load_cfg(path: Optional[str], sets: Sequence[str]) -> dict:
    path = path or os.path.join(BASE, "config.example.json")
    with open(path) as f:
        cfg = json.load(f)
    for item in sets or []:
        if "=" not in item:
            raise SystemExit("--set can dang key=value: %s" % item)
        k, v = item.split("=", 1)
        try:
            val = json.loads(v)
        except ValueError:
            val = v
        bt1._set_path(cfg, k.strip(), val)
    return cfg


def parse_space(text: Optional[str]) -> Optional[Dict[str, List[float]]]:
    if not text:
        return None
    out = {}
    for part in text.split(";"):
        part = part.strip()
        if part:
            k, vals = part.split("=", 1)
            out[k.strip()] = [float(x) for x in vals.split(",") if x.strip()]
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="tai nen 5m cong khai")
    d.add_argument("--symbols", required=True, help="BTCUSDT,ETHUSDT,...")
    d.add_argument("--days", type=float, default=120)
    d.add_argument("--data-dir", required=True)

    def common(sp):
        sp.add_argument("--data-dir", required=True)
        sp.add_argument("--symbols", help="mac dinh: moi file trong data-dir")
        sp.add_argument("--config", help="mac dinh config.example.json")
        sp.add_argument("--set", action="append", default=[],
                        help="ghi de config, vd grid.tp_pct=0.012")
        sp.add_argument("--start")
        sp.add_argument("--end")
        sp.add_argument("--rescan-minutes", type=int, default=60)
        sp.add_argument("--min-h1", type=int, default=498)
        sp.add_argument("--scan-cache")
        sp.add_argument("--entry", choices=("limit", "market"),
                        default="limit")
        sp.add_argument("--fill-through", type=float, default=0.0002)
        sp.add_argument("--json-out")

    s = sub.add_parser("scan", help="tinh truoc chi bao scanner")
    common(s)
    st = sub.add_parser("study", help="kiem chung scanner")
    common(st)
    st.add_argument("--horizon-hours", type=float, default=24)
    st.add_argument("--every-hours", type=float, default=4)
    r = sub.add_parser("run", help="mo phong portfolio")
    common(r)
    w = sub.add_parser("walk-forward", help="walk-forward tp x step")
    common(w)
    w.add_argument("--train-days", type=float, default=21)
    w.add_argument("--test-days", type=float, default=7)
    w.add_argument("--step-days", type=float)
    w.add_argument("--space", help='vd "grid.tp_pct=0.008,0.01;'
                   'grid.step_mult=0.8,1.2"')
    return p


def _dump(obj: dict, path: Optional[str]) -> None:
    if path:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
        print("Da ghi %s" % path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "download":
        os.makedirs(a.data_dir, exist_ok=True)
        end = int(time.time() * 1000) // BAR_MS * BAR_MS
        start = end - int(a.days * DAY_MS)
        for sym in [x.strip().upper() for x in a.symbols.split(",")
                    if x.strip()]:
            n = bt1.download_bars(sym, start, end, data_path(a.data_dir, sym))
            print("%s: %d nen" % (sym, n))
        return 0
    cfg = load_cfg(a.config, a.set)
    syms = [x.strip().upper() for x in a.symbols.split(",")] \
        if a.symbols else None
    start, end = bt1.parse_time(a.start), bt1.parse_time(a.end)
    data = load_data(a.data_dir, syms)
    if not data:
        raise SystemExit("khong co du lieu trong %s" % a.data_dir)
    sc = cfg.get("scanner") or {}
    scans = scan_all(data, a.rescan_minutes,
                     int(sc.get("range_hours", 48)), a.min_h1, a.scan_cache)
    if a.cmd == "scan":
        print("Xong: %d symbol, %d moc" % (
            len(scans), sum(len(v) for v in scans.values())))
        return 0
    if a.cmd == "study":
        res = scanner_study(data, scans, cfg, a.horizon_hours,
                            a.every_hours, a.entry, a.fill_through,
                            start, end)
        print(json.dumps({k: v for k, v in res.items()
                          if k not in ("rows", "by_symbol")},
                         indent=1, ensure_ascii=False))
        _dump(res, a.json_out)
        return 0
    if a.cmd == "run":
        res = PortfolioSim(data, scans, cfg, a.entry, start, end,
                           fill_through=a.fill_through,
                           rescan_minutes=a.rescan_minutes).run()
        print(json.dumps(res, indent=1, ensure_ascii=False))
        _dump(res, a.json_out)
        return 0
    res = walk_forward(data, scans, cfg, a.train_days, a.test_days,
                       a.step_days, parse_space(a.space), a.entry,
                       a.fill_through, start, a.rescan_minutes)
    print(json.dumps({k: v for k, v in res.items() if k != "folds"},
                     indent=1, ensure_ascii=False))
    _dump(res, a.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
