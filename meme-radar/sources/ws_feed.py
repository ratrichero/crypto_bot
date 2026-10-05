"""Kenh real-time: websocket logsSubscribe Helius -> hang doi tin hieu BUY.

Moi vi duoc subscribe rieng (Helius gioi han 1 address/subscription).
Nhan notification -> fetch transaction -> parse_buy -> day vao queue.
Tu reconnect khi rot mang. Poll RPC trong radar.py van chay lam fallback.
"""
import asyncio
import json
import queue
import threading
import time

import websockets

from .helius import Helius, parse_buy, parse_sell, load_wallets, redact_key


class WSFeed(threading.Thread):
    def __init__(self, cfg, base, out_q, log, stop_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.base = base
        self.q = out_q
        self.log = log
        self.stop_event = stop_event

    def run(self):
        try:
            asyncio.run(self._loop())
        except Exception as e:
            self.log(f"ws feed thread died: {e}")

    async def _loop(self):
        backoff = 5
        while not self.stop_event.is_set():
            try:
                await self._run_once()
                backoff = 5
            except Exception as e:
                if self.stop_event.is_set():
                    return
                self.log(f"ws feed reconnect sau {backoff}s ({redact_key(str(e))[:100]})")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _run_once(self):
        key = self.cfg.get("helius_api_key")
        if not key:
            self.log("ws feed: thieu key, khong chay")
            return
        wallets = load_wallets(self.cfg, self.base)
        if not wallets:
            self.log("ws feed: chua co vi theo doi")
            return
        h = Helius(key, timeout=30)
        url = f"wss://mainnet.helius-rpc.com/?api-key={key}"
        sub2wallet = {}
        async with websockets.connect(url, max_size=8 * 1024 * 1024,
                                      ping_interval=20, ping_timeout=20) as ws:
            # subscribe tung vi, map sub_id -> wallet qua ack
            for i, w in enumerate(wallets):
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1000 + i, "method": "logsSubscribe",
                    "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
            # doc ack cho den khi du sub (xen ke notification thi xu ly luon)
            pending = len(wallets)
            self.log(f"ws feed: dang subscribe {pending} vi...")
            while pending > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                except asyncio.TimeoutError:
                    raise RuntimeError("ws subscribe timeout")
                d = json.loads(raw)
                if "id" in d and "result" in d:
                    idx = d["id"] - 1000
                    if 0 <= idx < len(wallets):
                        sub2wallet[d["result"]] = wallets[idx]
                        pending -= 1
                elif d.get("method") == "logsNotification":
                    await self._handle(d, sub2wallet, h)
            self.log("ws feed: LIVE - nhan tin real-time")
            async for raw in ws:
                if self.stop_event.is_set():
                    return
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("method") == "logsNotification":
                    await self._handle(d, sub2wallet, h)

    async def _handle(self, d, sub2wallet, h):
        try:
            params = d.get("params") or {}
            val = params.get("result") or {}
            sig = val.get("value", {}).get("signature")
            if not sig or val.get("value", {}).get("err"):
                return
            wallet = sub2wallet.get(params.get("subscription"))
            if not wallet:
                return
            tx = await asyncio.to_thread(
                h.rpc, "getTransaction",
                [sig, {"encoding": "jsonParsed",
                       "maxSupportedTransactionVersion": 1,
                       "commitment": "confirmed"}])
            if not tx:
                return
            b = parse_buy(tx, wallet, self.cfg.get("min_sol_spent", 0.3))
            if b:
                self.q.put({
                    "tid": sig, "wallet": wallet, "token": b["mint"],
                    "sol_spent": b["sol_spent"],
                    "ts": int(time.time()), "src": "ws", "side": "buy",
                })
                self.log(f"ws BUY: {wallet[:6]}.. {b['sol_spent']:.2f} SOL -> "
                         f"{b['mint'][:10]}..")
                return
            sl = parse_sell(tx, wallet, self.cfg.get("min_sol_spent", 0.3))
            if sl:
                self.q.put({
                    "tid": sig, "wallet": wallet, "token": sl["mint"],
                    "sol_amount": sl["sol_received"],
                    "ts": int(time.time()), "src": "ws", "side": "sell",
                })
                self.log(f"ws SELL: {wallet[:6]}.. {sl['sol_received']:.2f} SOL <- "
                         f"{sl['mint'][:10]}..")
        except Exception as e:
            self.log(f"ws handle loi: {redact_key(str(e))[:100]}")
