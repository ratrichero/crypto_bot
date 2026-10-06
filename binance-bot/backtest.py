#!/usr/bin/env python3
"""Offline Binance Futures backtest and walk-forward evaluator.

The simulator intentionally mirrors the Binance strategy rules without
creating any exchange client or authenticated request.  It consumes public
5-minute OHLCV data, derives a forming 15-minute candle, evaluates signals
only from candles that were closed before an entry, and uses conservative
intrabar ordering when OHLC cannot reveal whether TP or SL happened first.

Examples:
  python3 backtest.py download --symbol BTCUSDT --days 90 \\
      --output /tmp/btcusdt-5m.jsonl
  python3 backtest.py download-funding --symbol BTCUSDT --days 90 \\
      --output /tmp/btcusdt-funding.jsonl
  python3 backtest.py walk-forward --data /tmp/btcusdt-5m.jsonl \\
      --symbol BTCUSDT --train-days 30 --test-days 7 --step-days 7 \\
      --json-out /tmp/btcusdt-walk-forward.json

The downloaded data and reports are deliberately outside Git by default.
No API key is read and no trading/private endpoint is used.
"""
from __future__ import annotations

import argparse
import csv
import copy
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from indicators import atr  # noqa: E402
import strategy  # noqa: E402

BAR_MS = 5 * 60 * 1000
GROUP_MS = 15 * 60 * 1000
DEFAULT_LIMIT = 1500
FUNDING_LIMIT = 1000


# A small, explicit search space is easier to audit than a broad optimizer.
# Leverage, margin size, max positions and risk limits are never optimized.
SEARCH_SPACE = {
    "scalp.tp_pct": (0.008, 0.010, 0.012),
    "scalp.sl_pct": (0.003, 0.004, 0.005),
    "grid.step_mult": (0.8, 1.0, 1.2),
}


def _timestamp_ms(value: Any) -> int:
    number = int(float(value))
    return number if number >= 10**11 else number * 1000


def parse_time(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return _timestamp_ms(text)
    except ValueError:
        pass
    normalized = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _bar_from_row(row: Any) -> Optional[dict]:
    """Normalize Binance array, dict, or CSV rows to one OHLCV bar."""
    if isinstance(row, (list, tuple)):
        if len(row) < 6:
            return None
        timestamp, op, hi, lo, close, volume = row[:6]
    elif isinstance(row, dict):
        timestamp = (row.get("ts") or row.get("timestamp")
                     or row.get("openTime") or row.get("open_time"))
        op = row.get("o", row.get("open"))
        hi = row.get("h", row.get("high"))
        lo = row.get("l", row.get("low"))
        close = row.get("c", row.get("close"))
        volume = row.get("v", row.get("volume", 0))
    else:
        return None
    if timestamp is None or op is None or hi is None or lo is None or close is None:
        return None
    try:
        return {
            "ts": _timestamp_ms(timestamp),
            "o": float(op),
            "h": float(hi),
            "l": float(lo),
            "c": float(close),
            "v": float(volume or 0),
        }
    except (TypeError, ValueError):
        return None


def load_bars(path: str, start_ms: Optional[int] = None,
              end_ms: Optional[int] = None) -> List[dict]:
    """Load JSON/JSONL/CSV OHLCV and remove duplicate timestamps."""
    rows: Iterable[Any]
    lower = path.lower()
    if lower.endswith(".csv"):
        with open(path, newline="") as handle:
            rows = list(csv.DictReader(handle))
    elif lower.endswith(".jsonl"):
        parsed = []
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    parsed.append(json.loads(line))
        rows = parsed
    else:
        with open(path) as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            rows = (payload.get("data") or payload.get("klines")
                    or payload.get("rows") or [])
        else:
            rows = payload

    unique: Dict[int, dict] = {}
    for row in rows:
        bar = _bar_from_row(row)
        if bar is None:
            continue
        if start_ms is not None and bar["ts"] < start_ms:
            continue
        if end_ms is not None and bar["ts"] >= end_ms:
            continue
        if not (bar["o"] > 0 and bar["h"] >= bar["l"] > 0
                and bar["h"] >= bar["o"] and bar["h"] >= bar["c"]
                and bar["l"] <= bar["o"] and bar["l"] <= bar["c"]):
            raise ValueError("invalid OHLC at timestamp %s in %s"
                             % (bar["ts"], path))
        unique[bar["ts"]] = bar
    bars = [unique[ts] for ts in sorted(unique)]
    if len(bars) > 1:
        gaps = sum(1 for a, b in zip(bars, bars[1:])
                   if b["ts"] - a["ts"] != BAR_MS)
        if gaps:
            print("WARNING: %s contains %d 5m gaps; indicators use available bars"
                  % (path, gaps), file=sys.stderr)
    return bars


def _funding_from_row(row: Any) -> Optional[Tuple[int, float]]:
    if isinstance(row, (list, tuple)):
        if len(row) < 2:
            return None
        timestamp, rate = row[:2]
    elif isinstance(row, dict):
        timestamp = (row.get("ts") or row.get("timestamp")
                     or row.get("fundingTime") or row.get("funding_time"))
        rate = (row.get("rate") if row.get("rate") is not None
                else row.get("fundingRate", row.get("funding_rate")))
    else:
        return None
    if timestamp is None or rate is None:
        return None
    try:
        return _timestamp_ms(timestamp), float(rate)
    except (TypeError, ValueError):
        return None


def load_funding(path: str) -> List[Tuple[int, float]]:
    """Load Binance funding history or a small equivalent CSV/JSON dataset."""
    lower = path.lower()
    if lower.endswith(".csv"):
        with open(path, newline="") as handle:
            rows: Iterable[Any] = list(csv.DictReader(handle))
    elif lower.endswith(".jsonl"):
        parsed = []
        with open(path) as handle:
            for line in handle:
                if line.strip():
                    parsed.append(json.loads(line))
        rows = parsed
    else:
        with open(path) as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            rows = (payload.get("data") or payload.get("fundingRates")
                    or payload.get("rows") or [])
        else:
            rows = payload
    unique: Dict[int, float] = {}
    for row in rows:
        parsed = _funding_from_row(row)
        if parsed is None:
            continue
        timestamp, rate = parsed
        if not math.isfinite(rate):
            raise ValueError("invalid funding rate at timestamp %s" % timestamp)
        unique[timestamp] = rate
    events = sorted(unique.items())
    if not events:
        raise ValueError("funding data contains no valid timestamp/rate rows")
    return events


def save_jsonl(path: str, bars: Sequence[dict]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as handle:
        for bar in bars:
            handle.write(json.dumps(bar, separators=(",", ":")) + "\n")


def download_bars(symbol: str, start_ms: int, end_ms: int,
                  output: str, limit: int = DEFAULT_LIMIT) -> int:
    """Download public Binance USD-M 5m klines with pagination only."""
    if end_ms <= start_ms:
        raise ValueError("end must be after start")
    cursor = start_ms
    rows: List[dict] = []
    while cursor < end_ms:
        query = urllib.parse.urlencode({
            "symbol": symbol.upper(),
            "interval": "5m",
            "limit": min(int(limit), DEFAULT_LIMIT),
            "startTime": cursor,
            "endTime": end_ms - 1,
        })
        url = "https://fapi.binance.com/fapi/v1/klines?" + query
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json",
                     "User-Agent": "crypto-bot-backtest/1.0"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise RuntimeError("Binance klines response is not a list: %r"
                               % payload)
        page = [_bar_from_row(row) for row in payload]
        page = [bar for bar in page if bar is not None]
        if not page:
            break
        rows.extend(page)
        next_cursor = page[-1]["ts"] + BAR_MS
        if next_cursor <= cursor:
            raise RuntimeError("Binance klines cursor did not advance")
        cursor = next_cursor
        print("downloaded %d bars through %s" %
              (len(rows), datetime.fromtimestamp(
                  page[-1]["ts"] / 1000, tz=timezone.utc).isoformat()),
              flush=True)
        if len(page) < min(int(limit), DEFAULT_LIMIT):
            break
        time.sleep(0.2)
    # A live Binance response can contain the still-forming last kline. Keep
    # only bars whose 5m close is at or before the requested cutoff.
    unique = {bar["ts"]: bar for bar in rows
              if start_ms <= bar["ts"] < end_ms
              and bar["ts"] + BAR_MS <= end_ms}
    result = [unique[ts] for ts in sorted(unique)]
    save_jsonl(output, result)
    return len(result)


def save_funding_jsonl(path: str,
                       events: Sequence[Tuple[int, float]]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as handle:
        for timestamp, rate in events:
            handle.write(json.dumps({"ts": timestamp, "rate": rate},
                                    separators=(",", ":")) + "\n")


def download_funding(symbol: str, start_ms: int, end_ms: int,
                     output: str, limit: int = FUNDING_LIMIT) -> int:
    """Download public Binance USD-M funding events with pagination."""
    if end_ms <= start_ms:
        raise ValueError("end must be after start")
    cursor = start_ms
    rows: List[Tuple[int, float]] = []
    page_limit = min(int(limit), FUNDING_LIMIT)
    while cursor < end_ms:
        query = urllib.parse.urlencode({
            "symbol": symbol.upper(),
            "limit": page_limit,
            "startTime": cursor,
            "endTime": end_ms - 1,
        })
        url = "https://fapi.binance.com/fapi/v1/fundingRate?" + query
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json",
                     "User-Agent": "crypto-bot-backtest/1.0"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise RuntimeError("Binance funding response is not a list: %r"
                               % payload)
        page = [_funding_from_row(row) for row in payload]
        page = [event for event in page if event is not None]
        if not page:
            break
        rows.extend(page)
        next_cursor = page[-1][0] + 1
        if next_cursor <= cursor:
            raise RuntimeError("Binance funding cursor did not advance")
        cursor = next_cursor
        print("downloaded %d funding events through %s" %
              (len(rows), datetime.fromtimestamp(
                  page[-1][0] / 1000, tz=timezone.utc).isoformat()),
              flush=True)
        if len(page) < page_limit:
            break
        time.sleep(0.2)
    unique = {timestamp: rate for timestamp, rate in rows
              if start_ms <= timestamp < end_ms}
    events = sorted(unique.items())
    save_funding_jsonl(output, events)
    return len(events)


def _aggregate_group(group: Sequence[dict]) -> dict:
    return {
        "ts": (group[0]["ts"] // GROUP_MS) * GROUP_MS,
        "o": group[0]["o"],
        "h": max(row["h"] for row in group),
        "l": min(row["l"] for row in group),
        "c": group[-1]["c"],
        "v": sum(row.get("v", 0) for row in group),
    }


class CandleIndex:
    """Incremental 15m aggregation with a forming group per 5m bar."""

    def __init__(self, bars: Sequence[dict]):
        self.completed: List[dict] = []
        self.completed_count: List[int] = []
        self.forming: List[dict] = []
        current: Optional[dict] = None
        current_key: Optional[int] = None
        current_rows: List[dict] = []
        for bar in bars:
            key = (bar["ts"] // GROUP_MS) * GROUP_MS
            if current is None or key != current_key:
                if current_rows:
                    self.completed.append(_aggregate_group(current_rows))
                current_rows = [bar]
                current_key = key
            else:
                current_rows.append(bar)
            current = _aggregate_group(current_rows)
            self.completed_count.append(len(self.completed))
            self.forming.append(current)

    def candles(self, index: int, limit: int = 100) -> List[dict]:
        count = self.completed_count[index]
        return (self.completed[max(0, count - limit):count]
                + [self.forming[index]])


def _set_path(cfg: dict, dotted: str, value: Any) -> None:
    current = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value


def candidate_configs(base: dict) -> List[Tuple[dict, dict]]:
    candidates = []
    for tp in SEARCH_SPACE["scalp.tp_pct"]:
        for sl in SEARCH_SPACE["scalp.sl_pct"]:
            for step_mult in SEARCH_SPACE["grid.step_mult"]:
                cfg = copy.deepcopy(base)
                _set_path(cfg, "scalp.tp_pct", tp)
                _set_path(cfg, "scalp.sl_pct", sl)
                _set_path(cfg, "grid.step_mult", step_mult)
                params = {"scalp.tp_pct": tp, "scalp.sl_pct": sl,
                          "grid.step_mult": step_mult}
                candidates.append((cfg, params))
    return candidates


def _utc_day(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()


class Simulator:
    """Single-symbol event simulator matching the current Binance strategy."""

    def __init__(self, bars: Sequence[dict], candle_index: CandleIndex,
                 symbol: str, cfg: dict, start: int, end: int,
                 context_cache: Optional[Sequence[dict]] = None,
                 signal_cache: Optional[Sequence[Tuple[Optional[str], dict]]] = None,
                 funding_events: Optional[Sequence[Tuple[int, float]]] = None):
        self.bars = bars
        self.candles = candle_index
        self.symbol = symbol
        self.cfg = cfg
        self.start = start
        self.end = end
        self.equity = float(cfg.get("start_equity", 1000.0))
        self.initial_equity = self.equity
        self.positions: List[dict] = []
        self.trades: List[dict] = []
        self.equity_curve: List[float] = []
        self.exposure_curve: List[dict] = []
        self.regime: Optional[str] = None
        self.candidate_regime: Optional[str] = None
        self.candidate_count = 0
        self.last_regime_bar: Optional[int] = None
        self.grid = {"anchor": None, "taken": {}, "step": None,
                     "rebuild_pending": False, "risk_halted": False}
        self.halted = False
        self.halt_reason = ""
        self.day = _utc_day(bars[start]["ts"])
        self.day_start_equity = self.equity
        self.fees = 0.0
        self.slippage_paid = 0.0
        self.funding_paid = 0.0
        self.funding_events_applied = 0
        self.funding_events = list(funding_events) if funding_events is not None else None
        self.funding_index = 0
        self.funding_rate_8h = cfg.get("backtest_funding_rate_8h")
        if self.funding_rate_8h is not None:
            self.funding_rate_8h = float(self.funding_rate_8h)
        self.next_funding_ms = self._next_funding_boundary(bars[start]["ts"])
        self.funding_status = (
            "provided" if self.funding_events is not None else
            "constant_assumption" if self.funding_rate_8h is not None else
            "not_supplied"
        )
        if self.funding_events is not None:
            while (self.funding_index < len(self.funding_events)
                   and self.funding_events[self.funding_index][0] <= bars[start]["ts"]):
                self.funding_index += 1
        self.daily_stop_events = 0
        self.grid_basket_stop_events = 0
        self.ambiguous_intrabar_events = 0
        self._pid = 0
        self.current_timestamp_ms = bars[start]["ts"]
        self.cooldown_until_ms = 0
        self.context_cache = context_cache
        self.signal_cache = signal_cache
        self._warm_context(start)

    def _next_funding_boundary(self, timestamp_ms: int) -> int:
        period = 8 * 60 * 60 * 1000
        return ((timestamp_ms // period) + 1) * period

    def _warm_context(self, index: int) -> None:
        """Load precomputed regime context without replaying market events."""
        if self.context_cache is not None and index > 0:
            cached = self.context_cache[index - 1]
            self.regime = cached["regime"]
            self.candidate_regime = cached.get("candidate")
            self.candidate_count = cached.get("candidate_count", 0)

    def _context(self, index: int) -> Tuple[List[dict], List[dict]]:
        c15 = self.candles.candles(index)
        c5 = list(self.bars[max(0, index - 100):index + 1])
        return c5, c15

    def _update_context(self, index: int) -> None:
        bar = self.bars[index]
        if self.context_cache is not None:
            cached = self.context_cache[index]
            self.regime = cached["regime"]
            current_atr = cached["atr"]
        else:
            c15 = self.candles.candles(index)
            closed_count = self.candles.completed_count[index]
            if closed_count != self.last_regime_bar:
                candidate, _adx = strategy.detect_regime(
                    c15,
                    self.cfg["adx_threshold"],
                    previous=self.regime,
                    range_threshold=self.cfg.get("adx_range_threshold"),
                )
                if self.regime is None:
                    self.regime = candidate
                    self.candidate_regime = None
                    self.candidate_count = 0
                elif candidate == self.regime:
                    self.candidate_regime = None
                    self.candidate_count = 0
                elif candidate == self.candidate_regime:
                    self.candidate_count += 1
                    if self.candidate_count >= int(
                            self.cfg.get("regime_confirm_bars", 2)):
                        self.regime = candidate
                        self.candidate_regime = None
                        self.candidate_count = 0
                else:
                    self.candidate_regime = candidate
                    self.candidate_count = 1
                self.last_regime_bar = closed_count
            try:
                current_atr = atr(c15[:-1], 14)
            except Exception:
                current_atr = None
        grid_cfg = self.cfg["grid"]
        if current_atr and bar["o"] > 0:
            step = round(min(grid_cfg["step_max"], max(
                grid_cfg["step_min"],
                grid_cfg["step_mult"] * current_atr / bar["o"],
            )), 6)
        else:
            step = grid_cfg["step_pct"]
        self.grid["step"] = step

    def _mark_equity(self, price: float) -> float:
        unrealized = 0.0
        for pos in self.positions:
            if pos["side"] == "long":
                unrealized += (price - pos["entry"]) * pos["qty"]
            else:
                unrealized += (pos["entry"] - price) * pos["qty"]
        return self.equity + unrealized

    def _mark_notional(self, price: float) -> float:
        return sum(abs(pos["qty"] * price) for pos in self.positions)

    def _symbol_disabled(self) -> bool:
        disabled_until = self.cfg.get("disabled_symbols", {}).get(self.symbol, 0)
        try:
            return self.current_timestamp_ms / 1000 < float(disabled_until)
        except (TypeError, ValueError):
            return False

    def _used_margin(self) -> float:
        return sum(pos["notional"] / self.cfg["leverage"]
                   for pos in self.positions)

    def _next_id(self) -> int:
        self._pid += 1
        return self._pid

    def _fee(self, notional: float) -> float:
        return notional * float(self.cfg["fee_rate"])

    def _open(self, side: str, price: float, tag: str,
              level: Optional[str] = None) -> bool:
        notional = float(self.cfg["order_margin_usdt"]
                         * self.cfg["leverage"])
        if len(self.positions) >= int(self.cfg.get("max_total_positions", 999)):
            return False
        if self._used_margin() + notional / self.cfg["leverage"] > self.equity:
            return False
        mark_equity = self._mark_equity(price)
        current_notional = self._mark_notional(price)
        if (current_notional + notional
                > mark_equity * self.cfg["risk"]["max_notional_mult"]):
            return False
        slip = float(self.cfg.get("slippage", 0.0))
        entry = price * (1 + slip) if side == "long" else price * (1 - slip)
        self.slippage_paid += abs(entry - price) * (notional / entry)
        sl_pct = (self.cfg["scalp"]["sl_pct"] if tag == "scalp" else 0.0)
        tp_pct = (self.cfg["scalp"]["tp_pct"] if tag == "scalp"
                  else self.grid["step"])
        if side == "long":
            sl = entry * (1 - sl_pct) if sl_pct else None
            tp = entry * (1 + tp_pct) if tp_pct else None
        else:
            sl = entry * (1 + sl_pct) if sl_pct else None
            tp = entry * (1 - tp_pct) if tp_pct else None
        fee = self._fee(notional)
        self.equity -= fee
        self.fees += fee
        self.positions.append({
            "id": self._next_id(), "symbol": self.symbol, "side": side,
            "qty": notional / entry, "entry": entry, "notional": notional,
            "sl": sl, "tp": tp, "tag": tag, "level": level,
            "entry_fee": fee,
        })
        return True

    def _close(self, pos: dict, price: float, reason: str) -> dict:
        slip = float(self.cfg.get("slippage", 0.0))
        exit_price = price * (1 - slip) if pos["side"] == "long" else price * (1 + slip)
        self.slippage_paid += abs(exit_price - price) * pos["qty"]
        if pos["side"] == "long":
            gross = (exit_price - pos["entry"]) * pos["qty"]
        else:
            gross = (pos["entry"] - exit_price) * pos["qty"]
        exit_fee = self._fee(pos["notional"])
        self.equity += gross - exit_fee
        self.fees += exit_fee
        self.positions = [item for item in self.positions if item["id"] != pos["id"]]
        if pos.get("level") is not None:
            self.grid["taken"].pop(pos["level"], None)
        net = gross - pos.get("entry_fee", 0.0) - exit_fee
        record = {
            "id": pos["id"], "symbol": self.symbol, "side": pos["side"],
            "tag": pos["tag"], "level": pos.get("level"),
            "entry": pos["entry"], "exit": exit_price,
            "notional": pos["notional"], "pnl": net, "reason": reason,
        }
        self.trades.append(record)
        if pos["tag"] == "scalp" and reason == "SL":
            cooldown_minutes = float(
                self.cfg["scalp"].get("cooldown_after_sl_min", 0)
            )
            self.cooldown_until_ms = self.current_timestamp_ms + int(
                cooldown_minutes * 60 * 1000
            )
        return record

    def _close_all(self, price: float, reason: str) -> None:
        for pos in list(self.positions):
            self._close(pos, price, reason)

    def _charge_funding(self, rate: float, mark_price: float) -> None:
        for pos in self.positions:
            payment = abs(pos["qty"] * mark_price) * rate
            if pos["side"] == "short":
                payment = -payment
            self.equity -= payment
            self.funding_paid += payment
        self.funding_events_applied += 1

    def _apply_funding(self, timestamp_ms: int, mark_price: float) -> None:
        if self.funding_events is not None:
            while (self.funding_index < len(self.funding_events)
                   and self.funding_events[self.funding_index][0] <= timestamp_ms):
                _event_ts, rate = self.funding_events[self.funding_index]
                self._charge_funding(rate, mark_price)
                self.funding_index += 1
            return
        if self.funding_rate_8h is None:
            return
        while timestamp_ms >= self.next_funding_ms:
            self._charge_funding(self.funding_rate_8h, mark_price)
            self.next_funding_ms += 8 * 60 * 60 * 1000

    def _risk_check(self, price: float) -> None:
        mark_equity = self._mark_equity(price)
        daily_limit = float(self.cfg["risk"]["daily_max_loss_pct"])
        if (not self.halted and self.day_start_equity > 0
                and mark_equity <= self.day_start_equity * (1 - daily_limit)):
            self.halted = True
            self.halt_reason = "daily stop"
            self.daily_stop_events += 1
            self._close_all(price, "DAILY_STOP")
            self.grid["risk_halted"] = True
        grid_limit = float(self.cfg["risk"].get("grid_basket_max_loss_pct", 0.0))
        if grid_limit <= 0 or self.grid["risk_halted"]:
            return
        grid_pnl = 0.0
        for pos in self.positions:
            if pos["tag"] != "grid":
                continue
            grid_pnl += ((price - pos["entry"]) * pos["qty"]
                         if pos["side"] == "long"
                         else (pos["entry"] - price) * pos["qty"])
        if grid_pnl <= -self._mark_equity(price) * grid_limit and grid_pnl < 0:
            self.grid_basket_stop_events += 1
            self.grid["risk_halted"] = True
            self.grid["rebuild_pending"] = True
            for pos in list(self.positions):
                if pos["tag"] == "grid":
                    self._close(pos, price, "GRID_BASKET_STOP")
            self.grid["taken"] = {}

    def _exit_at_price(self, price: float) -> None:
        # When an OHLC bar touches both exits, the event path and this order
        # make SL win over TP. That is deliberately conservative.
        for pos in list(self.positions):
            if pos["side"] == "long":
                if pos.get("sl") is not None and price <= pos["sl"]:
                    self._close(pos, price, "SL")
                elif pos.get("tp") is not None and price >= pos["tp"]:
                    self._close(pos, price, "TP")
            else:
                if pos.get("sl") is not None and price >= pos["sl"]:
                    self._close(pos, price, "SL")
                elif pos.get("tp") is not None and price <= pos["tp"]:
                    self._close(pos, price, "TP")

    def _grid_state_at(self, price: float) -> None:
        if (self.grid["risk_halted"] or self.regime != "ranging"
                or self._symbol_disabled()):
            return
        grid_cfg = self.cfg["grid"]
        active = [pos for pos in self.positions if pos["tag"] == "grid"]
        anchor = self.grid["anchor"]
        if self.grid["rebuild_pending"] and not active:
            self.grid.update({"anchor": price, "taken": {},
                              "rebuild_pending": False})
            return
        if anchor is None:
            self.grid["anchor"] = price
            return
        if abs(price / anchor - 1) > grid_cfg["range_steps"] * self.grid["step"]:
            self.grid["rebuild_pending"] = bool(active)
            if not active:
                self.grid.update({"anchor": price, "taken": {}})
            return

    def _grid_candidates(self, a: float, b: float) -> List[Tuple[float, str, str]]:
        if (self.halted or self.grid["risk_halted"]
                or self.regime != "ranging" or self._symbol_disabled()
                or self.grid["anchor"] is None):
            return []
        if self.grid["rebuild_pending"]:
            return []
        grid_cfg = self.cfg["grid"]
        n_grid = sum(1 for pos in self.positions if pos["tag"] == "grid")
        if n_grid >= int(grid_cfg["max_positions"]):
            return []
        levels: List[Tuple[float, str, str]] = []
        anchor = self.grid["anchor"]
        step = self.grid["step"]
        for k in range(1, int(grid_cfg["levels_each_side"]) + 1):
            if n_grid + len(levels) >= int(grid_cfg["max_positions"]):
                break
            buy_level = anchor * (1 - k * step)
            buy_key = "b%d" % k
            sell_level = anchor * (1 + k * step)
            sell_key = "s%d" % k
            for level, key, side in ((buy_level, buy_key, "long"),
                                      (sell_level, sell_key, "short")):
                if key in self.grid["taken"]:
                    continue
                if a == b and level == a:
                    levels.append((level, key, side))
                elif b > a and a < level <= b:
                    levels.append((level, key, side))
                elif b < a and b <= level < a:
                    levels.append((level, key, side))
        return levels

    def _process_point(self, price: float, grid_entries_left: List[int]) -> None:
        self._risk_check(price)
        self._exit_at_price(price)
        self._risk_check(price)
        self._grid_state_at(price)
        if self.halted or grid_entries_left[0] <= 0:
            return
        candidates = self._grid_candidates(price, price)
        # Exact-touch entries are handled by the segment event loop; this
        # branch is only useful when an open is exactly on a level.
        if candidates:
            level, key, side = candidates[0]
            if self._open(side, level, "grid", key):
                self.grid["taken"][key] = self.positions[-1]["id"]
                grid_entries_left[0] -= 1

    def _process_segment(self, start_price: float, end_price: float,
                         grid_entries_left: List[int]) -> None:
        if start_price == end_price:
            self._process_point(end_price, grid_entries_left)
            return
        current = start_price
        for _ in range(100):
            direction = 1 if end_price > current else -1
            candidates: List[Tuple[float, str, Any]] = []
            for pos in self.positions:
                trigger: Optional[float] = None
                reason = ""
                if direction > 0:
                    if pos.get("tp") is not None and current < pos["tp"] <= end_price:
                        trigger, reason = pos["tp"], "TP"
                    if pos.get("sl") is not None and current < pos["sl"] <= end_price:
                        # A long SL is below entry, while a short SL is above;
                        # only the trigger in the direction of travel applies.
                        if pos["side"] == "short":
                            trigger, reason = pos["sl"], "SL"
                else:
                    if pos.get("sl") is not None and end_price <= pos["sl"] < current:
                        if pos["side"] == "long":
                            trigger, reason = pos["sl"], "SL"
                    if pos.get("tp") is not None and end_price <= pos["tp"] < current:
                        if pos["side"] == "short":
                            trigger, reason = pos["tp"], "TP"
                if trigger is not None:
                    fraction = abs((trigger - current) / (end_price - current))
                    candidates.append((fraction, "exit", (pos["id"], reason)))
            if grid_entries_left[0] > 0:
                for level, key, side in self._grid_candidates(current, end_price):
                    fraction = abs((level - current) / (end_price - current))
                    candidates.append((fraction, "grid", (level, key, side)))
            if not candidates:
                self._process_point(end_price, grid_entries_left)
                return
            next_fraction = min(item[0] for item in candidates)
            event_price = current + (end_price - current) * next_fraction
            # Exits at a price are processed before a grid entry at that price,
            # matching the live loop's update_positions -> manage_grid order.
            self._process_point(event_price, grid_entries_left)
            for fraction, kind, payload in candidates:
                if abs(fraction - next_fraction) > 1e-9 or kind != "grid":
                    continue
                level, key, side = payload
                if key in self.grid["taken"] or self.halted:
                    continue
                if self._open(side, level, "grid", key):
                    self.grid["taken"][key] = self.positions[-1]["id"]
                    grid_entries_left[0] -= 1
            if abs(event_price - end_price) < 1e-9:
                return
            current = event_price
        raise RuntimeError("too many intrabar events; possible simulator loop")

    def _maybe_open_scalp(self, index: int, price: float) -> None:
        if self.halted or self.regime != "trending" or self._symbol_disabled():
            return
        if self.current_timestamp_ms < self.cooldown_until_ms:
            return
        s = self.cfg["scalp"]
        if sum(1 for pos in self.positions if pos["tag"] == "scalp") >= s["max_positions"]:
            return
        if any(pos["tag"] == "scalp" and pos["symbol"] == self.symbol
               for pos in self.positions):
            return
        if self.signal_cache is not None:
            signal, _info = self.signal_cache[index]
        else:
            c5, c15 = self._context(index)
            signal, _info = strategy.scalp_signal(c5, c15, self.cfg)
        if signal:
            self._open(signal, price, "scalp")

    def _count_ambiguous_exits(self, bar: dict) -> None:
        for pos in self.positions:
            sl = pos.get("sl")
            tp = pos.get("tp")
            if sl is None or tp is None:
                continue
            touches_sl = bar["l"] <= sl <= bar["h"]
            touches_tp = bar["l"] <= tp <= bar["h"]
            if touches_sl and touches_tp:
                self.ambiguous_intrabar_events += 1

    def process_bar(self, index: int) -> None:
        bar = self.bars[index]
        timestamp = bar["ts"]
        self.current_timestamp_ms = timestamp
        current_day = _utc_day(timestamp)
        if current_day != self.day:
            self.day = current_day
            self.day_start_equity = self._mark_equity(bar["o"])
            self.halted = False
            self.halt_reason = ""
            self.grid["risk_halted"] = False
            self.grid["rebuild_pending"] = False
        self._apply_funding(timestamp, bar["o"])
        self._update_context(index)
        # Live order flow updates existing positions, then manages the grid,
        # then evaluates scalp entries. Keep that ordering at the bar open.
        self._process_segment(bar["o"], bar["o"], [1])
        self._maybe_open_scalp(index, bar["o"])
        self._count_ambiguous_exits(bar)
        if bar["c"] >= bar["o"]:
            path = (bar["o"], bar["l"], bar["h"], bar["c"])
        else:
            path = (bar["o"], bar["h"], bar["l"], bar["c"])
        entries_left = [int(self.cfg["grid"].get("max_entries_per_cycle", 1))]
        for start_price, end_price in zip(path, path[1:]):
            self._process_segment(start_price, end_price, entries_left)
        mark_equity = self._mark_equity(bar["c"])
        mark_notional = self._mark_notional(bar["c"])
        self.equity_curve.append(mark_equity)
        self.exposure_curve.append({
            "notional": mark_notional,
            "pct": (mark_notional / mark_equity * 100
                     if mark_equity > 0 else 0.0),
        })

    def finish(self) -> None:
        if self.positions:
            self._close_all(self.bars[self.end - 1]["c"], "END_OF_TEST")
        if not self.equity_curve or self.equity_curve[-1] != self.equity:
            self.equity_curve.append(self.equity)

    def result(self) -> dict:
        self.finish()
        if self.funding_events is not None:
            segment_start = self.bars[self.start]["ts"]
            segment_end = self.bars[self.end - 1]["ts"]
            funding_available = sum(
                segment_start <= timestamp <= segment_end
                for timestamp, _rate in self.funding_events
            )
        else:
            funding_available = 0
        funding_status = self.funding_status
        if self.funding_events is not None and funding_available == 0:
            funding_status = "provided_no_events_in_segment"
        return metrics(
            self.initial_equity, self.equity, self.trades,
            self.equity_curve, self.fees, self.funding_paid,
            self.halted, self.halt_reason,
            slippage_paid=self.slippage_paid,
            exposure_curve=self.exposure_curve,
            daily_stop_events=self.daily_stop_events,
            grid_basket_stop_events=self.grid_basket_stop_events,
            funding_status=funding_status,
            funding_events_available=funding_available,
            funding_events_applied=self.funding_events_applied,
            ambiguous_intrabar_events=self.ambiguous_intrabar_events,
        )


def metrics(initial: float, final: float, trades: Sequence[dict],
            equity_curve: Sequence[float], fees: float,
            funding_paid: float, halted: bool, halt_reason: str,
            slippage_paid: float = 0.0,
            exposure_curve: Optional[Sequence[dict]] = None,
            daily_stop_events: int = 0,
            grid_basket_stop_events: int = 0,
            funding_status: str = "not_supplied",
            funding_events_available: int = 0,
            funding_events_applied: int = 0,
            ambiguous_intrabar_events: int = 0) -> dict:
    peak = initial
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss if gross_loss
                     else (float("inf") if wins else 0.0))
    exposure = list(exposure_curve or [])
    exposure_pct = [float(item["pct"]) for item in exposure]
    exposure_notional = [float(item["notional"]) for item in exposure]
    net_trade_pnl = sum(pnls) - funding_paid
    return {
        "initial_equity": round(initial, 8),
        "final_equity": round(final, 8),
        "net_pnl": round(final - initial, 8),
        "return_pct": round((final / initial - 1) * 100 if initial else 0, 8),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100 if trades else 0, 4),
        "gross_profit": round(gross_profit, 8),
        "gross_loss": round(gross_loss, 8),
        "profit_factor": (round(profit_factor, 6)
                          if math.isfinite(profit_factor) else "inf"),
        "expectancy": round(net_trade_pnl / len(pnls) if pnls else 0.0, 8),
        "expectancy_per_trade": round(net_trade_pnl / len(pnls) if pnls else 0.0, 8),
        "max_drawdown_pct": round(max_drawdown * 100, 8),
        "drawdown_scope": "segment_equity_curve",
        "fees": round(fees, 8),
        "slippage_paid": round(slippage_paid, 8),
        "funding_paid": round(funding_paid, 8),
        "funding_status": funding_status,
        "funding_events_available": funding_events_available,
        "funding_events_applied": funding_events_applied,
        "ambiguous_intrabar_events": ambiguous_intrabar_events,
        "intrabar_policy": "bullish_ohlc_path_low_first; bearish_ohlc_path_high_first; SL_checked_first_at_equal_price",
        "max_exposure_pct": round(max(exposure_pct) if exposure_pct else 0.0, 8),
        "average_exposure_pct": round(
            sum(exposure_pct) / len(exposure_pct) if exposure_pct else 0.0, 8),
        "max_exposure_usdt": round(
            max(exposure_notional) if exposure_notional else 0.0, 8),
        "exposure_bars_pct": round(
            sum(1 for value in exposure_notional if value > 0)
            / len(exposure_notional) * 100 if exposure_notional else 0.0,
            8,
        ),
        "exposure_samples": len(exposure),
        "daily_stop_events": daily_stop_events,
        "grid_basket_stop_events": grid_basket_stop_events,
        "halted": halted,
        "halt_reason": halt_reason,
    }


def build_context_cache(bars: Sequence[dict], candle_index: CandleIndex,
                        cfg: dict) -> List[dict]:
    """Precompute regime/ATR once; walk-forward search does not tune them."""
    cache: List[dict] = []
    last_completed_count: Optional[int] = None
    regime: Optional[str] = None
    candidate_regime: Optional[str] = None
    candidate_count = 0
    current_atr = None
    for index, _bar in enumerate(bars):
        completed_count = candle_index.completed_count[index]
        c15 = candle_index.candles(index)
        if completed_count != last_completed_count:
            candidate, _adx = strategy.detect_regime(
                c15,
                cfg["adx_threshold"],
                previous=regime,
                range_threshold=cfg.get("adx_range_threshold"),
            )
            if regime is None:
                regime = candidate
                candidate_regime = None
                candidate_count = 0
            elif candidate == regime:
                candidate_regime = None
                candidate_count = 0
            elif candidate == candidate_regime:
                candidate_count += 1
                if candidate_count >= int(cfg.get("regime_confirm_bars", 2)):
                    regime = candidate
                    candidate_regime = None
                    candidate_count = 0
            else:
                candidate_regime = candidate
                candidate_count = 1
            try:
                current_atr = atr(c15[:-1], 14)
            except Exception:
                current_atr = None
            last_completed_count = completed_count
        cache.append({"regime": regime, "atr": current_atr,
                      "candidate": candidate_regime,
                      "candidate_count": candidate_count})
    return cache


def build_signal_cache(bars: Sequence[dict], candle_index: CandleIndex,
                       cfg: dict) -> List[Tuple[Optional[str], dict]]:
    """Precompute signal-only work; search parameters are exit/grid values."""
    cache = []
    for index in range(len(bars)):
        c5 = list(bars[max(0, index - 100):index + 1])
        c15 = candle_index.candles(index)
        cache.append(strategy.scalp_signal(c5, c15, cfg))
    return cache


def run_segment(bars: Sequence[dict], candle_index: CandleIndex,
                symbol: str, cfg: dict, start: int, end: int,
                context_cache: Optional[Sequence[dict]] = None,
                signal_cache: Optional[Sequence[Tuple[Optional[str], dict]]] = None,
                funding_events: Optional[Sequence[Tuple[int, float]]] = None) -> dict:
    if end <= start:
        raise ValueError("segment end must be after start")
    simulator = Simulator(bars, candle_index, symbol, cfg, start, end,
                           context_cache=context_cache,
                           signal_cache=signal_cache,
                           funding_events=funding_events)
    for index in range(start, end):
        simulator.process_bar(index)
    return simulator.result()


def score_train(result: dict) -> float:
    """Train selector: reward return, penalize drawdown and tiny samples."""
    if result["trades"] < 3:
        return -1e9 + result["net_pnl"]
    return (result["return_pct"] - 0.50 * result["max_drawdown_pct"]
            + min(result["trades"], 100) * 0.001)


def aggregate_window_metrics(results: Sequence[dict], initial: float,
                             compounded_final: float) -> dict:
    """Combine independent OOS windows without pretending they are one fill log."""
    if not results:
        return metrics(initial, initial, [], [], 0.0, 0.0, False, "")
    trades = sum(int(result["trades"]) for result in results)
    wins = sum(int(result["wins"]) for result in results)
    losses = sum(int(result["losses"]) for result in results)
    gross_profit = sum(float(result.get("gross_profit", 0.0))
                       for result in results)
    gross_loss = sum(float(result.get("gross_loss", 0.0))
                     for result in results)
    exposure_samples = sum(int(result.get("exposure_samples", 0))
                           for result in results)
    weighted_exposure = sum(
        float(result.get("average_exposure_pct", 0.0))
        * int(result.get("exposure_samples", 0)) for result in results
    )
    weighted_active = sum(
        float(result.get("exposure_bars_pct", 0.0))
        * int(result.get("exposure_samples", 0)) for result in results
    )
    funding_statuses = {result.get("funding_status", "not_supplied")
                        for result in results}
    funding_status = (next(iter(funding_statuses))
                      if len(funding_statuses) == 1 else "mixed")
    return {
        "initial_equity": round(initial, 8),
        "final_equity": round(compounded_final, 8),
        "net_pnl": round(compounded_final - initial, 8),
        "return_pct": round((compounded_final / initial - 1) * 100
                             if initial else 0.0, 8),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / trades * 100 if trades else 0.0, 4),
        "gross_profit": round(gross_profit, 8),
        "gross_loss": round(gross_loss, 8),
        "profit_factor": (round(gross_profit / gross_loss, 6)
                          if gross_loss else ("inf" if wins else 0.0)),
        "expectancy": round(sum(float(result.get("net_pnl", 0.0))
                                 for result in results) / trades
                        if trades else 0.0, 8),
        "expectancy_per_trade": round(sum(float(result.get("net_pnl", 0.0))
                                           for result in results) / trades
                                  if trades else 0.0, 8),
        "max_drawdown_pct": round(max(
            float(result.get("max_drawdown_pct", 0.0)) for result in results), 8),
        "drawdown_scope": "maximum_independent_test_window",
        "fees": round(sum(float(result.get("fees", 0.0)) for result in results), 8),
        "slippage_paid": round(sum(
            float(result.get("slippage_paid", 0.0)) for result in results), 8),
        "funding_paid": round(sum(
            float(result.get("funding_paid", 0.0)) for result in results), 8),
        "funding_status": funding_status,
        "funding_events_available": sum(
            int(result.get("funding_events_available", 0)) for result in results),
        "funding_events_applied": sum(
            int(result.get("funding_events_applied", 0)) for result in results),
        "ambiguous_intrabar_events": sum(
            int(result.get("ambiguous_intrabar_events", 0)) for result in results),
        "intrabar_policy": "bullish_ohlc_path_low_first; bearish_ohlc_path_high_first; SL_checked_first_at_equal_price",
        "max_exposure_pct": round(max(
            float(result.get("max_exposure_pct", 0.0)) for result in results), 8),
        "average_exposure_pct": round(
            weighted_exposure / exposure_samples if exposure_samples else 0.0, 8),
        "max_exposure_usdt": round(max(
            float(result.get("max_exposure_usdt", 0.0)) for result in results), 8),
        "exposure_bars_pct": round(
            weighted_active / exposure_samples if exposure_samples else 0.0, 8),
        "exposure_samples": exposure_samples,
        "daily_stop_events": sum(int(result.get("daily_stop_events", 0))
                                 for result in results),
        "grid_basket_stop_events": sum(
            int(result.get("grid_basket_stop_events", 0)) for result in results),
        "halted": any(bool(result.get("halted")) for result in results),
        "halt_reason": "; ".join(sorted({str(result.get("halt_reason", ""))
                                             for result in results
                                             if result.get("halt_reason")})),
    }


def walk_forward(bars: Sequence[dict], symbol: str, base_cfg: dict,
                 train_days: int, test_days: int, step_days: int,
                 funding_events: Optional[Sequence[Tuple[int, float]]] = None) -> dict:
    if train_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train/test/step days must be positive")
    candle_index = CandleIndex(bars)
    context_cache = build_context_cache(bars, candle_index, base_cfg)
    signal_cache = build_signal_cache(bars, candle_index, base_cfg)
    bars_per_day = 24 * 60 // 5
    train_n = train_days * bars_per_day
    test_n = test_days * bars_per_day
    step_n = step_days * bars_per_day
    if len(bars) < train_n + test_n:
        raise ValueError("need at least %d bars for %dd train + %dd test; got %d"
                         % (train_n + test_n, train_days, test_days, len(bars)))
    folds = []
    start = 0
    candidates = candidate_configs(base_cfg)
    while start + train_n + test_n <= len(bars):
        train_end = start + train_n
        test_end = train_end + test_n
        train_runs = []
        for cfg, params in candidates:
            result = run_segment(bars, candle_index, symbol, cfg,
                                 start, train_end, context_cache, signal_cache,
                                 funding_events)
            train_runs.append((score_train(result), params, cfg, result))
        train_runs.sort(key=lambda item: item[0], reverse=True)
        _score, selected_params, selected_cfg, train_result = train_runs[0]
        test_result = run_segment(bars, candle_index, symbol, selected_cfg,
                                  train_end, test_end, context_cache,
                                  signal_cache, funding_events)
        baseline_result = run_segment(bars, candle_index, symbol, base_cfg,
                                      train_end, test_end, context_cache,
                                      signal_cache, funding_events)
        folds.append({
            "fold": len(folds) + 1,
            "train": {"start": bars[start]["ts"],
                       "end": bars[train_end - 1]["ts"],
                       "metrics": train_result},
            "test": {"start": bars[train_end]["ts"],
                     "end": bars[test_end - 1]["ts"],
                     "metrics": test_result},
            "baseline_test": baseline_result,
            "selected_params": selected_params,
            "train_score": round(_score, 8),
            "candidates": len(candidates),
        })
        start += step_n
    oos_capital = float(base_cfg.get("start_equity", 1000.0))
    baseline_capital = oos_capital
    oos_pnl = 0.0
    baseline_pnl = 0.0
    oos_trades = 0
    baseline_trades = 0
    max_fold_dd = 0.0
    for fold in folds:
        result = fold["test"]["metrics"]
        baseline = fold["baseline_test"]
        oos_capital *= 1 + result["return_pct"] / 100
        baseline_capital *= 1 + baseline["return_pct"] / 100
        oos_pnl += result["net_pnl"]
        baseline_pnl += baseline["net_pnl"]
        oos_trades += result["trades"]
        baseline_trades += baseline["trades"]
        max_fold_dd = max(max_fold_dd, result["max_drawdown_pct"])
    oos_results = [fold["test"]["metrics"] for fold in folds]
    baseline_results = [fold["baseline_test"] for fold in folds]
    oos_metrics = aggregate_window_metrics(
        oos_results, float(base_cfg.get("start_equity", 1000.0)), oos_capital)
    baseline_metrics = aggregate_window_metrics(
        baseline_results, float(base_cfg.get("start_equity", 1000.0)),
        baseline_capital)
    return {
        "schema": "binance-bot.walk-forward.v1",
        "symbol": symbol,
        "bars": len(bars),
        "bar_interval": "5m",
        "data_start": bars[0]["ts"],
        "data_end": bars[-1]["ts"],
        "windows": {"train_days": train_days, "test_days": test_days,
                     "step_days": step_days},
        "search_space": SEARCH_SPACE,
        "base_config": {
            "scalp.tp_pct": base_cfg["scalp"]["tp_pct"],
            "scalp.sl_pct": base_cfg["scalp"]["sl_pct"],
            "grid.step_mult": base_cfg["grid"]["step_mult"],
        },
        "oos": {
            "folds": len(folds),
            "metrics": oos_metrics,
            "compounded_final_equity": round(oos_capital, 8),
            "compounded_return_pct": round(
                (oos_capital / float(base_cfg.get("start_equity", 1000.0)) - 1) * 100,
                8,
            ),
            "sum_fold_pnl": round(oos_pnl, 8),
            "trades": oos_trades,
            "max_fold_drawdown_pct": round(max_fold_dd, 8),
            "baseline_compounded_final_equity": round(baseline_capital, 8),
            "baseline_compounded_return_pct": round(
                (baseline_capital / float(base_cfg.get("start_equity", 1000.0)) - 1) * 100,
                8,
            ),
            "baseline_sum_fold_pnl": round(baseline_pnl, 8),
            "baseline_trades": baseline_trades,
            "baseline_metrics": baseline_metrics,
        },
        "folds_detail": folds,
    }


def format_ts(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d")


def print_report(report: dict) -> None:
    print("WALK-FORWARD %s | %s -> %s | %d folds" %
          (report["symbol"], format_ts(report["data_start"]),
           format_ts(report["data_end"]), report["oos"]["folds"]))
    oos = report["oos"]["metrics"]
    print("OOS compounded: %+.2f%% | baseline: %+.2f%% | trades=%d baseline=%d" %
          (report["oos"]["compounded_return_pct"],
           report["oos"]["baseline_compounded_return_pct"],
           report["oos"]["trades"], report["oos"]["baseline_trades"]))
    print("OOS P&L=%+.2f DD=%.2f%% PF=%s expectancy=%+.4f "
          "fees=%.2f slippage=%.2f funding=%s exposure_max=%.2f%% "
          "daily_stops=%d ambiguous=%d" %
          (oos["net_pnl"], oos["max_drawdown_pct"], oos["profit_factor"],
           oos["expectancy"], oos["fees"], oos["slippage_paid"],
           oos["funding_status"], oos["max_exposure_pct"],
           oos["daily_stop_events"], oos["ambiguous_intrabar_events"]))
    for fold in report["folds_detail"]:
        train = fold["train"]["metrics"]
        test = fold["test"]["metrics"]
        print("  fold %d %s -> %s params=%s train=%+.2f%% test=%+.2f%% "
              "dd=%.2f%% trades=%d" %
              (fold["fold"], format_ts(fold["test"]["start"]),
               format_ts(fold["test"]["end"]), fold["selected_params"],
               train["return_pct"], test["return_pct"],
               test["max_drawdown_pct"], test["trades"]))


def load_config(path: str) -> dict:
    with open(path) as handle:
        cfg = json.load(handle)
    required = ("start_equity", "leverage", "order_margin_usdt", "fee_rate",
                "slippage", "scalp", "grid", "risk", "adx_threshold")
    missing = [key for key in required if key not in cfg]
    if missing:
        raise ValueError("config missing: " + ", ".join(missing))
    return cfg


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="download public 5m klines")
    download.add_argument("--symbol", required=True)
    download.add_argument("--start")
    download.add_argument("--end")
    download.add_argument("--days", type=int, default=90)
    download.add_argument("--output", required=True)

    funding_download = sub.add_parser(
        "download-funding", help="download public 8h funding events")
    funding_download.add_argument("--symbol", required=True)
    funding_download.add_argument("--start")
    funding_download.add_argument("--end")
    funding_download.add_argument("--days", type=int, default=90)
    funding_download.add_argument("--output", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", required=True, help="JSONL/JSON/CSV 5m OHLCV")
    common.add_argument("--symbol", default="BTCUSDT")
    common.add_argument("--config",
                        default=os.path.join(BASE, "config.example.json"))
    common.add_argument("--start")
    common.add_argument("--end")
    common.add_argument("--json-out")
    common.add_argument("--funding-data",
                        help="optional funding events JSON/JSONL/CSV; otherwise funding is not calculated")
    wf = sub.add_parser("walk-forward", parents=[common],
                        help="rolling train/test evaluation")
    wf.add_argument("--train-days", type=int, default=30)
    wf.add_argument("--test-days", type=int, default=7)
    wf.add_argument("--step-days", type=int, default=7)
    single = sub.add_parser("single", parents=[common],
                            help="run one fixed-config backtest")
    single.add_argument("--start-index", type=int, default=0)
    single.add_argument("--end-index", type=int)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("download", "download-funding"):
        end_ms = parse_time(args.end) or int(time.time() * 1000)
        start_ms = parse_time(args.start)
        if start_ms is None:
            start_ms = end_ms - int(args.days) * 24 * 60 * 60 * 1000
        if args.command == "download":
            count = download_bars(args.symbol, start_ms, end_ms, args.output)
            print("saved %d bars to %s" % (count, args.output))
        else:
            count = download_funding(args.symbol, start_ms, end_ms, args.output)
            print("saved %d funding events to %s" % (count, args.output))
        return 0

    start_ms = parse_time(args.start)
    end_ms = parse_time(args.end)
    bars = load_bars(args.data, start_ms, end_ms)
    if len(bars) < 100:
        raise SystemExit("need at least 100 valid 5m bars; got %d" % len(bars))
    cfg = load_config(args.config)
    funding_events = (load_funding(args.funding_data)
                      if args.funding_data else None)
    candle_index = CandleIndex(bars)
    if args.command == "single":
        context_cache = build_context_cache(bars, candle_index, cfg)
        signal_cache = build_signal_cache(bars, candle_index, cfg)
        start = max(0, args.start_index)
        end = args.end_index if args.end_index is not None else len(bars)
        end = min(end, len(bars))
        if start >= end:
            raise SystemExit("single segment must satisfy 0 <= start < end <= bars")
        result = run_segment(bars, candle_index, args.symbol, cfg, start, end,
                             context_cache, signal_cache, funding_events)
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.json_out:
            with open(args.json_out, "w") as handle:
                json.dump(result, handle, indent=2, sort_keys=True)
        return 0

    report = walk_forward(bars, args.symbol, cfg, args.train_days,
                          args.test_days, args.step_days, funding_events)
    print_report(report)
    if args.json_out:
        parent = os.path.dirname(os.path.abspath(args.json_out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.json_out, "w") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        print("report saved to %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
