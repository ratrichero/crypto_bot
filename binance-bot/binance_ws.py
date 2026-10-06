"""Threaded Binance USDⓈ-M futures market websocket client.

Binance's routed market-stream endpoint is required after the 2026 endpoint
migration:
    wss://fstream.binance.com/market/stream?streams=btcusdt@miniTicker/...

The client uses one connection, bounded reconnect backoff, and a minimum
connection interval.  It never reconnects in a tight loop after a server
rejection.
"""
from __future__ import annotations

import json
import random
import threading
import time
from typing import Callable, Optional

import websocket

import binance_safety

URL = "wss://fstream.binance.com/market/stream?streams="


class BinanceWS:
    def __init__(self, symbols, log: Callable[[str], None]):
        self.symbols = [s.lower() for s in symbols]
        self.log = log
        self.prices = {}
        self.last_msg = 0.0
        self.last_connect = 0.0
        self.reconnect_failures = 0
        self.fatal_error: Optional[str] = None
        self._stop = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="binance-ws",
        )
        self._thread.start()

    def stop(self):
        self._stop = True

    def snapshot(self):
        with self._lock:
            return dict(self.prices)

    def healthy(self):
        with self._lock:
            return (time.time() - self.last_msg) < 90 and bool(self.prices)

    def _backoff(self) -> float:
        # 5, 10, 20, ... with a small jitter; cap below the operator's
        # circuit cooldown.  A successful message resets the counter.
        exponent = max(0, min(self.reconnect_failures - 1, 6))
        base = min(300.0, 5.0 * (2 ** exponent))
        return base + random.uniform(0.0, min(3.0, base * 0.1))

    def _run(self):
        # Binance market-stream names are lowercase even though the raw
        # exchangeInfo/universe ids used by the engine are uppercase.
        streams = "/".join(str(s).lower() + "@miniTicker" for s in self.symbols)
        url = URL + streams
        self.log(
            "WS configured endpoint=/market stream_count=%d "
            "reconnect_backoff=5..300s" % len(self.symbols)
        )
        while not self._stop:
            try:
                # The governor is shared with REST and prevents several bot
                # threads/process restarts from making a connection storm.
                binance_safety.acquire("websocket_connect")
                self.last_connect = time.time()
                ws = websocket.create_connection(url, timeout=12)
                ws.settimeout(40)
                self.log(
                    "WS connected endpoint=/market subscribed=%d miniTickers"
                    % len(self.symbols)
                )
                connected_at = time.monotonic()
                while not self._stop:
                    try:
                        msg = ws.recv()
                        if not msg:
                            break
                    except Exception as exc:
                        self.log("WS recv ended: %s" % binance_safety.redact_body(exc))
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
                    # Only reset backoff after a stable connection.  A
                    # connection that emits one frame and is immediately
                    # closed must still back off exponentially.
                    if time.monotonic() - connected_at >= 30:
                        self.reconnect_failures = 0
                try:
                    ws.close()
                except Exception:
                    pass
                if self._stop:
                    break
                self.reconnect_failures += 1
                delay = self._backoff()
                self.log(
                    "WS reconnect scheduled in %.1fs failures=%d "
                    "last_message_age=%.1fs"
                    % (delay, self.reconnect_failures,
                       time.time() - self.last_msg if self.last_msg else -1)
                )
                time.sleep(delay)
            except binance_safety.BinanceSafetyStop as exc:
                self.fatal_error = str(exc)
                self.log("WS safety stop: %s" % self.fatal_error)
                return
            except Exception as exc:
                status, api_code, headers, body = binance_safety.classify_exception(exc)
                fatal = binance_safety.trip_for_exception(
                    exc, endpoint="websocket_connect", request_id=""
                )
                if fatal:
                    self.fatal_error = str(fatal)
                    self.log(
                        "WS rate-limit/ban stop status=%s api_code=%s body=%s"
                        % (status, api_code, binance_safety.redact_body(body))
                    )
                    return
                self.reconnect_failures += 1
                delay = self._backoff()
                self.log(
                    "WS error status=%s api_code=%s body=%s; reconnect in %.1fs "
                    "failures=%d"
                    % (status, api_code, binance_safety.redact_body(body),
                       delay, self.reconnect_failures)
                )
                time.sleep(delay)
        self.log("WS stopped")
