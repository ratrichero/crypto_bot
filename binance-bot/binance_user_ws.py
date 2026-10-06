#!/usr/bin/env python3
"""Binance USDⓈ-M private user-data WebSocket.

The class owns one listen-key connection and a low-frequency keepalive.  It is
only constructed by the live engine; dry-run/data-only never create a key.
Order/account events are passed to the engine callback and are not used as a
reason to issue blind REST retries.
"""
from __future__ import annotations

import json
import random
import threading
import time
from typing import Callable, Optional

import websocket

import binance_safety


URL = "wss://fstream.binance.com/private/ws/"


class BinanceUserDataWS:
    def __init__(
        self,
        listen_key: str,
        renew: Callable[[], str],
        on_event: Callable[[dict], None],
        log: Callable[[str], None],
        new_listen_key: Optional[Callable[[], str]] = None,
    ) -> None:
        self.listen_key = listen_key
        self.renew = renew
        self.new_listen_key = new_listen_key or renew
        self.on_event = on_event
        self.log = log
        self.fatal_error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._ws = None
        self._running = False
        self._failures = 0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running and not self._stop.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="binance-user-ws",
        )
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            daemon=True,
            name="binance-listen-key",
        )
        self._thread.start()
        self._keepalive_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _backoff(self) -> float:
        exponent = max(0, min(self._failures - 1, 6))
        return min(300.0, 5.0 * (2 ** exponent)) + random.uniform(0, 2)

    def _keepalive_loop(self) -> None:
        # Binance listen keys expire after 60 minutes. Renew at 30 minutes,
        # then retry transient keepalive failures every minute. Returning to
        # the 30-minute wait after one failure could let the key expire.
        while not self._stop.wait(30 * 60):
            while not self._stop.is_set():
                try:
                    new_key = self.renew()
                    if new_key and new_key != self.listen_key:
                        self.listen_key = new_key
                    self.log("USER WS listenKey renewed")
                    break
                except binance_safety.BinanceSafetyStop as exc:
                    self.fatal_error = str(exc)
                    self.log("USER WS safety stop: %s" % self.fatal_error)
                    self.stop()
                    return
                except Exception as exc:
                    # Do not spin on a keepalive error, but retry before the
                    # 60-minute Binance listen-key expiry deadline.
                    self.log("USER WS keepalive failed; retry in 60s: %s" %
                             binance_safety.redact_body(exc))
                    if self._stop.wait(60):
                        return

    def _run(self) -> None:
        while not self._stop.is_set():
            ws = None
            try:
                if not self.listen_key:
                    self.listen_key = self.new_listen_key()
                    if not self.listen_key:
                        raise RuntimeError("new Binance listenKey is empty")
                binance_safety.acquire("websocket_connect")
                ws = websocket.create_connection(
                    URL + self.listen_key,
                    timeout=12,
                )
                ws.settimeout(40)
                with self._lock:
                    self._ws = ws
                    self._running = True
                self.log("USER WS connected endpoint=/private")
                stable_since = time.monotonic()
                while not self._stop.is_set():
                    if time.monotonic() - stable_since >= 23 * 60 * 60:
                        self.log("USER WS reconnecting before 24h lifetime")
                        break
                    try:
                        msg = ws.recv()
                        if not msg:
                            break
                    except websocket.WebSocketTimeoutException:
                        # A private stream can be idle for minutes; an idle
                        # recv timeout is not a reconnect condition.
                        continue
                    except Exception as exc:
                        self.log("USER WS recv ended: %s" %
                                 binance_safety.redact_body(exc))
                        break
                    try:
                        event = json.loads(msg)
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("e") == "listenKeyExpired":
                        self.log("USER WS listenKey expired; obtaining a new key")
                        self.listen_key = ""
                        self.listen_key = self.new_listen_key()
                        if not self.listen_key:
                            raise RuntimeError("new Binance listenKey is empty")
                        break
                    try:
                        self.on_event(event)
                    except Exception as exc:
                        # An event handler must not kill the connection. The
                        # engine logs/reconciles malformed events separately.
                        self.log("USER WS event handler failed: %s" %
                                 binance_safety.redact_body(exc))
                    if time.monotonic() - stable_since >= 30:
                        self._failures = 0
                self._failures += 1
            except binance_safety.BinanceSafetyStop as exc:
                self.fatal_error = str(exc)
                self.log("USER WS safety stop: %s" % self.fatal_error)
                return
            except Exception as exc:
                status, api_code, _headers, body = binance_safety.classify_exception(exc)
                fatal = binance_safety.trip_for_exception(
                    exc, endpoint="websocket_user_connect", request_id=""
                )
                if fatal:
                    self.fatal_error = str(fatal)
                    self.log(
                        "USER WS rate-limit/ban stop status=%s api_code=%s body=%s"
                        % (status, api_code, binance_safety.redact_body(body))
                    )
                    return
                self._failures += 1
                delay = self._backoff()
                self.log(
                    "USER WS error status=%s api_code=%s body=%s; "
                    "reconnect in %.1fs failures=%d"
                    % (status, api_code, binance_safety.redact_body(body),
                       delay, self._failures)
                )
                self._stop.wait(delay)
            finally:
                with self._lock:
                    self._running = False
                    if self._ws is ws:
                        self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if not self._stop.is_set():
                delay = self._backoff()
                self.log("USER WS reconnect scheduled in %.1fs" % delay)
                self._stop.wait(delay)
        self.log("USER WS stopped")
