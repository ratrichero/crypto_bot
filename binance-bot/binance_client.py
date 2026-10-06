#!/usr/bin/env python3
"""Binance USDT-M futures public market data client (no auth needed)."""
import time

import requests

BASE = "https://fapi.binance.com"
TIMEOUT = 12


def _get(path, params=None, tries=3):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(BASE + path, params=params or {},
                             timeout=TIMEOUT)
            if r.status_code in (429, 418):
                # bi gioi han: KHONG retry ngay (lam ban nang them), nem luon
                r.raise_for_status()
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            # 429/418: dung han, khong thu lai
            if "429" in str(e) or "418" in str(e):
                break
        time.sleep(1.5)
    raise last


def get_klines(symbol, interval="5m", limit=100):
    """Return oldest-first candles: [{ts,o,h,l,c}]."""
    rows = _get("/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": limit})
    out = []
    for x in rows:  # Binance tra oldest-first san
        out.append({
            "ts": int(x[0]),
            "o": float(x[1]),
            "h": float(x[2]),
            "l": float(x[3]),
            "c": float(x[4]),
        })
    return out


def get_ticker_24h():
    """All USDT-M futures 24h tickers (for universe building)."""
    return _get("/fapi/v1/ticker/24hr")


def get_exchange_info():
    """Full exchange info (symbols, filters). Cached by callers."""
    return _get("/fapi/v1/exchangeInfo")
