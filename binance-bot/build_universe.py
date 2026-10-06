#!/usr/bin/env python3
"""Build universe.json: top USDT-M perpetuals by 24h quote volume.

Run: python3 build_universe.py [top_n]
Writes universe.json: [{"symbol": "BTCUSDT", "quoteVolume": ...}, ...]
Public endpoints only, no API key needed.
"""
import json
import os
import sys

import binance_client

BASE = os.path.dirname(os.path.abspath(__file__))
TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def main():
    info = binance_client.get_exchange_info()
    perps = {s["symbol"] for s in info["symbols"]
             if s.get("contractType") == "PERPETUAL"
             and s.get("quoteAsset") == "USDT"
             and s.get("status") == "TRADING"}
    tickers = binance_client.get_ticker_24h()
    rows = []
    for t in tickers:
        sym = t.get("symbol")
        if sym in perps:
            try:
                rows.append({"symbol": sym,
                             "quoteVolume": float(t["quoteVolume"])})
            except (KeyError, ValueError, TypeError):
                pass
    rows.sort(key=lambda r: r["quoteVolume"], reverse=True)
    uni = rows[:TOP_N]
    p = os.path.join(BASE, "universe.json")
    with open(p, "w") as f:
        json.dump(uni, f, indent=1)
    print("universe.json: %d symbols (top by 24h quote volume)" % len(uni))
    print(", ".join(r["symbol"] for r in uni[:10]), "...")


if __name__ == "__main__":
    main()
