"""Threaded Binance USDT-M futures websocket client (public, no auth).

One combined stream for all symbols:
    wss://fstream.binance.com/stream?streams=btcusdt@miniTicker/ethusdt@miniTicker/...
miniTicker payload: {"s":"BTCUSDT","c":"67123.45",...} -> prices[symbol] = close.
"""
import json
import threading
import time

import websocket

URL = "wss://fstream.binance.com/stream?streams="


class BinanceWS:
    def __init__(self, symbols, log):
        self.symbols = [s.lower() for s in symbols]
        self.log = log
        self.prices = {}
        self.last_msg = 0.0
        self._stop = False
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="binance-ws").start()

    def stop(self):
        self._stop = True

    def healthy(self):
        return (time.time() - self.last_msg) < 90 and bool(self.prices)

    def _run(self):
        streams = "/".join(s + "@miniTicker" for s in self.symbols)
        url = URL + streams
        while not self._stop:
            try:
                ws = websocket.create_connection(url, timeout=12)
                ws.settimeout(40)
                self.log(f"WS connected, subscribed {len(self.symbols)} miniTickers")
                while not self._stop:
                    try:
                        msg = ws.recv()
                    except Exception:
                        break
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    data = d.get("data") or {}
                    if data.get("e") != "24hrMiniTicker":
                        continue
                    try:
                        px = float(data["c"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    with self._lock:
                        self.prices[data["s"]] = px
                        self.last_msg = time.time()
            except Exception as e:
                self.log(f"WS error: {e}")
            time.sleep(5)
        self.log("WS stopped")
