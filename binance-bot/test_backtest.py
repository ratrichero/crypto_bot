#!/usr/bin/env python3
"""Offline regression tests for backtest.py.

Run from the repository root with::

    python3 binance-bot/test_backtest.py

The tests use only the Python standard library and never call Binance or read
API credentials.  They cover closed-candle boundaries, conservative OHLC
fills, cost accounting, funding status, daily stops, and walk-forward window
separation.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import backtest  # noqa: E402


PASS = []
FAIL = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    suffix = " | " + detail if detail and not condition else ""
    print(("PASS " if condition else "FAIL ") + name + suffix)


def make_bars(count: int, start_price: float = 100.0) -> list[dict]:
    start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    bars = []
    price = start_price
    for index in range(count):
        # Deterministic oscillation gives indicators enough variation without
        # making any test depend on random state.
        change = 0.18 if index % 11 < 6 else -0.13
        close = max(1.0, price + change)
        high = max(price, close) + 0.08
        low = min(price, close) - 0.08
        bars.append({
            "ts": start + index * backtest.BAR_MS,
            "o": price,
            "h": high,
            "l": low,
            "c": close,
            "v": 1.0,
        })
        price = close
    return bars


def test_config() -> dict:
    with open(os.path.join(BASE, "config.example.json")) as handle:
        cfg = json.load(handle)
    cfg = copy.deepcopy(cfg)
    cfg["start_equity"] = 1000.0
    cfg["fee_rate"] = 0.0005
    cfg["slippage"] = 0.0001
    cfg["grid"]["max_positions"] = 0
    cfg["grid"]["levels_each_side"] = 0
    cfg["risk"]["grid_basket_max_loss_pct"] = 0.0
    return cfg


def test_closed_candle_no_lookahead() -> None:
    bars = make_bars(180)
    index = backtest.CandleIndex(bars)
    i = 101
    context = backtest.build_context_cache(bars, index, test_config())
    signals = backtest.build_signal_cache(bars, index, test_config())

    future_changed = copy.deepcopy(bars)
    future_changed[i + 1]["h"] += 1000.0
    future_changed[i + 1]["c"] += 500.0
    future_index = backtest.CandleIndex(future_changed)
    future_context = backtest.build_context_cache(
        future_changed, future_index, test_config())
    future_signals = backtest.build_signal_cache(
        future_changed, future_index, test_config())
    check("future 5m bar cannot change current context",
          context[i] == future_context[i])
    check("future 5m bar cannot change current signal",
          signals[i] == future_signals[i])

    current_changed = copy.deepcopy(bars)
    current_changed[i]["h"] += 1000.0
    current_changed[i]["c"] += 500.0
    current_index = backtest.CandleIndex(current_changed)
    current_context = backtest.build_context_cache(
        current_changed, current_index, test_config())
    current_signals = backtest.build_signal_cache(
        current_changed, current_index, test_config())
    check("forming 5m/15m candle cannot change current context",
          context[i] == current_context[i])
    check("forming 5m/15m candle cannot change current signal",
          signals[i] == current_signals[i])


def test_intrabar_and_costs() -> None:
    cfg = test_config()
    bars = [{
        "ts": int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
        "o": 100.0, "h": 102.0, "l": 98.0, "c": 101.0, "v": 1.0,
    }]
    candle_index = backtest.CandleIndex(bars)
    simulator = backtest.Simulator(bars, candle_index, "TESTUSDT", cfg, 0, 1)
    check("manual scalp opens for intrabar test",
          simulator._open("long", 100.0, "scalp"))
    simulator.process_bar(0)
    result = simulator.result()
    check("conservative bullish OHLC path prioritizes SL",
          result["trades"] == 1 and simulator.trades[0]["reason"] == "SL",
          repr(simulator.trades))
    check("ambiguous TP/SL bars are reported",
          result["ambiguous_intrabar_events"] == 1)
    check("taker fees are reported", result["fees"] > 0)
    check("slippage cost is reported", result["slippage_paid"] > 0)


def test_funding_and_daily_stop() -> None:
    cfg = test_config()
    start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    bars = [
        {"ts": start, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0, "v": 1.0},
        {"ts": start + 8 * 60 * 60 * 1000, "o": 100.0, "h": 100.0,
         "l": 100.0, "c": 100.0, "v": 1.0},
    ]
    funding = [(bars[1]["ts"], 0.001)]
    simulator = backtest.Simulator(
        bars, backtest.CandleIndex(bars), "TESTUSDT", cfg, 0, 2,
        funding_events=funding)
    simulator._open("long", 100.0, "scalp")
    simulator.process_bar(0)
    simulator.process_bar(1)
    result = simulator.result()
    check("funding data is explicitly marked provided",
          result["funding_status"] == "provided")
    check("funding event is applied to open position",
          result["funding_events_applied"] == 1
          and result["funding_paid"] > 0)

    stop_bars = [{
        "ts": start, "o": 100.0, "h": 100.0, "l": 89.0, "c": 90.0, "v": 1.0,
    }]
    stop_cfg = copy.deepcopy(cfg)
    stop_cfg["scalp"]["sl_pct"] = 0.0
    stop_cfg["scalp"]["tp_pct"] = 0.0
    stop_simulator = backtest.Simulator(
        stop_bars, backtest.CandleIndex(stop_bars), "TESTUSDT", stop_cfg, 0, 1)
    stop_simulator._open("long", 100.0, "scalp")
    stop_simulator.process_bar(0)
    stop_result = stop_simulator.result()
    check("daily stop event is counted", stop_result["daily_stop_events"] == 1)
    check("daily stop closes the position", any(
        trade["reason"] == "DAILY_STOP" for trade in stop_simulator.trades))


def test_walk_forward_boundaries() -> None:
    bars = make_bars(5 * 24 * 12)
    cfg = test_config()
    original = copy.deepcopy(backtest.SEARCH_SPACE)
    backtest.SEARCH_SPACE = {
        "scalp.tp_pct": (cfg["scalp"]["tp_pct"],),
        "scalp.sl_pct": (cfg["scalp"]["sl_pct"],),
        "grid.step_mult": (cfg["grid"]["step_mult"],),
    }
    try:
        report = backtest.walk_forward(
            bars, "TESTUSDT", cfg, train_days=2, test_days=1, step_days=1)
    finally:
        backtest.SEARCH_SPACE = original
    check("walk-forward creates rolling OOS windows",
          len(report["folds_detail"]) == 3)
    separated = all(
        fold["train"]["end"] < fold["test"]["start"]
        and fold["test"]["end"] < bars[-1]["ts"] + backtest.BAR_MS
        for fold in report["folds_detail"]
    )
    check("train and OOS windows are time-separated", separated)
    required = {
        "net_pnl", "max_drawdown_pct", "profit_factor", "win_rate_pct",
        "trades", "expectancy", "fees", "slippage_paid", "funding_paid",
        "max_exposure_pct", "daily_stop_events",
    }
    check("OOS aggregate contains required metrics",
          required.issubset(report["oos"]["metrics"]))
    check("OOS reports missing funding instead of assuming zero",
          report["oos"]["metrics"]["funding_status"] == "not_supplied")


def main() -> int:
    test_closed_candle_no_lookahead()
    test_intrabar_and_costs()
    test_funding_and_daily_stop()
    test_walk_forward_boundaries()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
