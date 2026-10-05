"""Threaded OKX public websocket client for live tickers (no auth)."""
import json
import threading
import time

import websocket

URL = "wss://ws.okx.com:8443/ws/v5/public"


class TickerWS:
    def __init__(self, inst_ids, log):
        self.insts = inst_ids
        self.log = log
        self.prices = {}
        self.last_msg = 0.0
        self._stop = False
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="okx-ws").start()

    def stop(self):
        self._stop = True

    def healthy(self):
        return (time.time() - self.last_msg) < 90 and bool(self.prices)

    def _run(self):
        while not self._stop:
            try:
                ws = websocket.create_connection(URL, timeout=12)
                args = [{"channel": "tickers", "instId": i}
                        for i in self.insts]
                ws.send(json.dumps({"op": "subscribe", "args": args}))
                ws.settimeout(35)
                self.log(f"WS connected, subscribed {len(args)} tickers")
                while not self._stop:
                    try:
                        msg = ws.recv()
                    except Exception:
                        break
                    if msg == "ping":
                        try:
                            ws.send("pong")
                        except Exception:
                            break
                        continue
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    if "event" in d:
                        continue
                    arg = d.get("arg", {})
                    data = d.get("data") or []
                    if arg.get("channel") == "tickers" and data:
                        try:
                            last = float(data[0]["last"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        with self._lock:
                            self.prices[arg["instId"]] = last
                            self.last_msg = time.time()
            except Exception as e:
                self.log(f"WS error: {e}")
            time.sleep(5)
        self.log("WS stopped")
