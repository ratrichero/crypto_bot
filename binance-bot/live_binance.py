#!/usr/bin/env python3
"""Live trading engine for Binance USDT-M perpetual futures (ccxt binanceusdm).

Same role as live_okx.py for the OKX bot: drop-in engine with
open/close/unrealized, modes "dry_run" | "live" (default "dry_run").

DESIGN CHOICES (differences vs OKX worth knowing):
  1. HEDGE MODE. The strategy runs a TWO-WAY grid (long and short on the
     same symbol at once). Binance one-way mode would net those out, so the
     account is switched to hedge (dual-side) mode at startup and every
     order carries positionSide LONG/SHORT. Fails fast with a clear message
     if the account can't switch (e.g. open positions in one-way mode).
  2. Quantity, not contracts. Binance sizes orders in base asset
     (e.g. BTC), rounded DOWN to the symbol's stepSize; minQty and
     minNotional are enforced before sending.
  3. SL/TP like the OKX bot by default: the fast loop (~0.5s) watches WS
     prices and closes with opposite-side market orders. Optional
     exchange-side STOP_MARKET/TAKE_PROFIT_MARKET Algo Orders are guarded by
     config ``exchange_protection`` and remain disabled until testnet
     validation.

SAFETY:
  - Credentials ONLY from env BINANCE_API_KEY / BINANCE_API_SECRET.
    Never hardcoded, never in config, never printed/logged.
  - dry_run needs NO keys and makes ZERO authenticated calls (not even
    reads). Only public market data flows in the main loop.
  - live refuses to start without both env vars (fail closed).
  - Code paths contain NO withdraw endpoints, ever.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

import binance_safety

BASE = os.path.dirname(os.path.abspath(__file__))
KEY_ENV = "BINANCE_API_KEY"
SECRET_ENV = "BINANCE_API_SECRET"


# ---------------------------------------------------------------- credentials
def load_credentials():
    """Read API key/secret from environment. Fail closed with clear message."""
    key = os.environ.get(KEY_ENV)
    secret = os.environ.get(SECRET_ENV)
    if not key or not secret:
        raise RuntimeError(
            "Thieu credentials Binance: can bien moi truong %s va %s.\n"
            "Key phai co quyen Trade Futures, TAT quyen Withdraw, "
            "whitelist IP cua VPS. Khong bao gio dan key vao chat."
            % (KEY_ENV, SECRET_ENV))
    return key, secret


def _mask(s):
    s = str(s or "")
    return (s[:3] + "..." + s[-2:]) if len(s) > 6 else "***"


# ------------------------------------------------------------ contract sizing
def qty_for(notional, price, step_size, min_qty=0.0, min_notional=0.0):
    """Convert USD notional -> base-asset quantity (round DOWN to stepSize).

    Returns None when below minQty or minNotional (order would be rejected).
    Pure function: no network, easy to unit-test.
    """
    if not price or price <= 0 or not step_size or step_size <= 0:
        return None
    step = Decimal(str(step_size))
    raw = Decimal(str(notional)) / Decimal(str(price))
    q = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
    if q <= 0 or q < Decimal(str(min_qty)):
        return None
    if float(q) * float(price) < float(min_notional):
        return None
    return float(q)


# -------------------------------------------------------------- data-only
class DataOnlyEngine:
    """Market-data validation mode: no simulated or authenticated orders."""

    def __init__(self, log=None):
        self.ex = None
        self.log = log or (lambda m: print(m, flush=True))
        self.log(
            "BinanceEngine DATA_ONLY: chi doc WS/REST public va tinh tin hieu; "
            "bo qua moi open/close, khong goi API xac thuc."
        )

    def unrealized(self, prices):
        return 0.0


# ------------------------------------------------------------------- engine
class BinanceEngine:
    """Engine backed by real Binance USDT-M orders (or dry-run logging)."""

    def __init__(self, cfg, state, dry_run=False, log=None, symbols=None):
        self.cfg = cfg
        self.state = state
        self.dry_run = dry_run
        self._bot_symbols = {str(symbol).upper() for symbol in
                             (symbols or cfg.get("symbols", []))}
        self.log = log or (lambda m: print(m, flush=True))
        self._pid = state.get("_pid", 0)
        self._client_nonce = state.get("_client_nonce", 0)
        self._filters = {}   # raw symbol -> (step_size, min_qty, min_notional)
        self._price_ticks = {}
        self._markets = None
        self._symbol_map = {}  # raw Binance symbol -> CCXT unified symbol
        self._lev_done = set()
        self._dry_n = 0
        # Failure state is keyed by symbol/order action.  A transient API or
        # validation error must not be retried by the 0.5s market loop.
        self._action_failures = {}
        self._action_cooldowns = {}
        self._symbol_cooldowns = {}
        self._order_events = {}
        self._order_condition = threading.Condition()
        self._last_account_event = 0.0
        self._account_position_snapshot = {}
        self._last_reconcile = 0.0
        self._user_ws = None
        self._last_order_response = None
        self._cooldown_base = float(cfg.get("order_failure_cooldown_seconds", 30))
        self._cooldown_max = float(cfg.get("order_failure_cooldown_max_seconds", 900))
        # Postgres: ghi trade truc tiep, khong qua JSONL+sync.
        # Lazy connect; None = chua co / khong dung duoc.
        self._db_conn = None
        self._db_ok = False
        if dry_run:
            self.ex = None
            self.log("BinanceEngine DRY_RUN: khong can key, KHONG goi bat ky "
                     "API xac thuc nao. Chi log lenh SE dat.")
        else:
            key, secret = load_credentials()
            import ccxt
            self.ex = ccxt.binanceusdm({
                "apiKey": key,
                "secret": secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    "adjustForTimeDifference": True,
                    "recvWindow": int(cfg.get("recv_window_ms", 5000)),
                    # Tat warning khi fetchOpenOrders khong co symbol
                    # (ta chu dong chap nhan weight 40)
                    "fetchOpenOrders": {"warnWithoutSymbol": False},
                },
            })
            if cfg.get("use_testnet"):
                self.ex.set_sandbox_mode(True)
                self.log("WARNING: dung SANDBOX testnet, khong phai san that")
            self.log("BinanceEngine LIVE: da nap key %s (da che). "
                     "MOI LENH DAT LA TIEN THAT." % _mask(key))
            self._ensure_hedge_mode()
            self.refresh_equity()
            self._reconcile_startup()

    # ------------------------------------------------------------ helpers

    def _db_connect(self):
        """Lazy Postgres connection. Tra ve None neu khong dung duoc."""
        if self._db_conn is not None:
            return self._db_conn
        url = os.environ.get("DATABASE_URL")
        if not url:
            return None
        try:
            import psycopg
            conn = psycopg.connect(url)
            conn.autocommit = True
            self._db_conn = conn
            return conn
        except Exception as e:
            self.log("DB warning: khong ket noi duoc Postgres: %s" % e)
            return None

    def init_db(self):
        """Khoi tao DB 1 lan khi start bot. Khong fail neu DB loi."""
        conn = self._db_connect()
        if conn is not None:
            self._db_ok = True
            self.log("DB: da ket noi Postgres; trade se ghi truc tiep "
                     "vao binance_trades (JSONL van giu lam backup)")
        else:
            self.log("DB warning: khong co DATABASE_URL hoac ket noi that "
                     "bai; chi ghi JSONL")

    def _db_insert_trade(self, rec):
        """Ghi 1 trade da dong truc tiep vao Postgres.

        Dung ON CONFLICT DO NOTHING de tranh trung. Khong bao gio raise:
        neu DB loi thi log warning va tra False, JSONL van la backup.
        """
        try:
            conn = self._db_connect()
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO binance_trades
                       (id, symbol, side, tag, entry, exit, notional, pnl,
                        reason, closed_at, live, dry, close_ord)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               to_timestamp(%s), %s,%s,%s)
                       ON CONFLICT (id) DO NOTHING""",
                    (rec["id"], rec["symbol"], rec["side"], rec["tag"],
                     rec["entry"], rec["exit"], rec["notional"], rec["pnl"],
                     rec["reason"], rec["closed_at"],
                     rec.get("live", True), rec.get("dry", False),
                     rec.get("close_ord")),
                )
            self._db_ok = True
            return True
        except Exception as e:
            self.log("DB warning: insert trade #%s that bai: %s "
                     "(JSONL van co du lieu)" % (rec.get("id"), e))
            # Dong connection hong de lan sau ket noi lai
            try:
                if self._db_conn is not None:
                    self._db_conn.close()
            except Exception:
                pass
            self._db_conn = None
            self._db_ok = False
            return False
    def _next_id(self):
        self._pid += 1
        self.state["_pid"] = self._pid
        return self._pid

    def _fees(self, notional):
        return notional * self.cfg["fee_rate"]

    def used_margin(self):
        return sum(p["notional"] / self.cfg["leverage"]
                   for p in self.state["positions"])

    # ------------------------------------------------------- request safety
    def _private_call(self, endpoint, fn, *args, **kwargs):
        """Run one ccxt call through the shared IP/end-point governor."""
        weight = kwargs.pop("_weight", 1)
        return binance_safety.call_private(
            endpoint,
            fn,
            *args,
            exchange=self.ex,
            weight=weight,
            **kwargs,
        )

    def _new_client_order_id(self, symbol, side):
        self._client_nonce += 1
        self.state["_client_nonce"] = self._client_nonce
        # A persisted counter makes the id stable/auditable without using a
        # random retry token. Binance allows at most 36 restricted characters.
        raw = "".join(ch for ch in str(symbol).upper()
                      if ch.isalnum() or ch in "_-" )
        return "b%s%s%s" % (
            raw[:18],
            "L" if side in ("long", "buy") else "S",
            self._client_nonce,
        )[:36]

    def _find_order_by_client_id(self, symbol, client_order_id):
        """Reconcile an ambiguous network failure exactly once."""
        if self.dry_run or not client_order_id:
            return None
        params = {"symbol": symbol, "origClientOrderId": client_order_id}
        try:
            return self._private_call(
                "private:order_status",
                self.ex.fapiPrivateGetOrder,
                params,
            )
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            # -2013/order-not-found is the expected negative result. Other
            # errors are logged and the original create error is preserved.
            if "-2013" not in str(exc) and "order does not exist" not in str(exc).lower():
                self.log("WARNING reconcile clientOrderId %s failed: %s" %
                         (client_order_id, binance_safety.redact_body(exc)))
            return None

    @staticmethod
    def _order_status(order):
        """Return a normalized Binance/CCXT order status."""
        return str((order or {}).get("status")
                   or (order or {}).get("X") or "").strip().upper()

    @staticmethod
    def _order_average(order):
        """Extract an average fill price from raw Binance or CCXT fields."""
        order = order or {}
        for key in ("average", "avgPrice", "ap"):
            try:
                value = float(order.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        try:
            filled = float(order.get("filled") or order.get("executedQty") or 0)
            cost = float(order.get("cost") or 0)
        except (TypeError, ValueError):
            filled = cost = 0.0
        if filled > 0 and cost > 0:
            return cost / filled
        return None

    def _validate_order_result(self, order, context, expected_qty=None):
        """Reject a failed/partial result instead of creating false state.

        A GET order response always contains an order id, including for
        CANCELED, REJECTED and EXPIRED orders. Treating any such response as a
        successful MARKET fill would create a local position that Binance does
        not have. A partial MARKET result is also unsafe to model as the
        requested quantity, so it fails closed and requires reconciliation.
        """
        order = order or {}
        status = self._order_status(order)
        failed = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED",
                  "EXPIRED_IN_MATCH"}
        partial = {"PARTIALLY_FILLED", "PARTIAL"}
        try:
            filled = float(order.get("filled") or order.get("executedQty") or 0)
            requested = float(expected_qty or order.get("amount")
                              or order.get("origQty") or 0)
        except (TypeError, ValueError):
            filled = requested = 0.0
        is_partial_qty = (filled > 0 and requested > 0
                          and filled < requested - max(1e-12, requested * 1e-9))
        if status in failed and (filled <= 0 or not is_partial_qty):
            if filled > 0:
                self.state["halted"] = True
                self.state["halt_reason"] = (
                    "partial market order requires exchange reconciliation"
                )
            raise RuntimeError("%s returned terminal order status %s"
                               % (context, status))
        if status in partial or is_partial_qty:
            self.state["halted"] = True
            self.state["halt_reason"] = (
                "partial market order requires exchange reconciliation"
            )
            raise RuntimeError("%s returned partial order status %s; "
                               "manual reconciliation required"
                               % (context, status or "quantity"))

    def start_user_stream(self):
        """Start private ORDER_TRADE_UPDATE/ACCOUNT_UPDATE listener."""
        if self.dry_run or self._user_ws is not None:
            return
        listen_key = self._new_user_stream_key()
        from binance_user_ws import BinanceUserDataWS
        self._user_ws = BinanceUserDataWS(
            listen_key=listen_key,
            renew=self._renew_user_stream,
            new_listen_key=self._new_user_stream_key,
            on_event=self.on_user_event,
            log=self.log,
        )
        self._user_ws.start()
        self.log("USER WS started for order/account events")

    def _new_user_stream_key(self):
        response = self._private_call(
            "private:user_stream",
            self.ex.fapiPrivatePostListenKey,
            {},
        )
        listen_key = response.get("listenKey")
        if not listen_key:
            raise RuntimeError("Binance listenKey missing from response")
        return listen_key

    def _renew_user_stream(self):
        if self._user_ws is None:
            return ""
        response = self._private_call(
            "private:user_stream",
            self.ex.fapiPrivatePutListenKey,
            {"listenKey": self._user_ws.listen_key},
        )
        return response.get("listenKey") or self._user_ws.listen_key

    def stop_user_stream(self):
        if self._user_ws is not None:
            self._user_ws.stop()

    def user_stream_error(self):
        return self._user_ws.fatal_error if self._user_ws else None

    def on_user_event(self, event):
        kind = event.get("e")
        if kind == "ORDER_TRADE_UPDATE":
            order = event.get("o") or {}
            order_id = order.get("i")
            if order_id is not None:
                with self._order_condition:
                    self._order_events[str(order_id)] = order
                    # Do not let an event stream outage turn every historical
                    # order update into an unbounded process-memory leak.
                    if len(self._order_events) > 4096:
                        for _ in range(1024):
                            self._order_events.pop(next(iter(self._order_events)))
                    self._order_condition.notify_all()
            self.log("USER ORDER event symbol=%s order=%s status=%s exec=%s "
                     "positionSide=%s"
                     % (order.get("s"), order.get("i"), order.get("X"),
                        order.get("x"), order.get("ps")))
        elif kind == "ACCOUNT_UPDATE":
            self._last_account_event = time.time()
            for position in (event.get("a") or {}).get("P", []):
                symbol = position.get("s")
                side = str(position.get("ps") or "").lower()
                if symbol and side in ("long", "short"):
                    try:
                        amount = abs(float(position.get("pa", 0) or 0))
                    except (TypeError, ValueError):
                        continue
                    self._account_position_snapshot[(symbol, side)] = amount
            # Reconcile promptly on the next main-loop iteration, while the
            # periodic guard below also catches a dropped/private-stream gap.
            self._last_reconcile = 0.0

    def _wait_order_event(self, order_id, timeout):
        key = str(order_id)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._order_condition:
            while key not in self._order_events:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._order_condition.wait(timeout=remaining)
            event = dict(self._order_events[key])
            self._order_events.pop(key, None)
            return event

    def _action_key(self, action, symbol, side=None, tag=None, level=None):
        return ":".join(str(x) for x in (action, symbol, side or "-",
                                          tag or "-", level or "-"))

    def _cooldown_reason(self, key):
        until = self._action_cooldowns.get(key, 0.0)
        remaining = until - time.time()
        if remaining > 0:
            return "cooldown %.1fs" % remaining
        return None

    def _symbol_cooldown_reason(self, symbol):
        remaining = self._symbol_cooldowns.get(symbol, 0.0) - time.time()
        if remaining > 0:
            return "symbol cooldown %.1fs" % remaining
        return None

    def _mark_action_failure(self, key, error):
        count = self._action_failures.get(key, 0) + 1
        self._action_failures[key] = count
        delay = min(self._cooldown_max,
                    self._cooldown_base * (2 ** min(count - 1, 6)))
        now = time.time()
        self._action_cooldowns[key] = now + delay
        # A grid can expose several levels at once.  Also cool the whole
        # symbol so the next level cannot immediately issue another request.
        parts = key.split(":")
        symbol = parts[1] if len(parts) > 1 else ""
        if symbol:
            self._symbol_cooldowns[symbol] = max(
                self._symbol_cooldowns.get(symbol, 0.0), now + delay
            )
        self.log(
            "ORDER ACTION COOLDOWN key=%s seconds=%.1f failure=%d error=%s"
            % (key, delay, count, binance_safety.redact_body(error))
        )

    def _mark_action_success(self, key):
        self._action_failures.pop(key, None)
        self._action_cooldowns.pop(key, None)

    def _load_markets(self):
        if self._markets is None:
            self._markets = self._private_call(
                "private:exchange_info",
                self.ex.load_markets,
            )
        return self._markets

    def _ccxt_symbol(self, symbol):
        """Map raw Binance id (BTCUSDT) to CCXT id (BTC/USDT:USDT)."""
        if self.dry_run:
            return symbol
        if symbol in self._symbol_map:
            return self._symbol_map[symbol]
        markets = self._load_markets()
        for unified, market in markets.items():
            info = market.get("info") or {}
            market_id = market.get("id") or info.get("symbol")
            if str(market_id).upper() != str(symbol).upper():
                continue
            if market.get("swap") is False:
                continue
            settle = market.get("settle")
            if settle and str(settle).upper() != "USDT":
                continue
            self._symbol_map[symbol] = unified
            return unified
        self._symbol_map[symbol] = None
        return None

    def _raw_symbol(self, position):
        info = position.get("info") or {}
        raw = info.get("symbol") or position.get("symbol")
        if raw in self._symbol_map.values():
            for candidate, unified in self._symbol_map.items():
                if unified == raw:
                    return candidate
        if raw and "/" in str(raw) and not self.dry_run:
            markets = self._load_markets()
            market = markets.get(raw) or {}
            market_info = market.get("info") or {}
            return str(market_info.get("symbol") or market.get("id") or raw).upper()
        return str(raw).upper() if raw else raw

    def _ensure_hedge_mode(self):
        """Bat hedge (dual-side) mode cho tai khoan. Can cho grid 2 chieu."""
        try:
            self._private_call(
                "private:account",
                self.ex.set_position_mode,
                True,
            )
            self.log("Binance position mode: HEDGE (dual-side) OK")
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            msg = str(e)
            # -4059 "No need to change position side": tai khoan da o hedge mode
            if "-4059" in msg or "No need to change position side" in msg:
                self.log("Binance position mode: da o HEDGE tu truoc, OK")
                return
            raise RuntimeError(
                "Khong bat duoc hedge mode tren Binance: %s. "
                "Grid 2 chieu BAT BUOC hedge mode (one-way se net long/short "
                "cung symbol). Dong het vi the/lenh cho tren san roi chay lai."
                % e)

    def _filters_for(self, symbol):
        if symbol not in self._filters:
            markets = self._load_markets()
            ccxt_symbol = self._ccxt_symbol(symbol)
            if not ccxt_symbol or ccxt_symbol not in markets:
                # Coin bi delist / khong co tren futures -> bo qua, khong crash
                self._filters[symbol] = None
                return None
            m = markets[ccxt_symbol]
            lot = None
            market_lot = None
            minn = 0.0
            tick = 0.0
            for f in (m.get("info") or {}).get("filters", []):
                ft = f.get("filterType")
                if ft == "PRICE_FILTER":
                    tick = float(f.get("tickSize", 0) or 0)
                elif ft == "LOT_SIZE":
                    lot = (float(f["stepSize"]), float(f["minQty"]))
                elif ft == "MARKET_LOT_SIZE":
                    market_lot = (float(f["stepSize"]), float(f["minQty"]))
                elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                    minn = float(f.get("notional", f.get("minNotional", 0)))
            # All current bot orders are MARKET. Binance exposes a separate
            # MARKET_LOT_SIZE filter; fall back to LOT_SIZE when the market
            # filter is absent or publishes a zero step size (seen on some
            # contract metadata snapshots).
            selected = (market_lot if market_lot and market_lot[0] > 0
                        else lot)
            if not selected:
                raise RuntimeError("khong doc duoc LOT_SIZE cho %s" % symbol)
            self._filters[symbol] = (selected[0], selected[1], minn)
            self._price_ticks[symbol] = tick
        return self._filters[symbol]

    def _rounded_trigger_price(self, symbol, price):
        if price is None:
            return None
        if symbol not in self._price_ticks:
            self._filters_for(symbol)
        tick = self._price_ticks.get(symbol) or 0
        if not tick:
            return str(price)
        step = Decimal(str(tick))
        value = (Decimal(str(price)) / step).to_integral_value(
            rounding=ROUND_DOWN
        ) * step
        return format(value, "f")

    def _create_exchange_protection(self, pos):
        """Create optional Binance conditional orders for a live position."""
        if self.dry_run or not self.cfg.get("exchange_protection", False):
            return {}
        orders = {}
        order_side = "SELL" if pos["side"] == "long" else "BUY"
        position_side = "LONG" if pos["side"] == "long" else "SHORT"
        triggers = (
            ("sl", pos.get("sl"), "STOP_MARKET"),
            ("tp", pos.get("tp"), "TAKE_PROFIT_MARKET"),
        )
        try:
            for label, trigger, order_type in triggers:
                if trigger is None:
                    continue
                client_algo_id = self._new_client_order_id(
                    pos["symbol"], pos["side"]
                )[:36]
                pos["%s_client_algo_id" % label] = client_algo_id
                params = {
                    "algoType": "CONDITIONAL",
                    "symbol": pos["symbol"],
                    "side": order_side,
                    "positionSide": position_side,
                    "type": order_type,
                    "quantity": pos["qty"],
                    "triggerPrice": self._rounded_trigger_price(
                        pos["symbol"], trigger
                    ),
                    "workingType": self.cfg.get(
                        "protection_working_type", "MARK_PRICE"
                    ),
                    "clientAlgoId": client_algo_id,
                    # Current USD-M Algo Order docs specify lowercase string
                    # values "true"/"false" for this parameter.
                    "priceProtect": (
                        "true" if self.cfg.get("protection_price_protect", False)
                        else "false"
                    ),
                }
                try:
                    response = self._private_call(
                        "private:trade",
                        self.ex.fapiPrivatePostAlgoOrder,
                        params,
                    )
                    algo_id = response.get("algoId")
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception:
                    # The POST may have reached Binance before the transport
                    # failed. Reconcile by clientAlgoId before cleanup; an
                    # unknown Algo id must never become an invisible orphan.
                    algo_id = self._find_open_algo_by_client_id(client_algo_id)
                    if not algo_id:
                        raise
                if not algo_id:
                    # A successful HTTP response without an id is also
                    # ambiguous; query the idempotency key once.
                    algo_id = self._find_open_algo_by_client_id(client_algo_id)
                if not algo_id:
                    raise RuntimeError("Binance algo order missing algoId")
                orders[label] = algo_id
                # Persist each id on the position immediately. If creating a
                # later guard fails, cleanup can be incomplete; retaining the
                # id prevents an orphaned Algo Order from becoming invisible
                # to the subsequent fail-closed close path.
                pos["%s_algo_id" % label] = algo_id
            return orders
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception:
            # If the second protection order fails, remove the first one so
            # the position is not left with only half of its intended guard.
            # Use clientAlgoId too when a POST response was ambiguous.
            for label in ("sl", "tp"):
                algo_id = orders.get(label)
                client_algo_id = pos.get("%s_client_algo_id" % label)
                if not algo_id and not client_algo_id:
                    continue
                identifier = {"symbol": pos["symbol"]}
                if algo_id:
                    identifier["algoId"] = algo_id
                else:
                    identifier["clientAlgoId"] = client_algo_id
                try:
                    self._private_call(
                        "private:trade",
                        self.ex.fapiPrivateDeleteAlgoOrder,
                        identifier,
                    )
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as cancel_error:
                    self.log("CRITICAL protection cleanup failed guard=%s: %s"
                             % (algo_id or client_algo_id,
                                binance_safety.redact_body(cancel_error)))
            raise

    def _cancel_exchange_protection(self, pos):
        if self.dry_run:
            return
        if (not self.cfg.get("exchange_protection", False)
                and not pos.get("sl_algo_id") and not pos.get("tp_algo_id")):
            return
        absent_ids = []
        for key in ("sl_algo_id", "tp_algo_id"):
            label = key[:-8]  # sl_algo_id -> sl; tp_algo_id -> tp
            algo_id = pos.get(key)
            client_algo_id = pos.get("%s_client_algo_id" % label)
            if not algo_id and not client_algo_id:
                continue
            identifier = {"symbol": pos["symbol"]}
            if algo_id:
                identifier["algoId"] = algo_id
            else:
                # Binance supports clientAlgoId on Cancel Algo Order; this is
                # the recovery path when POST succeeded but its response/id
                # was lost in transit.
                identifier["clientAlgoId"] = client_algo_id
            try:
                self._private_call(
                    "private:trade",
                    self.ex.fapiPrivateDeleteAlgoOrder,
                    identifier,
                )
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                message = str(exc).lower()
                if ("-2011" in message or "-2013" in message
                        or "not found" in message
                        or "does not exist" in message):
                    self.log("INFO protection guard=%s already absent"
                             % (algo_id or client_algo_id))
                    absent_ids.append(algo_id or client_algo_id)
                    continue
                # Never send a market close while an exchange-side guard may
                # still be live: a later trigger could open a new Hedge leg.
                self.log("CRITICAL protection cancel failed guard=%s: %s"
                         % (algo_id or client_algo_id,
                            binance_safety.redact_body(exc)))
                raise RuntimeError(
                    "exchange protection cancellation uncertain for algo %s"
                    % (algo_id or client_algo_id)
                ) from exc

        if absent_ids:
            # "Algo not found" is also the response when a STOP/TP has
            # already triggered. Verify the Hedge leg still exists before
            # sending an opposite-side MARKET order; otherwise that order
            # would open a new reverse leg in Hedge Mode.
            try:
                rows = self._private_call(
                    "private:account",
                    self.ex.fetch_positions,
                    _weight=5,
                )
                actual = self._aggregate_positions(rows).get(
                    (pos["symbol"], pos["side"]), 0.0
                )
                expected = float(pos.get("qty", 0) or 0)
                step = 0.0
                try:
                    step = float((self._filters_for(pos["symbol"]) or (0,))[0] or 0)
                except Exception:
                    pass
                tolerance = max(step * 1.1, abs(expected) * 0.001, 1e-10)
                if actual <= 0 or abs(actual - expected) > tolerance:
                    raise RuntimeError(
                        "Hedge position changed while protection was absent "
                        "(expected=%s actual=%s)" % (expected, actual)
                    )
            except binance_safety.BinanceSafetyStop:
                raise
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    "cannot verify Hedge position after absent protection"
                ) from exc

    def _set_leverage(self, symbol):
        if symbol in self._lev_done:
            return
        if self.dry_run:
            self.log("DRY_RUN set-leverage %s lev=%s cross" %
                     (symbol, self.cfg["leverage"]))
        else:
            ccxt_symbol = self._ccxt_symbol(symbol)
            if not ccxt_symbol:
                raise RuntimeError("unknown_symbol: %s" % symbol)
            try:
                self._private_call(
                    "private:trade",
                    self.ex.set_margin_mode,
                    "cross",
                    ccxt_symbol,
                )
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as e:
                # -4046 "No need to change margin type": da dung che do -> bo qua
                msg = str(e)
                if "-4046" not in msg and "No need to change margin type" not in msg:
                    raise
            self._private_call(
                "private:trade",
                self.ex.set_leverage,
                self.cfg["leverage"],
                ccxt_symbol,
            )
        self._lev_done.add(symbol)

    def get_positions(self):
        """Vi the dang mo tren san (read-only)."""
        if self.dry_run:
            return []
        return self._private_call(
            "private:account",
            self.ex.fetch_positions,
            _weight=5,
        )

    def get_balance_usdt(self):
        """So du USDT futures (read-only)."""
        if self.dry_run:
            eq = self.state.get("equity", 0.0)
            return {"total": eq, "free": eq - self.used_margin()}
        bal = self._private_call(
            "private:account",
            self.ex.fetch_balance,
            _weight=5,
        )
        u = bal.get("USDT", {})
        return {"total": float(u.get("total", 0) or 0),
                "free": float(u.get("free", 0) or 0)}

    def refresh_equity(self):
        if self.dry_run:
            return
        try:
            wallet = self.get_balance_usdt()["total"]
            self.state["wallet_equity"] = wallet
            self.state["equity"] = wallet
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self.log("WARNING refresh_equity that bai: %s (giu equity cu)" % e)

    @staticmethod
    def _number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def mark_to_market_equity(self, mark_prices):
        """Return account equity including open-position PnL.

        Binance's futures account response normally exposes
        ``totalMarginBalance``. That is preferred because it already includes
        unrealized PnL. The fallback uses wallet balance plus the local
        mark-price estimate and is only used when the exchange payload lacks
        the aggregate fields.
        """
        if self.dry_run:
            return self.state.get("equity", 0.0) + self.unrealized(mark_prices)
        balance = self._private_call(
            "private:account",
            self.ex.fetch_balance,
            _weight=5,
        )
        info = balance.get("info") or {}
        for key in ("totalMarginBalance", "marginBalance"):
            value = self._number(info.get(key))
            if value is not None:
                return value
        wallet = self._number(info.get("totalWalletBalance"))
        unrealized = self._number(info.get("totalUnrealizedProfit"))
        if wallet is not None:
            return wallet + (unrealized or 0.0)
        usdt = balance.get("USDT", {}) or {}
        wallet = self._number(usdt.get("total"))
        if wallet is None:
            raise RuntimeError("Binance account equity missing from response")
        return wallet + self.unrealized(mark_prices)

    def _check_liquidation_buffer(self, rows):
        """Halt new risk when an exchange position nears liquidation."""
        minimum = float(self.cfg.get("min_liquidation_buffer_pct", 0.0))
        if minimum <= 0:
            return True
        for position in rows or []:
            info = position.get("info") or {}
            mark = self._number(
                position.get("markPrice") or info.get("markPrice")
            )
            liquidation = self._number(
                position.get("liquidationPrice")
                or info.get("liquidationPrice")
            )
            if not mark or not liquidation or mark <= 0 or liquidation <= 0:
                continue
            distance = abs(mark - liquidation) / mark
            if distance < minimum:
                self.state["halted"] = True
                self.state["halt_reason"] = (
                    "position liquidation buffer breached"
                )
                self.log(
                    "CRITICAL liquidation buffer symbol=%s mark=%s "
                    "liquidation=%s distance=%.2f%% minimum=%.2f%%"
                    % (self._raw_symbol(position), mark, liquidation,
                       distance * 100, minimum * 100)
                )
                return False
        return True

    def _aggregate_positions(self, rows):
        """Aggregate exchange/base quantities by raw symbol and LONG/SHORT."""
        result = {}
        for position in rows or []:
            amount = float(position.get("contracts", 0) or 0)
            if amount == 0:
                continue
            info = position.get("info") or {}
            position_side = info.get("positionSide") or position.get("side")
            position_side = str(position_side or "").lower()
            if position_side == "both":
                position_side = "long" if amount > 0 else "short"
            if position_side not in ("long", "short"):
                continue
            symbol = self._raw_symbol(position)
            if not symbol:
                continue
            key = (symbol, position_side)
            result[key] = result.get(key, 0.0) + abs(amount)
        return result

    def reconcile_positions(self, force=False, rows=None):
        """Compare local grid lots with exchange aggregate Hedge positions.

        Binance exposes one aggregate LONG and one aggregate SHORT position per
        symbol, while the strategy stores individual grid/scalp lots. Therefore
        this validates grouped quantities and never tries to import an unknown
        lot into local state automatically.
        """
        if self.dry_run:
            return True
        now = time.time()
        interval = float(self.cfg.get("reconcile_interval_seconds", 60))
        if not force and now - self._last_reconcile < interval:
            return True
        self._last_reconcile = now
        try:
            rows = self.get_positions() if rows is None else rows
            if not self._check_liquidation_buffer(rows):
                return False
            exchange = self._aggregate_positions(rows)
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.log("WARNING position reconciliation failed: %s" %
                     binance_safety.redact_body(exc))
            return False

        local = {}
        for position in self.state["positions"]:
            key = (position["symbol"], position["side"])
            local[key] = local.get(key, 0.0) + float(position.get("qty", 0) or 0)
        keys = set(local) | set(exchange)
        mismatches = []
        for key in sorted(keys):
            expected = local.get(key, 0.0)
            actual = exchange.get(key, 0.0)
            step = 0.0
            try:
                step = float((self._filters_for(key[0]) or (0,))[0] or 0)
            except Exception:
                pass
            tolerance = max(step * 1.1, abs(expected) * 0.001, 1e-10)
            if abs(expected - actual) > tolerance:
                mismatches.append((key, expected, actual))
        if mismatches:
            self.state["halted"] = True
            self.state["halt_reason"] = "exchange position reconciliation mismatch"
            self.state["halted_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                                    time.gmtime())
            self.log("CRITICAL POSITION RECONCILE mismatch=%s; halt new entries"
                     % mismatches)
            return False
        # Tu phuc hoi: neu truoc do halt vi mismatch ma gio het -> unhalt
        if (self.state.get("halted") and
                self.state.get("halt_reason") == "exchange position reconciliation mismatch"):
            self.state["halted"] = False
            self.state["halt_reason"] = None
            self.log("RECOVERY: position reconciliation OK - "
                     "tat ca vi the tren san khop voi state, tu dong unhalt. "
                     "Thoi gian halt: tu %s" %
                     self.state.get("halted_at", "unknown"))
        return True

    def detect_exchange_closed(self, mark_prices=None):
        """Phat hien vi the da bi dong tren san (TP/SL algo khop) ma bot chua biet.

        Xay ra khi bot dang halt hoac miss tin WS: algo order tren san khop,
        vi the bien mat nhung state van giu. Moi lot thuoc (symbol, side) ma
        san con ~0 duoc ghi trade uoc tinh (exit = TP/SL gan nhat, danh dau
        estimated=True), day vao DB, xoa khoi state. Tra ve list rec de main
        loop goi _record_close (JSONL + grid bookkeeping).

        Chi xu ly truong hop san ve ~0 hoan toan; dong mot phan thi de
        reconcile_positions halt nhu cu.
        """
        if self.dry_run:
            return []
        now = time.time()
        if now - getattr(self, "_last_detect_closed", 0) < 10:
            return []
        self._last_detect_closed = now
        try:
            rows = self._private_call("private:account",
                                      self.ex.fetch_positions, _weight=5)
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.log("WARNING detect_exchange_closed: khong lay duoc "
                     "positions: %s" % binance_safety.redact_body(exc))
            return []
        exchange = self._aggregate_positions(rows)
        marks = mark_prices or {}
        recs = []
        for pos in list(self.state.get("positions", [])):
            key = (pos.get("symbol"), pos.get("side"))
            local_qty = float(pos.get("qty", 0) or 0)
            if local_qty <= 0:
                continue
            actual = exchange.get(key, 0.0)
            try:
                step = float((self._filters_for(key[0]) or (0,))[0] or 0)
            except Exception:
                step = 0.0
            tolerance = max(step * 1.1, abs(local_qty) * 0.001, 1e-10)
            if abs(local_qty - actual) <= tolerance:
                continue  # khop voi san
            if actual > tolerance:
                continue  # dong mot phan -> de reconcile_positions halt nhu cu
            # San ve ~0 trong khi state van co -> da bi dong ngoai
            symbol, side = key
            entry = float(pos.get("entry", 0) or 0)
            tp = pos.get("tp")
            sl = pos.get("sl")
            mark = marks.get(symbol)
            try:
                mark = float(mark) if mark is not None else None
            except (TypeError, ValueError):
                mark = None
            fired, exit_px = "?", None
            if side == "long":
                if tp and mark is not None and mark >= tp:
                    fired, exit_px = "TP", tp
                elif sl and mark is not None and mark <= sl:
                    fired, exit_px = "SL", sl
                elif tp:
                    fired, exit_px = "TP?", tp
                elif sl:
                    fired, exit_px = "SL?", sl
            else:
                if tp and mark is not None and mark <= tp:
                    fired, exit_px = "TP", tp
                elif sl and mark is not None and mark >= sl:
                    fired, exit_px = "SL", sl
                elif tp:
                    fired, exit_px = "TP?", tp
                elif sl:
                    fired, exit_px = "SL?", sl
            # Thu lay gia khop that tu lich su giao dich san
            actual_exit = None
            try:
                trades = self._private_call(
                    "private:account", self.ex.fetch_my_trades, symbol, None,
                    5, _weight=5)
                # Tim lenh dong gan nhat (nguoc chieu voi side)
                close_side = "sell" if side == "long" else "buy"
                for t in reversed(trades or []):
                    if str(t.get("side", "")).lower() == close_side:
                        actual_exit = float(t.get("price") or 0)
                        if actual_exit > 0:
                            fired = "EXCHANGE"
                            break
            except Exception:
                pass
            if actual_exit:
                exit_px = actual_exit
            elif exit_px is None:
                exit_px = mark if mark else entry
            if side == "long":
                pnl = (exit_px - entry) * local_qty
            else:
                pnl = (entry - exit_px) * local_qty
            fee = self._fees(pos.get("notional", 0) or 0)
            net = pnl - fee
            self.state["equity"] = float(self.state.get("equity", 0) or 0) + net
            self.state["stats"]["fees"] = float(
                self.state["stats"].get("fees", 0) or 0) + fee
            self.state["stats"]["trades"] = int(
                self.state["stats"].get("trades", 0) or 0) + 1
            if net > 0:
                self.state["stats"]["wins"] = int(
                    self.state["stats"].get("wins", 0) or 0) + 1
            else:
                self.state["stats"]["losses"] = int(
                    self.state["stats"].get("losses", 0) or 0) + 1
            rec = {
                "id": pos["id"], "symbol": symbol, "side": side,
                "tag": pos.get("tag"), "entry": round(entry, 6),
                "exit": round(exit_px, 6),
                "notional": round(float(pos.get("notional", 0) or 0), 2),
                "pnl": round(net, 2), "reason": "CLOSED_ON_EXCHANGE",
                "closed_at": int(now),
                "live": True, "dry": self.dry_run,
                "estimated": fired != "EXCHANGE", "exit_fired": fired,
            }
            self.state["positions"] = [p for p in self.state["positions"]
                                       if p.get("id") != pos["id"]]
            self._db_insert_trade(rec)
            self.log("EXCHANGE_CLOSE #%s %s %s: khong con tren san, uoc tinh "
                     "dong @%s (%s) pnl=%+.2f [estimated]"
                     % (pos["id"], symbol, side, round(exit_px, 6), fired,
                        net))
            recs.append(rec)
        return recs

    def _reconcile_startup_open_orders(self):
        """Block resume when a previous normal order is still working."""
        if self.dry_run:
            return True
        query = getattr(self.ex, "fetch_open_orders", None)
        if query is None:
            self.state["halted"] = True
            self.state["halt_reason"] = "open order reconciliation unavailable"
            self.log("CRITICAL CCXT has no open order query; halt startup")
            return False
        try:
            # Binance charges weight 40 when symbol is omitted for the
            # all-symbol open-order query.
            orders = self._private_call("private:order_status", query,
                                        _weight=40)
            if orders is None:
                orders = []
            if not isinstance(orders, list):
                raise RuntimeError("unexpected open order response")
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.state["halted"] = True
            self.state["halt_reason"] = "open order reconciliation unavailable"
            self.log("CRITICAL open order reconciliation failed: %s" %
                     binance_safety.redact_body(exc))
            return False

        bot_symbols = set(getattr(self, "_bot_symbols", set()))
        working = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            raw_symbol = (order.get("symbol")
                          or (order.get("info") or {}).get("symbol")
                          or "")
            try:
                symbol = str(self._raw_symbol(order) or raw_symbol).upper()
            except Exception:
                symbol = str(raw_symbol).upper()
            if "/" in symbol:
                symbol = symbol.split("/")[0]
            if not bot_symbols or symbol in bot_symbols:
                working.append({
                    "symbol": symbol,
                    "id": order.get("id") or (order.get("info") or {}).get("orderId"),
                    "client": order.get("clientOrderId")
                    or (order.get("info") or {}).get("clientOrderId"),
                    "status": order.get("status")
                    or (order.get("info") or {}).get("status"),
                })
        if working:
            self.state["halted"] = True
            self.state["halt_reason"] = "unmanaged open exchange order"
            self.log("CRITICAL working exchange orders at startup=%s; "
                     "cancel/reconcile manually before resume" % working)
            return False
        return True

    def _fetch_open_algo_orders(self):
        """Return normalized open USD-M Algo Orders from the exchange."""
        query = getattr(self.ex, "fapiPrivateGetOpenAlgoOrders", None)
        if query is None:
            raise RuntimeError("CCXT has no open Algo Order query")
        # Binance charges weight 40 when symbol is omitted; keeping the
        # all-symbol startup scan in the governor prevents a false local
        # estimate from hiding the real IP budget.
        response = self._private_call("private:trade", query, {}, _weight=40)
        if isinstance(response, list):
            open_orders = response
        elif isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                open_orders = (data.get("orders") or data.get("list")
                               or data.get("algoOrders") or [])
            else:
                open_orders = (response.get("orders")
                               or response.get("list")
                               or data or [])
            if isinstance(open_orders, dict):
                open_orders = [open_orders]
        else:
            open_orders = []
        if not isinstance(open_orders, list):
            raise RuntimeError("unexpected open Algo Order response")
        return open_orders

    def _find_open_algo_by_client_id(self, client_algo_id):
        for order in self._fetch_open_algo_orders():
            if not isinstance(order, dict):
                continue
            candidate = (order.get("clientAlgoId")
                         or order.get("origClientAlgoId")
                         or (order.get("info") or {}).get("clientAlgoId"))
            if str(candidate or "") != str(client_algo_id):
                continue
            return (order.get("algoId") or order.get("algoOrderId")
                    or (order.get("info") or {}).get("algoId"))
        return None

    def _reconcile_exchange_protection(self, position_rows):
        """Verify persisted Algo guards before allowing a resumed live bot.

        A restart can happen after an Algo Order was accepted but before its
        id was persisted, or after a local close failed. Never guess which
        guard belongs to which position and never auto-cancel an unknown guard:
        query all open Futures Algo Orders and halt on any bot-symbol
        discrepancy so an operator can reconcile it on Binance first.
        """
        if self.dry_run:
            return True
        try:
            open_orders = self._fetch_open_algo_orders()
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.state["halted"] = True
            self.state["halt_reason"] = (
                "exchange protection reconciliation unavailable"
            )
            self.log("CRITICAL open Algo Order reconciliation failed: %s" %
                     binance_safety.redact_body(exc))
            return False

        expected = {}
        missing_required = []
        bot_symbols = set(getattr(self, "_bot_symbols", set()))
        for position in self.state.get("positions", []):
            symbol = str(position.get("symbol") or "").upper()
            if not symbol:
                continue
            bot_symbols.add(symbol)
            for label, price_key in (("sl", "sl"), ("tp", "tp")):
                algo_id = position.get("%s_algo_id" % label)
                if (self.cfg.get("exchange_protection", False)
                        and position.get(price_key) is not None and not algo_id):
                    missing_required.append("%s:%s" %
                                            (position.get("id"), label))
                if algo_id:
                    expected[str(algo_id)] = (position, label)

        exchange_symbols = set()
        for position in position_rows or []:
            symbol = self._raw_symbol(position)
            if symbol:
                exchange_symbols.add(str(symbol).upper())
        bot_symbols.update(exchange_symbols)
        seen = set()
        unknown = []
        guard_mismatch = []
        for order in open_orders:
            if not isinstance(order, dict):
                continue
            info = order.get("info") or {}
            algo_id = (order.get("algoId") or order.get("algoOrderId")
                       or order.get("i") or info.get("algoId"))
            if algo_id is not None:
                algo_id = str(algo_id)
                seen.add(algo_id)
            symbol = str(order.get("symbol") or order.get("s")
                         or info.get("symbol") or "").upper()
            if symbol in bot_symbols and algo_id not in expected:
                unknown.append((symbol, algo_id))
                continue
            if algo_id not in expected:
                continue
            position, label = expected[algo_id]
            wanted_symbol = str(position.get("symbol") or "").upper()
            wanted_side = ("LONG" if position.get("side") == "long"
                           else "SHORT")
            wanted_order_side = ("SELL" if position.get("side") == "long"
                                 else "BUY")
            order_side = str(order.get("positionSide")
                             or info.get("positionSide") or "").upper()
            order_direction = str(order.get("side") or info.get("side")
                                  or "").upper()
            order_type = str(order.get("orderType") or order.get("type")
                             or info.get("orderType") or info.get("type")
                             or "").upper()
            quantity = order.get("quantity") or order.get("origQty")
            try:
                quantity_mismatch = (
                    quantity is None
                    or abs(float(quantity) - float(position.get("qty", 0)))
                    > max(1e-10, float(position.get("qty", 0)) * 0.001)
                )
            except (TypeError, ValueError):
                quantity_mismatch = True
            wanted_type = "STOP_MARKET" if label == "sl" else "TAKE_PROFIT_MARKET"
            if (symbol != wanted_symbol or order_side != wanted_side
                    or order_direction != wanted_order_side
                    or order_type != wanted_type or quantity_mismatch):
                guard_mismatch.append((algo_id, symbol, order_side,
                                       order_direction, order_type))

        missing = sorted(set(expected) - seen)
        if missing or unknown or missing_required or guard_mismatch:
            self.state["halted"] = True
            self.state["halt_reason"] = (
                "exchange protection reconciliation mismatch"
            )
            self.state["halted_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                                    time.gmtime())
            self.log("CRITICAL Algo protection mismatch missing=%s "
                     "missing_required=%s unknown=%s guard_mismatch=%s; "
                     "halt until manually reconciled"
                     % (missing, missing_required, unknown, guard_mismatch))
            return False
        # Tu phuc hoi: neu truoc do halt vi protection mismatch ma gio het -> unhalt
        if (self.state.get("halted") and
                self.state.get("halt_reason") == "exchange protection reconciliation mismatch"):
            self.state["halted"] = False
            self.state["halt_reason"] = None
            self.log("RECOVERY: protection reconciliation OK - "
                     "tat ca SL/TP tren san khop voi state, tu dong unhalt. "
                     "Thoi gian halt: tu %s" %
                     self.state.get("halted_at", "unknown"))
        return True

    def cleanup_orphan_orders(self):
        """Quet va xoa lenh condition mo coi (khong con vi the tuong ung).
        Chay dinh ky moi 5 phut. Tra ve so lenh da xoa."""
        if self.dry_run:
            return 0
        try:
            # Lay vi the thuc te tren san
            positions = self._private_call(
                "private:account", self.ex.fetch_positions, _weight=5)
            live_symbols = set()
            for p in positions or []:
                amt = float(p.get("contracts", 0) or 0)
                if amt != 0:
                    sym = str(p.get("symbol", "")).split("/")[0] + "USDT"
                    live_symbols.add(sym.upper())
            # Lay algo orders
            algos = self._private_call(
                "private:trade", self.ex.fapiPrivateGetOpenAlgoOrders)
            if not isinstance(algos, list):
                return 0
            cleaned = 0
            for o in algos:
                sym = str(o.get("symbol", "")).upper()
                if sym not in live_symbols:
                    aid = o.get("algoId")
                    try:
                        self._private_call(
                            "private:trade",
                            self.ex.fapiPrivateDeleteAlgoOrder,
                            {"symbol": sym, "algoId": int(aid)})
                        self.log(f"CLEANUP: da xoa lenh mo coi {sym} algo={aid}")
                        cleaned += 1
                    except Exception as e:
                        self.log(f"CLEANUP loi khi xoa {aid}: {e}")
            return cleaned
        except Exception as e:
            self.log(f"CLEANUP loi: {e}")
            return 0

    def _reconcile_startup(self):
        """Remove paper ghosts, then validate aggregate exchange quantities."""
        try:
            rows = self.get_positions()
            exchange = self._aggregate_positions(rows)
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self.state["halted"] = True
            self.state["halt_reason"] = "exchange position reconciliation unavailable"
            self.log("CRITICAL khong doc duoc vi the san de doi chieu: %s" % e)
            return
        kept = [p for p in self.state["positions"] if p.get("live")]
        pruned = [p for p in self.state["positions"] if not p.get("live")]
        still = []
        exchange_keys = set(exchange)
        for p in kept:
            if (p["symbol"], p["side"]) in exchange_keys:
                still.append(p)
            else:
                pruned.append(p)
        if pruned:
            self.log("WARNING loai bo %d vi the khong ton tai tren san: ids=%s"
                     % (len(pruned), [p["id"] for p in pruned]))
        self.state["positions"] = still
        local_keys = {(p["symbol"], p["side"]) for p in still}
        for key in sorted(exchange_keys - local_keys):
            self.log("CRITICAL san co vi the %s qty=%s ma state khong quan ly "
                     "-> halt de doi chieu/close tay" % (key, exchange[key]))
            self.state["halted"] = True
            self.state["halt_reason"] = "unmanaged exchange position"
        self.reconcile_positions(force=True, rows=rows)
        self._reconcile_startup_open_orders()
        self._reconcile_exchange_protection(rows)

    def _place_market(self, symbol, side, qty, position_side,
                      reduce_only=False, ref_price=None):
        """side: 'buy'/'sell'. Returns (order_id, fill_price_or_None)."""
        params = {"positionSide": position_side}
        # Binance rejects reduceOnly together with positionSide LONG/SHORT
        # in Hedge Mode. Closing is expressed by the opposite side plus the
        # correct positionSide; reduceOnly is only valid for BOTH/one-way.
        if reduce_only and position_side == "BOTH":
            params["reduceOnly"] = True
        if self.dry_run:
            self._dry_n += 1
            oid = "dryrun-%d" % self._dry_n
            self.log("DRY_RUN dat lenh: %s %s qty=%s %s" %
                     (symbol, side, qty, json.dumps(params)))
            return oid, None
        ccxt_symbol = self._ccxt_symbol(symbol)
        if not ccxt_symbol:
            raise RuntimeError("unknown_symbol: %s" % symbol)
        client_order_id = self._new_client_order_id(symbol, side)
        params["newClientOrderId"] = client_order_id
        params["newOrderRespType"] = self.cfg.get("new_order_resp_type", "RESULT")
        self._last_order_response = None
        fn = (self.ex.create_market_buy_order if side == "buy"
              else self.ex.create_market_sell_order)
        try:
            od = self._private_call(
                "private:trade",
                fn,
                ccxt_symbol,
                qty,
                params,
            )
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            # A transport timeout can happen after Binance accepted the
            # order. Reconcile once by clientOrderId; never blindly submit a
            # second order.
            text = str(e).lower()
            uncertain = any(word in text for word in
                            ("timeout", "timed out", "network", "connection"))
            if uncertain:
                existing = self._find_order_by_client_id(symbol, client_order_id)
                if existing and existing.get("orderId") is not None:
                    self._validate_order_result(
                        existing,
                        "reconciled order %s" % client_order_id,
                        expected_qty=qty,
                    )
                    self.log("ORDER reconciled after transport failure "
                             "clientOrderId=%s orderId=%s"
                             % (client_order_id, existing.get("orderId")))
                    self._last_order_response = existing
                    return existing.get("orderId"), client_order_id
                # We cannot prove whether Binance accepted the MARKET order.
                # A cooldown alone would permit another entry while the first
                # request may still fill; halt until positions/open orders are
                # reconciled by the next startup/operator check.
                self.state["halted"] = True
                self.state["halt_reason"] = (
                    "ambiguous market order requires reconciliation"
                )
            raise RuntimeError("dat lenh %s %s that bai: %s"
                               % (symbol, side, e))
        self._validate_order_result(od, "created order", expected_qty=qty)
        self._last_order_response = od
        return (od.get("id") or od.get("orderId"),
                od.get("clientOrderId") or od.get("origClientOrderId")
                or client_order_id)

    def _fill_price(self, symbol, order_id, ref_price):
        if self.dry_run:
            return ref_price

        response = self._last_order_response or {}
        self._last_order_response = None
        self._validate_order_result(response, "order %s" % order_id)
        average = self._order_average(response)
        if average is not None:
            return average

        # Prefer the ordered private stream. This removes the old six-request
        # polling burst when ORDER_TRADE_UPDATE is healthy.
        if self._user_ws is not None and self._user_ws.running:
            event_order = self._wait_order_event(
                order_id,
                float(self.cfg.get("order_event_timeout_seconds", 8)),
            )
            if event_order:
                self._validate_order_result(
                    event_order,
                    "order event %s" % order_id,
                )
                average = self._order_average(event_order)
                if average is not None:
                    return average

        ccxt_symbol = self._ccxt_symbol(symbol)
        if not ccxt_symbol:
            raise RuntimeError("unknown_symbol: %s" % symbol)
        # REST is now a bounded fallback, not the normal order-status path.
        for attempt in range(2):
            try:
                od = self._private_call(
                    "private:order_status",
                    self.ex.fetch_order,
                    order_id,
                    ccxt_symbol,
                )
                self._validate_order_result(od, "order poll %s" % order_id)
                average = self._order_average(od)
                if average is not None:
                    return average
                if self._order_status(od) in ("CLOSED", "FILLED"):
                    break
            except binance_safety.BinanceSafetyStop:
                raise
            except RuntimeError:
                raise
            except Exception as e:
                self.log("WARNING poll order %s: %s" % (order_id, e))
            if attempt == 0:
                time.sleep(1)
        # A reference/mark price is not a fill price. Falling back to it would
        # create a ghost local position after an accepted-but-unfilled order.
        self.state["halted"] = True
        self.state["halt_reason"] = (
            "order fill reconciliation required"
        )
        raise RuntimeError("khong xac dinh duoc gia fill cho order %s; "
                           "khong ghi state" % order_id)

    # ----------------------------------------------------------------- open
    def open(self, symbol, side, notional, price, sl_pct, tp_pct, tag,
             level=None):
        """side: 'long' or 'short'. Returns (position, reason).

        Every failed symbol/action is put on exponential cooldown.  A
        Binance rate-limit/ban signal is deliberately re-raised so the outer
        bot can stop instead of trying the same action on the next tick.
        """
        key = self._action_key("open", symbol, side, tag, level)
        symbol_cooldown = self._symbol_cooldown_reason(symbol)
        if symbol_cooldown:
            return None, symbol_cooldown
        cooldown = self._cooldown_reason(key)
        if cooldown:
            return None, cooldown
        if self.state.get("halted"):
            return None, "halted: %s" % self.state.get("halt_reason", "")
        if len(self.state["positions"]) >= self.cfg.get("max_total_positions", 999):
            return None, "max_positions"
        try:
            margin_need = notional / self.cfg["leverage"]
            free = self.get_balance_usdt()["free"]
            if free < margin_need:
                self._mark_action_failure(key, "insufficient_margin")
                return None, "insufficient_margin"
            total_notional = sum(p["notional"]
                                 for p in self.state["positions"])
            risk_equity = float(
                self.state.get("mark_equity", self.state.get("equity", 0.0))
                or self.state.get("equity", 0.0)
                or 0.0
            )
            if (total_notional + notional
                    > risk_equity * self.cfg["risk"]["max_notional_mult"]):
                self._mark_action_failure(key, "exposure_cap")
                return None, "exposure_cap"

            self._set_leverage(symbol)
            if self.dry_run:
                qty = qty_for(notional, price, 0.000001)
            else:
                filters = self._filters_for(symbol)
                if filters is None:
                    self._mark_action_failure(key, "unknown_symbol")
                    return None, "unknown_symbol"
                step, minq, minn = filters
                qty = qty_for(notional, price, step, minq, minn)
                if qty is None:
                    self._mark_action_failure(key, "size_too_small")
                    return None, "size_too_small"
            bside = "buy" if side == "long" else "sell"
            ps = "LONG" if side == "long" else "SHORT"
            oid, client_order_id = self._place_market(
                symbol, bside, qty, ps, ref_price=price
            )
            entry = self._fill_price(symbol, oid, price)
            if side == "long":
                sl = entry * (1 - sl_pct) if sl_pct else None
                tp = entry * (1 + tp_pct) if tp_pct else None
            else:
                sl = entry * (1 + sl_pct) if sl_pct else None
                tp = entry * (1 - tp_pct) if tp_pct else None
            fee = self._fees(notional)
            pos = {
                "id": self._next_id(),
                "symbol": symbol, "side": side, "qty": qty, "entry": entry,
                "notional": notional, "sl": sl, "tp": tp, "tag": tag,
                "level": level, "opened_at": int(time.time()), "fee_entry": fee,
                "live": True, "dry": self.dry_run, "ord_id": oid,
                "client_order_id": client_order_id,
            }
            self.state["equity"] -= fee
            self.state["stats"]["fees"] += fee
            self.state["positions"].append(pos)
            if not self.dry_run and self.cfg.get("exchange_protection", False):
                try:
                    protection = self._create_exchange_protection(pos)
                    pos["sl_algo_id"] = protection.get("sl")
                    pos["tp_algo_id"] = protection.get("tp")
                    pos["protection_status"] = "armed"
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as protection_error:
                    # Retry dat protection: moi 10s, toi da 2 phut (12 lan).
                    # Neu van that bai -> dong vi the de tranh mat kiem soat.
                    pos["protection_status"] = "retrying"
                    pos["protection_error"] = binance_safety.redact_body(
                        protection_error
                    )
                    pos["protection_retry_at"] = time.time() + 10
                    pos["protection_deadline"] = time.time() + 120
                    self.log("WARNING protection that bai, se retry sau 10s: %s" %
                             binance_safety.redact_body(protection_error))
            self._mark_action_success(key)
            self.log("%s OPEN #%d %s %s entry=%s sl=%s tp=%s ord=%s "
                     "protection=%s"
                     % ("DRY_RUN" if self.dry_run else "LIVE",
                        pos["id"], symbol, side, entry, sl, tp, oid,
                        pos.get("protection_status", "disabled")))
            return pos, "ok"
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self._mark_action_failure(key, e)
            return None, "action_failed: %s" % e

    def retry_protection(self):
        """Retry dat SL/TP cho cac vi the dang 'retrying' hoac chua co protection.
        Tra ve True neu co thay doi."""
        now = time.time()
        changed = False
        for pos in self.state.get("positions", []):
            status = pos.get("protection_status")
            # Backfill: vi the cu chua co protection -> dat ngay
            if status is None and self.cfg.get("exchange_protection", False):
                if pos.get("sl") or pos.get("tp"):
                    log_msg = f"Backfill protection cho #{pos['id']} {pos['symbol']}"
                    self.log(log_msg)
                    try:
                        protection = self._create_exchange_protection(pos)
                        pos["sl_algo_id"] = protection.get("sl")
                        pos["tp_algo_id"] = protection.get("tp")
                        pos["protection_status"] = "armed"
                        changed = True
                        self.log(f"Backfill thanh cong #{pos['id']}")
                    except Exception as e:
                        pos["protection_status"] = "retrying"
                        pos["protection_retry_at"] = now + 10
                        pos["protection_deadline"] = now + 120
                        changed = True
                continue
            if status != "retrying":
                continue
            if now < pos.get("protection_retry_at", 0):
                continue
            if now >= pos.get("protection_deadline", 0):
                # Het 2 phut van that bai -> dong vi the
                self.log("CRITICAL protection retry het 2 phut, dong vi the #%s %s" %
                         (pos["id"], pos["symbol"]))
                try:
                    self.close(pos, pos["entry"], "PROTECTION_FAILED")
                    changed = True
                except Exception as e:
                    self.log("CRITICAL khong dong duoc vi the khong protection: %s" % e)
                    self.state["halted"] = True
                    self.state["halt_reason"] = "unprotected position cannot close"
                continue
            # Thu dat lai protection
            try:
                protection = self._create_exchange_protection(pos)
                pos["sl_algo_id"] = protection.get("sl")
                pos["tp_algo_id"] = protection.get("tp")
                pos["protection_status"] = "armed"
                pos.pop("protection_retry_at", None)
                pos.pop("protection_deadline", None)
                self.log("Protection retry thanh cong cho #%s %s" %
                         (pos["id"], pos["symbol"]))
                changed = True
            except Exception as e:
                pos["protection_retry_at"] = now + 10
                self.log("Protection retry that bai, thu lai sau 10s: %s" %
                         binance_safety.redact_body(e))
                changed = True
        return changed

    # ---------------------------------------------------------------- close
    def close(self, pos, price, reason):
        """Dong bang market Hedge order, with per-position cooldown."""
        symbol = pos["symbol"]
        side = pos["side"]
        key = self._action_key("close", symbol, side, pos.get("tag"),
                               pos.get("id"))
        cooldown = self._cooldown_reason(key)
        if cooldown:
            return None
        try:
            self._cancel_exchange_protection(pos)
            bside = "sell" if side == "long" else "buy"
            ps = "LONG" if side == "long" else "SHORT"
            if self.dry_run:
                qty = pos["qty"]
            else:
                filters = self._filters_for(symbol)
                if filters is None:
                    raise RuntimeError("unknown_symbol: %s" % symbol)
                step, minq, minn = filters
                qty = qty_for(pos["qty"] * price, price, step, minq, minn)
                if qty is None:
                    qty = minq  # vi the qua nho -> thu dong voi minQty
            oid, client_order_id = self._place_market(
                symbol, bside, qty, ps, reduce_only=True, ref_price=price
            )
            ex = self._fill_price(symbol, oid, price)
            if side == "long":
                pnl = (ex - pos["entry"]) * pos["qty"]
            else:
                pnl = (pos["entry"] - ex) * pos["qty"]
            fee = self._fees(pos["notional"])
            net = pnl - fee
            self.state["equity"] += net
            self.state["stats"]["fees"] += fee
            self.state["stats"]["trades"] += 1
            if net > 0:
                self.state["stats"]["wins"] += 1
            else:
                self.state["stats"]["losses"] += 1
            # Verify vi the da dong that tren san truoc khi xoa khoi state.
            # Neu partial fill -> giu lai de retry, khong danh dau da dong.
            if not self.dry_run:
                import time as _t
                _t.sleep(2)
                actual = self._aggregate_positions(
                    self._private_call("private:account",
                                       self.ex.fetch_positions, _weight=5)
                ).get((symbol, side), 0)
                # Cho phep sai so nho do lam tron
                if abs(actual) > pos["qty"] * 0.01:
                    raise RuntimeError(
                        f"close partial: san con {actual}, bot nghi {pos['qty']} "
                        f"-> se retry")
            rec = {
                "id": pos["id"], "symbol": symbol, "side": side,
                "tag": pos["tag"], "entry": round(pos["entry"], 6),
                "exit": round(ex, 6), "notional": round(pos["notional"], 2),
                "pnl": round(net, 2), "reason": reason,
                "closed_at": int(time.time()),
                "live": True, "dry": self.dry_run, "close_ord": oid,
                "close_client_order_id": client_order_id,
            }
            self.state["positions"] = [p for p in self.state["positions"]
                                        if p["id"] != pos["id"]]
            self._mark_action_success(key)
            self.log("%s CLOSE #%d %s %s %s pnl=%+.2f ord=%s" %
                     ("DRY_RUN" if self.dry_run else "LIVE",
                      rec["id"], symbol, side, reason, net, oid))
            # Ghi truc tiep vao Postgres (khong block neu DB loi;
            # JSONL van duoc ghi o _record_close lam backup).
            self._db_insert_trade(rec)
            if not self.dry_run:
                self.refresh_equity()
            return rec
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            # A failed close can leave exchange exposure while local state
            # still says the position is open. Do not merely cooldown and let
            # a future risk day resume entries around that unknown exposure.
            self.state["halted"] = True
            self.state["halt_reason"] = "close action requires reconciliation"
            self._mark_action_failure(key, e)
            return None

    def unrealized(self, prices):
        u = 0.0
        for p in self.state["positions"]:
            px = prices.get(p["symbol"])
            if px is None:
                continue
            if p["side"] == "long":
                u += (px - p["entry"]) * p["qty"]
            else:
                u += (p["entry"] - px) * p["qty"]
        return u
