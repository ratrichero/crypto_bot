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
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

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


# ------------------------------------------------------------ Postgres
DB_INSERT_SQL = """INSERT INTO binance_trades
   (id, symbol, side, tag, entry, exit, notional, pnl,
    reason, closed_at, live, dry, close_ord)
   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
           to_timestamp(%s), %s,%s,%s)
   ON CONFLICT (id) DO NOTHING"""
DB_EXT_COLS = ("pnl_gross", "fee_entry", "fee_exit", "fee_estimated",
               "estimated", "exit_source")
DB_MIGRATE_SQL = [
    "ALTER TABLE binance_trades ADD COLUMN IF NOT EXISTS %s %s" % (c, t)
    for c, t in (("pnl_gross", "DOUBLE PRECISION"),
                 ("fee_entry", "DOUBLE PRECISION"),
                 ("fee_exit", "DOUBLE PRECISION"),
                 ("fee_estimated", "BOOLEAN"), ("estimated", "BOOLEAN"),
                 ("exit_source", "TEXT"))]
DB_INSERT_EXT_SQL = """INSERT INTO binance_trades
   (id, symbol, side, tag, entry, exit, notional, pnl,
    reason, closed_at, live, dry, close_ord, pnl_gross, fee_entry,
    fee_exit, fee_estimated, estimated, exit_source)
   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
           to_timestamp(%s), %s,%s,%s, %s,%s,%s,%s,%s,%s)
   ON CONFLICT (id) DO UPDATE SET """ + ", ".join(
    "%s = COALESCE(binance_trades.%s, EXCLUDED.%s)" % (c, c, c)
    for c in DB_EXT_COLS)


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


def normalize_qty(qty, step_size):
    """Lam tron qty ve boi so GAN NHAT cua stepSize (khu sai so float).

    Dung cho qty da hop le (lot da mo / vi the san), khong dung de size lenh
    moi (lenh moi lam tron XUONG bang qty_for)."""
    if not step_size or float(step_size) <= 0:
        return float(qty or 0)
    step = Decimal(str(step_size))
    q = (Decimal(str(qty or 0)) / step).to_integral_value(
        rounding=ROUND_HALF_UP) * step
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
class GuardAlreadyFilled(Exception):
    """close() found that Binance's own TP/SL already closed the lot."""

    def __init__(self, label, outcome):
        super().__init__("guard %s already filled" % label)
        self.label = label
        self.outcome = outcome


class GuardInFlight(Exception):
    """A guard is triggering right now; its result is not known yet."""


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
        # ALGO_UPDATE events keyed "SYMBOL:algoId" (algoId is per symbol).
        self._algo_events = {}
        self._protection_sync_due = True
        self._last_protection_sync = 0.0
        self._trigger_seen = {}
        self._pending_close_recs = []
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

    def _db_ext_ready(self, cur):
        """ALTER idempotent cac cot phi/nguon gia; loi -> insert cot cu,
        thu lai sau 10 phut (khong bao gio lam mat trade)."""
        if getattr(self, "_db_ext", False):
            return True
        if time.time() < getattr(self, "_db_ext_retry_at", 0.0):
            return False
        try:
            for sql in DB_MIGRATE_SQL:
                cur.execute(sql)
            self._db_ext = True
        except Exception as e:
            self._db_ext_retry_at = time.time() + 600
            self.log("DB warning: migrate binance_trades that bai: %s "
                     "-> insert cot cu" % e)
            try:
                cur.connection.rollback()
            except Exception:
                pass
        return getattr(self, "_db_ext", False)

    def _db_insert_trade(self, rec):
        """Ghi 1 trade da dong truc tiep vao Postgres.

        Dung ON CONFLICT DO NOTHING de tranh trung. Khong bao gio raise:
        neu DB loi thi log warning va tra False, JSONL van la backup.
        """
        try:
            conn = self._db_connect()
            if conn is None:
                return False
            base = (rec["id"], rec["symbol"], rec["side"], rec["tag"],
                    rec["entry"], rec["exit"], rec["notional"], rec["pnl"],
                    rec["reason"], rec["closed_at"],
                    rec.get("live", True), rec.get("dry", False),
                    rec.get("close_ord"))
            with conn.cursor() as cur:
                if self._db_ext_ready(cur):
                    est = rec.get("estimated")
                    cur.execute(DB_INSERT_EXT_SQL, base + (
                        rec.get("pnl_gross"), rec.get("fee_entry"),
                        rec.get("fee_exit"), rec.get("fee_estimated"),
                        None if est is None else bool(est),
                        rec.get("exit_source")))
                else:
                    cur.execute(DB_INSERT_SQL, base)
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

    _USD_ASSETS = ("USDT", "USDC", "BUSD", "FDUSD")

    def _order_fill_summary(self, symbol, order_id):
        """Fill THAT cua 1 order tu userTrades (source of truth).

        Loc theo orderId, KHONG truyen since: ccxt tu dat endTime =
        since + 7 ngay khi since cu hon 7 ngay (lot mo lau) -> mat trade
        dong. Order o day luon vua khop nen nam trong 7 ngay mac dinh.

        Tra ve {"qty", "avg", "commission"} hoac None khi khong doc duoc,
        khong co trade, hoac phi tra bang tai san khong phai USD (vd BNB) ->
        caller dung phi uoc tinh va danh dau fee_estimated.
        """
        if self.dry_run or not order_id:
            return None
        try:
            ccxt_symbol = self._ccxt_symbol(symbol) or symbol
            trades = self._private_call(
                "private:account", self.ex.fetch_my_trades, ccxt_symbol,
                None, 100, {"orderId": str(order_id)}, _weight=5) or []
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.log("WARNING userTrades order %s %s: %s" %
                     (symbol, order_id, binance_safety.redact_body(exc)))
            return None
        qty = cost = commission = 0.0
        for trade in trades:
            info = trade.get("info") or {}
            if str(trade.get("order") or info.get("orderId") or "") != str(
                    order_id):
                continue
            fee = trade.get("fee") or {}
            asset = str(fee.get("currency") or info.get("commissionAsset")
                        or "").upper()
            try:
                q = float(trade.get("amount") or info.get("qty") or 0)
                px = float(trade.get("price") or info.get("price") or 0)
                c = float(fee.get("cost") if fee.get("cost") is not None
                          else info.get("commission") or 0)
            except (TypeError, ValueError):
                return None
            if c and asset not in self._USD_ASSETS:
                self.log("WARNING order %s %s: phi tra bang %s -> dung phi "
                         "uoc tinh" % (symbol, order_id, asset or "?"))
                return None
            qty += q
            cost += q * px
            commission += abs(c)
        if qty <= 0:
            return None
        return {"qty": qty, "avg": cost / qty, "commission": commission}

    def _book_exit(self, pos, exit_px, close_order_id=None, order_qty=None):
        """Tinh so khi dong 1 lot; MOI duong dong (bot/algo/san) dung chung.

        pnl (record) = gop - phi dong THAT - phi mo THAT (khop
        binance_trades.pnl 'P&L rong'). Phi lay tu userTrades; khong doc duoc
        -> fee_rate x notional thuc te, fee_estimated=True. Lenh dong chung
        nhieu lot (order_qty > lot) -> chia phi theo ty le qty.
        """
        side = pos.get("side")
        lot_qty = float(pos.get("qty", 0) or 0)
        entry = float(pos.get("entry", 0) or 0)
        gross = ((exit_px - entry) if side == "long" else (entry - exit_px)) \
            * lot_qty
        if close_order_id:
            booked = getattr(self, "_booked_close_orders", None)
            if booked is None:
                booked = self._booked_close_orders = set()
            booked.add(str(close_order_id))
        summary = self._order_fill_summary(pos.get("symbol"), close_order_id)
        if summary is not None:
            share = 1.0
            filled = summary["qty"]
            if filled > 0 and filled > lot_qty:
                share = lot_qty / filled
            fee_exit = summary["commission"] * share
            exit_estimated = False
        else:
            fee_exit = self._fees(abs(exit_px) * lot_qty)
            exit_estimated = not self.dry_run
        fee_entry = float(pos.get("fee_entry", 0) or 0)
        return {
            "gross": gross, "fee_entry": fee_entry, "fee_exit": fee_exit,
            "net": gross - fee_exit - fee_entry,
            # phi mo da tru vao equity luc mo -> equity chi cong phan nay
            "equity_delta": gross - fee_exit,
            "fee_estimated": bool(exit_estimated
                                  or pos.get("fee_entry_estimated")),
        }

    def _apply_booking(self, booking):
        st = self.state
        st["equity"] = float(st.get("equity", 0) or 0) + booking["equity_delta"]
        stats = st["stats"]
        stats["fees"] = float(stats.get("fees", 0) or 0) + booking["fee_exit"]
        stats["trades"] = int(stats.get("trades", 0) or 0) + 1
        if booking["net"] > 0:
            stats["wins"] = int(stats.get("wins", 0) or 0) + 1
        else:
            stats["losses"] = int(stats.get("losses", 0) or 0) + 1

    @staticmethod
    def _booking_fields(booking):
        return {
            "pnl": round(booking["net"], 4),
            "pnl_gross": round(booking["gross"], 4),
            "fee_entry": round(booking["fee_entry"], 6),
            "fee_exit": round(booking["fee_exit"], 6),
            "fee_estimated": booking["fee_estimated"],
        }

    def _record_entry_fee(self, pos):
        """Thay phi mo uoc tinh bang commission that (goi SAU khi dat TP/SL
        de khong lam cham bao ve lot)."""
        summary = self._order_fill_summary(pos.get("symbol"), pos.get("ord_id"))
        if summary is None:
            pos["fee_entry_estimated"] = True
            return
        delta = summary["commission"] - float(pos.get("fee_entry", 0) or 0)
        pos["fee_entry"] = summary["commission"]
        pos.pop("fee_entry_estimated", None)
        self.state["equity"] = float(self.state.get("equity", 0) or 0) - delta
        stats = self.state["stats"]
        stats["fees"] = float(stats.get("fees", 0) or 0) + delta

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
        elif kind == "ALGO_UPDATE":
            order = event.get("o") or {}
            symbol = str(order.get("s") or "").upper()
            algo_id = order.get("aid")
            status = str(order.get("X") or "").upper()
            if algo_id is not None and symbol:
                with self._order_condition:
                    self._algo_events["%s:%s" % (symbol, algo_id)] = order
                    if len(self._algo_events) > 4096:
                        for _ in range(1024):
                            self._algo_events.pop(next(iter(self._algo_events)))
            if status not in ("NEW", "TRIGGERING"):
                # A guard finished/was cancelled: resolve it on the next loop
                # instead of waiting for the periodic protection sync.
                self._protection_sync_due = True
            self.log("USER ALGO event symbol=%s algo=%s status=%s ap=%s aq=%s "
                     "positionSide=%s rm=%s"
                     % (symbol, algo_id, status, order.get("ap"),
                        order.get("aq"), order.get("ps"), order.get("rm")))
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

    def _missing_guards(self, pos):
        """Labels whose exchange guard is required but has no algo id."""
        if self.dry_run or not self.cfg.get("exchange_protection", False):
            return []
        return [label for label in ("sl", "tp")
                if pos.get(label) is not None
                and not pos.get("%s_algo_id" % label)]

    def _create_exchange_protection(self, pos):
        """Arm the MISSING Binance conditional guards of a live lot.

        Each leg is independent: an existing leg is never re-created (that
        produced duplicates/orphans) and a successful leg is never cancelled
        because the other one failed (a lone SL is far better than nothing).
        Raises after trying every missing leg if any of them failed; the
        caller keeps the lot in 'retrying' until all legs are armed.
        """
        if self.dry_run or not self.cfg.get("exchange_protection", False):
            return {}
        order_side = "SELL" if pos["side"] == "long" else "BUY"
        position_side = "LONG" if pos["side"] == "long" else "SHORT"
        order_types = {"sl": "STOP_MARKET", "tp": "TAKE_PROFIT_MARKET"}
        errors = []
        for label in self._missing_guards(pos):
            client_key = "%s_client_algo_id" % label
            # A previous POST may have landed although its response was
            # lost: adopt it by clientAlgoId instead of placing a duplicate.
            previous = pos.get(client_key)
            if previous:
                try:
                    found = self._find_open_algo_by_client_id(previous,
                                                              pos["symbol"])
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as exc:
                    errors.append((label, exc))
                    continue
                if found:
                    pos["%s_algo_id" % label] = found
                    self.log("PROTECTION #%s nhan lai guard %s algo=%s"
                             % (pos.get("id"), label.upper(), found))
                    continue
            client_algo_id = self._new_client_order_id(
                pos["symbol"], pos["side"]
            )[:36]
            pos[client_key] = client_algo_id
            params = {
                "algoType": "CONDITIONAL",
                "symbol": pos["symbol"],
                "side": order_side,
                "positionSide": position_side,
                "type": order_types[label],
                "quantity": pos["qty"],
                "triggerPrice": self._rounded_trigger_price(
                    pos["symbol"], pos[label]
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
                algo_id = None
                try:
                    response = self._private_call(
                        "private:trade",
                        self.ex.fapiPrivatePostAlgoOrder,
                        params,
                    )
                    algo_id = (response or {}).get("algoId")
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as post_error:
                    # The POST may have reached Binance before the transport
                    # failed. Reconcile by clientAlgoId; an unknown Algo id
                    # must never become an invisible orphan.
                    algo_id = self._find_open_algo_by_client_id(
                        client_algo_id, pos["symbol"])
                    if not algo_id:
                        raise post_error
                if not algo_id:
                    # A successful HTTP response without an id is also
                    # ambiguous; query the idempotency key once.
                    algo_id = self._find_open_algo_by_client_id(
                        client_algo_id, pos["symbol"])
                if not algo_id:
                    raise RuntimeError("Binance algo order missing algoId")
                # Persist each id on the position immediately so a later
                # failure can never make this guard invisible.
                pos["%s_algo_id" % label] = algo_id
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                errors.append((label, exc))
                text = str(exc)
                if "-4045" in text or "max stop order" in text.lower():
                    self.log("CRITICAL PROTECTION #%s: cham gioi han so lenh "
                             "dieu kien Binance (-4045) - kiem tra lenh mo coi"
                             % pos.get("id"))
        if errors:
            raise RuntimeError("; ".join(
                "%s: %s" % (label.upper(), binance_safety.redact_body(exc))
                for label, exc in errors))
        return {label: pos.get("%s_algo_id" % label) for label in ("sl", "tp")
                if pos.get("%s_algo_id" % label)}

    @staticmethod
    def _is_absent_error(exc):
        message = str(exc).lower()
        return ("-2011" in message or "-2013" in message
                or "not found" in message or "does not exist" in message)

    def _cancel_algo_quietly(self, symbol, algo_id=None, client_algo_id=None):
        """Cancel one Algo order; return 'cancelled', 'absent' or 'error'.

        Used when the lot is already gone (closed on the exchange): the
        leftover sibling guard must not stay live, but failure to cancel is
        logged instead of raised (orphan cleanup retries it later).
        """
        if self.dry_run or (not algo_id and not client_algo_id):
            return "absent"
        identifier = {"symbol": symbol}
        if algo_id:
            identifier["algoId"] = algo_id
        else:
            identifier["clientAlgoId"] = client_algo_id
        try:
            self._private_call("private:trade",
                               self.ex.fapiPrivateDeleteAlgoOrder, identifier)
            self.log("PROTECTION cancel %s algo=%s ok"
                     % (symbol, algo_id or client_algo_id))
            return "cancelled"
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            if self._is_absent_error(exc):
                return "absent"
            self.log("WARNING PROTECTION cancel %s algo=%s failed: %s"
                     % (symbol, algo_id or client_algo_id,
                        binance_safety.redact_body(exc)))
            return "error"

    def _cancel_leftover_guards(self, pos, skip_label=None):
        """Cancel every remaining guard of a lot that no longer exists."""
        for label in ("sl", "tp"):
            if label == skip_label:
                continue
            self._cancel_algo_quietly(pos.get("symbol"),
                                      pos.get("%s_algo_id" % label),
                                      pos.get("%s_client_algo_id" % label))

    def _cancel_exchange_protection(self, pos):
        if self.dry_run:
            return
        if (not self.cfg.get("exchange_protection", False)
                and not pos.get("sl_algo_id") and not pos.get("tp_algo_id")):
            return
        absent_ids = []
        absent_labels = []
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
                    absent_labels.append(label)
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

        for label in absent_labels:
            # "Not found" usually means Binance already triggered the guard
            # (the race with our own local TP/SL check). Ask what happened:
            # a fill is a normal exchange close, not a reconciliation fault.
            outcome = self._algo_outcome(pos, label)
            if outcome is None:
                continue
            if outcome["status"] in self._ALGO_IN_FLIGHT or (
                    outcome["status"] == "FINISHED"
                    and outcome.get("qty") is None):
                self._protection_sync_soon(1.0)
                raise GuardInFlight("guard %s of #%s is executing"
                                    % (label, pos.get("id")))
            if outcome["status"] == "FINISHED" and outcome["qty"] > 0:
                raise GuardAlreadyFilled(label, outcome)
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
                # Binance reports ONE aggregate per Hedge leg; compare it with
                # the sum of every local lot on that leg, not this lot alone.
                expected = self._local_qty(pos["symbol"], pos["side"])
                tolerance = self._qty_tolerance(pos["symbol"], expected)
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

    def _local_qty(self, symbol, side, exclude_id=None):
        """Sum of local lots for one Hedge leg (Binance only sees the sum)."""
        return sum(float(p.get("qty", 0) or 0)
                   for p in self.state.get("positions", [])
                   if p.get("symbol") == symbol and p.get("side") == side
                   and (exclude_id is None or p.get("id") != exclude_id))

    def _qty_tolerance(self, symbol, qty):
        step = 0.0
        try:
            step = float((self._filters_for(symbol) or (0,))[0] or 0)
        except Exception:
            pass
        return max(step * 1.1, abs(float(qty or 0)) * 0.001, 1e-10)

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

        San ve ~0: ghi nhan moi lot cua leg. Leg giam MOT PHAN: xem
        _detect_partial_leg_reduction (chi khi khop dung nhom lot, guard
        con nguyen); khong khop -> reconcile_positions halt nhu cu.

        Fail-closed: mot lan doc positionRisk rong/tre (API glitch, lenh vua
        khop chua hien) KHONG du de xoa lot. Lot phai (1) da mo it nhat
        ``exchange_close_min_age_seconds`` va (2) bi thay ~0 o hai lan quet
        lien tiep cach nhau >= ``exchange_close_confirm_seconds``.
        """
        if self.dry_run:
            return []
        now = time.time()
        interval = float(self.cfg.get("exchange_close_check_seconds", 10))
        if now - getattr(self, "_last_detect_closed", 0) < interval:
            return []
        self._last_detect_closed = now
        min_age = float(self.cfg.get("exchange_close_min_age_seconds", 60))
        confirm_after = float(self.cfg.get("exchange_close_confirm_seconds",
                                           interval))
        pending = getattr(self, "_exchange_close_pending", None)
        if pending is None:
            pending = self._exchange_close_pending = {}
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
        live_ids = {p.get("id") for p in self.state.get("positions", [])}
        for stale in [i for i in pending if i not in live_ids]:
            pending.pop(stale, None)
        marks = mark_prices or {}
        recs = []
        used_orders = set()
        leg_closing = [p for p in self.state.get("positions", [])
                       if exchange.get((p.get("symbol"), p.get("side")), 0.0)
                       <= self._qty_tolerance(p.get("symbol"), p.get("qty"))]
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
                pending.pop(pos.get("id"), None)
                continue  # khop voi san
            if actual > tolerance:
                pending.pop(pos.get("id"), None)
                continue  # dong mot phan -> de reconcile_positions halt nhu cu
            # San ve ~0 trong khi state van co -> co the da bi dong ngoai.
            opened_at = float(pos.get("opened_at", 0) or 0)
            if opened_at and now - opened_at < min_age:
                pending.pop(pos.get("id"), None)
                continue  # lot vua mo: positionRisk co the chua cap nhat
            first_seen = pending.get(pos.get("id"))
            if first_seen is None:
                pending[pos.get("id")] = now
                self.log("WARNING EXCHANGE_CLOSE? #%s %s %s: san bao ~0, "
                         "cho xac nhan lan 2" % (pos.get("id"), key[0], key[1]))
                continue
            if now - first_seen < confirm_after:
                continue
            pending.pop(pos.get("id"), None)
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
            # Gia khop that: chi lay fill dong DUNG leg (positionSide), sau
            # khi lot mo, va khop khoi luong lot (hoac ca leg neu 1 lenh dong
            # tat ca). Khong tim thay -> giu gia uoc tinh, danh dau estimated.
            group_qty = sum(float(p.get("qty", 0) or 0) for p in leg_closing
                            if (p.get("symbol"), p.get("side")) == key)
            actual_exit, close_order = self._exchange_exit_from_trades(
                pos, used_orders, group_qty)
            if actual_exit:
                fired = "EXCHANGE"
            if actual_exit:
                exit_px = actual_exit
            elif exit_px is None:
                exit_px = mark if mark else entry
            booking = self._book_exit(dict(pos, qty=local_qty), exit_px,
                                      close_order if actual_exit else None)
            self._apply_booking(booking)
            net = booking["net"]
            reason = self._exit_reason_from_price(pos, exit_px,
                                                  fired == "EXCHANGE")
            rec = {
                "id": pos["id"], "symbol": symbol, "side": side,
                "tag": pos.get("tag"), "entry": round(entry, 6),
                "exit": round(exit_px, 6),
                "notional": round(float(pos.get("notional", 0) or 0), 2),
                "reason": reason,
                "closed_at": int(now),
                "live": True, "dry": self.dry_run,
                "estimated": fired != "EXCHANGE", "exit_fired": fired,
                "exit_source": "exchange_detect",
                "close_ord": close_order,
                **self._booking_fields(booking),
            }
            self.state["positions"] = [p for p in self.state["positions"]
                                       if p.get("id") != pos["id"]]
            # TP/SL tren Binance khong phai OCO: lot da mat thi guard con lai
            # van treo va co the dong nham lot moi cung leg -> huy ngay.
            self._cancel_leftover_guards(pos)
            self._db_insert_trade(rec)
            self.log("EXCHANGE_CLOSE #%s %s %s: khong con tren san, dong "
                     "@%s (%s) reason=%s pnl=%+.2f [%s]"
                     % (pos["id"], symbol, side, round(exit_px, 6), fired,
                        reason, net,
                        "exchange fill" if fired == "EXCHANGE"
                        else "estimated"))
            recs.append(rec)
        recs.extend(self._detect_partial_leg_reduction(
            exchange, now, marks, min_age, confirm_after))
        return recs

    def _detect_partial_leg_reduction(self, exchange, now, marks, min_age,
                                      confirm_after):
        """Leg tren san GIAM mot phan (vd dong tay 1 trong nhieu lot grid).

        San la source of truth: neu phan giam khop dung tong qty cua mot
        nhom lot (chon LIFO) thi ghi nhan cac lot do da dong (gia = fill dong
        that tu userTrades, khong co -> mark, estimated) thay vi halt vo han.
        An toan: xac nhan 2 lan quet; lot du tuoi; neu bat ky guard nao cua
        leg khong con open (co the guard bot vua khop) -> hoan, de
        sync_exchange_protection ghi dung lot. Khong khop subset -> giu
        nguyen (reconcile_positions halt nhu cu).
        """
        pending = getattr(self, "_partial_pending", None)
        if pending is None:
            pending = self._partial_pending = {}
        legs = {}
        for p in self.state.get("positions", []):
            if float(p.get("qty", 0) or 0) > 0:
                legs.setdefault((p.get("symbol"), p.get("side")), []).append(p)
        for stale in [k for k in pending if k not in legs]:
            pending.pop(stale, None)
        recs = []
        for key, lots in legs.items():
            symbol, side = key
            local = sum(float(p.get("qty", 0) or 0) for p in lots)
            actual = exchange.get(key, 0.0)
            tol = self._qty_tolerance(symbol, local)
            if actual <= tol or actual >= local - tol:
                pending.pop(key, None)     # khop / ve 0 (o tren) / san nhieu hon
                continue
            reduction = local - actual
            chosen, acc = [], 0.0
            for p in sorted(lots, key=lambda x: (float(x.get("opened_at", 0)
                                                       or 0), x.get("id", 0)),
                            reverse=True):
                q = float(p.get("qty", 0) or 0)
                if acc + q <= reduction + tol:
                    chosen.append(p)
                    acc += q
                if abs(acc - reduction) <= tol:
                    break
            if not chosen or abs(acc - reduction) > tol:
                pending.pop(key, None)
                continue                   # khong quy duoc cho lot nao
            if any(now - float(p.get("opened_at", 0) or 0) < min_age
                   for p in chosen if p.get("opened_at")):
                continue
            prev = pending.get(key)
            if prev is None or abs(prev[1] - actual) > tol:
                pending[key] = (now, actual)
                self.log("WARNING LEG_REDUCED? %s %s: san %s < local %s, cho "
                         "xac nhan lan 2" % (symbol, side, actual, local))
                continue
            if now - prev[0] < confirm_after:
                continue
            if self.cfg.get("exchange_protection", False):
                try:
                    open_ids = {str(o.get("algoId")) for o in
                                self._fetch_open_algo_orders(symbol)
                                if isinstance(o, dict)}
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as exc:
                    self.log("WARNING leg reduced %s: khong doc duoc algo "
                             "(%s) -> hoan" %
                             (symbol, binance_safety.redact_body(exc)))
                    continue
                gone = [p.get("id") for p in lots for label in ("sl", "tp")
                        if p.get("%s_algo_id" % label)
                        and str(p.get("%s_algo_id" % label)) not in open_ids]
                if gone:
                    self.log("INFO leg reduced %s %s: guard cua lot %s khong "
                             "con open -> de protection sync ghi nhan"
                             % (symbol, side, sorted(set(gone))))
                    self._protection_sync_soon()
                    continue
            pending.pop(key, None)
            exit_px, order_id = self._reduction_fill(
                symbol, side, reduction,
                min(float(p.get("opened_at", 0) or 0) for p in lots))
            estimated = exit_px is None
            if estimated:
                try:
                    exit_px = float(marks.get(symbol))
                except (TypeError, ValueError):
                    exit_px = None
            for pos in chosen:
                px = exit_px or float(pos.get("entry", 0) or 0)
                booking = self._book_exit(pos, px,
                                          None if estimated else order_id)
                self._apply_booking(booking)
                rec = {
                    "id": pos["id"], "symbol": symbol, "side": side,
                    "tag": pos.get("tag"),
                    "entry": round(float(pos.get("entry", 0) or 0), 6),
                    "exit": round(px, 6),
                    "notional": round(float(pos.get("notional", 0) or 0), 2),
                    "reason": "CLOSED_ON_EXCHANGE",
                    "closed_at": int(now), "live": True, "dry": self.dry_run,
                    "estimated": estimated,
                    "exit_source": "exchange_detect_partial",
                    "close_ord": None if estimated else order_id,
                    **self._booking_fields(booking),
                }
                self.state["positions"] = [
                    p for p in self.state["positions"]
                    if p.get("id") != pos["id"]]
                self._cancel_leftover_guards(pos)
                self._db_insert_trade(rec)
                self.log("EXCHANGE_PARTIAL #%s %s %s: leg san giam %s -> ghi "
                         "nhan lot dong @%s pnl=%+.2f [%s]"
                         % (pos["id"], symbol, side, round(reduction, 8),
                            round(px, 6), booking["net"],
                            "estimated" if estimated else "exchange fill"))
                recs.append(rec)
        return recs

    def _reduction_fill(self, symbol, side, qty, since_s):
        """Lenh dong (khong phai cua bot) gan nhat tren leg co qty = qty.
        Tra ve (avg, order_id) hoac (None, None)."""
        close_side = "sell" if side == "long" else "buy"
        leg = "LONG" if side == "long" else "SHORT"
        try:
            trades = self._private_call(
                "private:account", self.ex.fetch_my_trades,
                self._ccxt_symbol(symbol) or symbol, None, 500,
                _weight=5) or []
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.log("WARNING userTrades %s: %s" %
                     (symbol, binance_safety.redact_body(exc)))
            return None, None
        booked = getattr(self, "_booked_close_orders", set())
        orders = {}
        for trade in trades:
            info = trade.get("info") or {}
            if (str(trade.get("side", "")).lower() != close_side
                    or str(info.get("positionSide", "")).upper() != leg):
                continue
            try:
                ts = int(trade.get("timestamp") or info.get("time") or 0)
                q = float(trade.get("amount") or info.get("qty") or 0)
                px = float(trade.get("price") or info.get("price") or 0)
            except (TypeError, ValueError):
                continue
            oid = str(trade.get("order") or info.get("orderId") or "")
            if ts < since_s * 1000 or q <= 0 or px <= 0 or oid in booked:
                continue
            g = orders.setdefault(oid, {"qty": 0.0, "cost": 0.0, "ts": 0})
            g["qty"] += q
            g["cost"] += q * px
            g["ts"] = max(g["ts"], ts)
        for oid, g in sorted(orders.items(), key=lambda i: i[1]["ts"],
                             reverse=True):
            if abs(g["qty"] - qty) <= self._qty_tolerance(symbol, qty):
                return g["cost"] / g["qty"], oid
        return None, None

    @staticmethod
    def _exit_reason_from_price(pos, exit_px, real_fill):
        """'SL'/'TP' only when a REAL fill is at/through that trigger.

        The scalp cooldown keys on reason == 'SL'; an estimated price must
        never claim a specific trigger.
        """
        if not real_fill or not exit_px:
            return "CLOSED_ON_EXCHANGE"
        sl, tp = pos.get("sl"), pos.get("tp")
        slack = 0.001
        if pos.get("side") == "long":
            if sl and exit_px <= sl * (1 + slack):
                return "SL"
            if tp and exit_px >= tp * (1 - slack):
                return "TP"
        else:
            if sl and exit_px >= sl * (1 - slack):
                return "SL"
            if tp and exit_px <= tp * (1 + slack):
                return "TP"
        return "CLOSED_ON_EXCHANGE"

    def _exchange_exit_from_trades(self, pos, used_orders, group_qty):
        """Average exit price of the exchange fill that closed ``pos``.

        Hedge Mode trades on the same symbol include opens of the opposite
        leg (a SELL also opens a SHORT) and fills of earlier positions, so a
        trade is accepted only if it is on the closing side of this leg
        (positionSide), executed after the lot opened, and its order quantity
        equals this lot (or the whole leg when one order closed every lot).
        Returns (price, order_id) or (None, None).
        """
        symbol, side = pos.get("symbol"), pos.get("side")
        close_side = "sell" if side == "long" else "buy"
        leg = "LONG" if side == "long" else "SHORT"
        opened_ms = int(float(pos.get("opened_at", 0) or 0) * 1000)
        try:
            ccxt_symbol = self._ccxt_symbol(symbol) or symbol
            # KHONG truyen since=opened: (1) lot mo > 7 ngay -> ccxt cat
            # endTime = since+7d -> mat fill dong gan day; (2) co startTime
            # Binance tra trade CU NHAT tu moc do (limit) -> symbol grid ban
            # day fill dong moi nhat. Khong since = trade MOI NHAT 7 ngay;
            # loc ts >= opened o duoi.
            trades = self._private_call(
                "private:account", self.ex.fetch_my_trades, ccxt_symbol,
                None, 500, _weight=5) or []
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.log("WARNING userTrades %s: %s" %
                     (symbol, binance_safety.redact_body(exc)))
            return None, None
        orders = {}
        for trade in trades:
            info = trade.get("info") or {}
            if str(trade.get("side", "")).lower() != close_side:
                continue
            if str(info.get("positionSide", "")).upper() != leg:
                continue
            try:
                ts = int(trade.get("timestamp") or info.get("time") or 0)
                qty = float(trade.get("amount") or info.get("qty") or 0)
                price = float(trade.get("price") or info.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if ts < opened_ms or qty <= 0 or price <= 0:
                continue
            oid = str(trade.get("order") or info.get("orderId") or "")
            group = orders.setdefault(oid, {"qty": 0.0, "cost": 0.0, "ts": 0})
            group["qty"] += qty
            group["cost"] += qty * price
            group["ts"] = max(group["ts"], ts)
        ranked = sorted(orders.items(), key=lambda item: item[1]["ts"],
                        reverse=True)
        lot_qty = float(pos.get("qty", 0) or 0)
        for oid, group in ranked:
            if oid in used_orders:
                continue
            if abs(group["qty"] - lot_qty) <= self._qty_tolerance(symbol, lot_qty):
                used_orders.add(oid)
                return group["cost"] / group["qty"], oid
        if group_qty and group_qty > lot_qty:
            for oid, group in ranked:
                if abs(group["qty"] - group_qty) <= self._qty_tolerance(
                        symbol, group_qty):
                    return group["cost"] / group["qty"], oid
        return None, None

    # ------------------------------------------- protection lifecycle
    _ALGO_IN_FLIGHT = ("NEW", "TRIGGERING", "TRIGGERED")
    _ALGO_LOST = ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED")

    @staticmethod
    def _float_or_none(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value

    def _algo_outcome(self, pos, label):
        """What happened to one guard of ``pos``.

        Returns {"status", "order_id", "avg", "qty", "reason"} or None when
        it cannot be determined (caller must then change nothing).  The
        private-stream ALGO_UPDATE is preferred; REST Query Algo Order is the
        fallback after a stream gap or restart.  For a FINISHED guard the
        resulting MARKET order is read for the real average fill/quantity.
        """
        symbol = str(pos.get("symbol") or "").upper()
        algo_id = pos.get("%s_algo_id" % label)
        client_algo_id = pos.get("%s_client_algo_id" % label)
        if not algo_id and not client_algo_id:
            return None
        condition = getattr(self, "_order_condition", None)
        events = getattr(self, "_algo_events", None) or {}
        if condition is not None:
            with condition:
                event = events.get("%s:%s" % (symbol, algo_id))
        else:
            event = events.get("%s:%s" % (symbol, algo_id))
        outcome = None
        if event is not None:
            outcome = {
                "status": str(event.get("X") or "").upper(),
                "order_id": event.get("ai") or None,
                "avg": self._float_or_none(event.get("ap")),
                "qty": self._float_or_none(event.get("aq")),
                "reason": event.get("rm"),
            }
            if outcome["status"] in self._ALGO_IN_FLIGHT:
                outcome = None   # stale snapshot; ask REST for the latest
        if outcome is None:
            params = ({"clientAlgoId": client_algo_id} if client_algo_id
                      else {"algoId": algo_id})
            try:
                row = self._private_call("private:order_status",
                                         self.ex.fapiPrivateGetAlgoOrder,
                                         params)
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                self.log("WARNING query algo %s %s: %s" %
                         (symbol, algo_id or client_algo_id,
                          binance_safety.redact_body(exc)))
                return None
            row = row or {}
            if str(row.get("symbol") or symbol).upper() != symbol or (
                    algo_id and row.get("algoId") is not None
                    and str(row.get("algoId")) != str(algo_id)):
                self.log("WARNING query algo %s returned another order %s/%s"
                         % (algo_id, row.get("symbol"), row.get("algoId")))
                return None
            outcome = {
                "status": str(row.get("algoStatus") or "").upper(),
                "order_id": row.get("actualOrderId") or None,
                "avg": self._float_or_none(row.get("actualPrice")),
                "qty": None,
                "reason": row.get("rejectReason") or row.get("rm"),
            }
        if outcome["avg"] is not None and outcome["avg"] <= 0:
            outcome["avg"] = None
        if (outcome["status"] == "FINISHED" and outcome["order_id"]
                and (outcome["avg"] is None or outcome["qty"] is None)):
            try:
                order = self._private_call(
                    "private:order_status", self.ex.fetch_order,
                    str(outcome["order_id"]),
                    self._ccxt_symbol(symbol) or symbol)
                outcome["avg"] = outcome["avg"] or self._order_average(order)
                outcome["qty"] = self._float_or_none(
                    (order or {}).get("filled")
                    or (order or {}).get("executedQty"))
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                self.log("WARNING fetch algo fill order %s: %s" %
                         (outcome["order_id"],
                          binance_safety.redact_body(exc)))
        # qty None = unknown fill (fail closed: caller changes nothing);
        # qty 0 = Binance explicitly reports no execution.
        return outcome

    def _mark_guard_lost(self, pos, label, outcome):
        """A guard ended without closing the lot: forget it and re-arm."""
        now = time.time()
        self.log("WARNING PROTECTION #%s %s %s guard %s algo=%s ket thuc "
                 "status=%s reason=%s nhung lot van mo -> dat lai"
                 % (pos.get("id"), pos.get("symbol"), pos.get("side"),
                    label.upper(), pos.get("%s_algo_id" % label),
                    outcome.get("status"), outcome.get("reason")))
        pos["%s_algo_id" % label] = None
        pos["%s_client_algo_id" % label] = None
        pos["protection_status"] = "retrying"
        pos["protection_retry_at"] = now
        pos.setdefault("protection_deadline", now + 120)

    def _finalize_exchange_close(self, pos, label, outcome):
        """Book a lot that Binance closed through its own TP/SL guard."""
        symbol, side = pos["symbol"], pos["side"]
        lot_qty = float(pos.get("qty", 0) or 0)
        filled = outcome.get("qty")
        if filled is not None and abs(filled - lot_qty) > self._qty_tolerance(
                symbol, lot_qty):
            self.state["halted"] = True
            self.state["halt_reason"] = (
                "exchange protection partial fill requires reconciliation")
            self.log("CRITICAL PROTECTION #%s %s guard %s khop %s / lot %s "
                     "-> halt" % (pos["id"], symbol, label.upper(), filled,
                                  lot_qty))
            return None
        exit_px = outcome.get("avg")
        estimated = not exit_px
        if estimated:
            exit_px = float(pos.get(label) or pos.get("entry"))
        entry = float(pos.get("entry", 0) or 0)
        booking = self._book_exit(pos, exit_px, outcome.get("order_id"))
        self._apply_booking(booking)
        net = booking["net"]
        rec = {
            "id": pos["id"], "symbol": symbol, "side": side,
            "tag": pos.get("tag"), "entry": round(entry, 6),
            "exit": round(exit_px, 6),
            "notional": round(float(pos.get("notional", 0) or 0), 2),
            **self._booking_fields(booking),
            "reason": label.upper(),
            "closed_at": int(time.time()), "live": True, "dry": self.dry_run,
            "close_ord": (str(outcome["order_id"]) if outcome.get("order_id")
                          else None),
            "exit_source": "exchange_algo",
            "algo_id": pos.get("%s_algo_id" % label),
            "estimated": estimated,
        }
        self.state["positions"] = [p for p in self.state["positions"]
                                   if p.get("id") != pos["id"]]
        sibling = "tp" if label == "sl" else "sl"
        result = self._cancel_algo_quietly(
            symbol, pos.get("%s_algo_id" % sibling),
            pos.get("%s_client_algo_id" % sibling))
        if result == "absent" and pos.get("%s_algo_id" % sibling):
            other = self._algo_outcome(pos, sibling)
            if (other and other["status"] == "FINISHED"
                    and (other.get("qty") or 0) > 0):
                self.state["halted"] = True
                self.state["halt_reason"] = (
                    "exchange protection double fill requires reconciliation")
                self.log("CRITICAL PROTECTION #%s ca SL va TP deu khop -> halt"
                         % pos["id"])
        self._db_insert_trade(rec)
        self.log("EXCHANGE %s #%s %s %s khop tren san @%s pnl=%+.2f ord=%s%s"
                 % (label.upper(), pos["id"], symbol, side, round(exit_px, 6),
                    net, rec["close_ord"],
                    " [gia uoc tinh]" if estimated else ""))
        return rec

    def defer_local_exit(self, pos, label):
        """True while Binance's own armed guard should execute this exit.

        The bot and the exchange watch the same mark price. Closing locally
        at the same moment races the exchange trigger (cancel says "not
        found", the lot is half-handled). With an armed guard the bot waits
        ``protection_grace_seconds`` for Binance, then falls back to its own
        market close if the guard still has not fired.
        """
        if self.dry_run or not self.cfg.get("exchange_protection", False):
            return False
        if (pos.get("protection_status") != "armed"
                or not pos.get("%s_algo_id" % label)):
            return False
        now = time.time()
        key = (pos.get("id"), label)
        first = self._trigger_seen.setdefault(key, now)
        grace = float(self.cfg.get("protection_grace_seconds", 15))
        if now - first < grace:
            self._protection_sync_soon(1.0)
            return True
        self.log("WARNING PROTECTION #%s guard %s chua khop sau %.0fs -> "
                 "bot tu dong lenh" % (pos.get("id"), label.upper(),
                                       now - first))
        return False

    def clear_local_trigger(self, pos):
        for label in ("sl", "tp"):
            self._trigger_seen.pop((pos.get("id"), label), None)

    def _protection_sync_soon(self, seconds=2.0):
        """Re-check an in-flight guard shortly, without a 0.5s REST loop."""
        interval = float(self.cfg.get("protection_sync_seconds", 10))
        self._last_protection_sync = min(
            getattr(self, "_last_protection_sync", 0.0),
            time.time() - interval + seconds)

    def sync_exchange_protection(self, force=False):
        """Resolve every lot guard against Binance; return closed records.

        For each symbol with guarded lots: one open-Algo query (weight 1).
        A guard that is no longer open is looked up (WS event or REST):
        FINISHED with a fill -> the lot was closed by Binance: book the real
        fill, cancel the sibling guard.  CANCELED/EXPIRED/REJECTED (or
        FINISHED without fill) -> the lot is unguarded: mark it for re-arm.
        Anything unknown changes nothing (fail closed).
        """
        if self.dry_run or not self.cfg.get("exchange_protection", False):
            return []
        now = time.time()
        interval = float(self.cfg.get("protection_sync_seconds", 10))
        if (not force and not self._protection_sync_due
                and now - self._last_protection_sync < interval):
            return []
        self._protection_sync_due = False
        self._last_protection_sync = now
        recs = []
        symbols = sorted({p.get("symbol") for p in self.state["positions"]
                          if p.get("live") and (p.get("sl_algo_id")
                                                or p.get("tp_algo_id"))})
        for symbol in symbols:
            try:
                open_rows = self._fetch_open_algo_orders(symbol)
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                self.log("WARNING protection sync %s: %s" %
                         (symbol, binance_safety.redact_body(exc)))
                continue
            open_ids = set()
            for row in open_rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("symbol") or symbol).upper() != symbol:
                    continue
                if row.get("algoId") is not None:
                    open_ids.add(str(row.get("algoId")))
            for pos in [p for p in list(self.state["positions"])
                        if p.get("symbol") == symbol]:
                for label in ("sl", "tp"):
                    algo_id = pos.get("%s_algo_id" % label)
                    if not algo_id or str(algo_id) in open_ids:
                        continue
                    outcome = self._algo_outcome(pos, label)
                    if outcome is None:
                        continue
                    status = outcome["status"]
                    if status in self._ALGO_IN_FLIGHT:
                        self._protection_sync_soon()
                        continue
                    if status == "FINISHED":
                        filled = outcome.get("qty")
                        if filled is None:
                            # Fill not readable yet: never guess either way.
                            self._protection_sync_soon()
                            continue
                        if filled > 0:
                            rec = self._finalize_exchange_close(pos, label,
                                                                outcome)
                            if rec:
                                recs.append(rec)
                            break
                        self._mark_guard_lost(pos, label, outcome)
                    elif status in self._ALGO_LOST:
                        self._mark_guard_lost(pos, label, outcome)
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
        if self.state.get("halted") and self.state.get("halt_reason") in (
                "unmanaged open exchange order",
                "open order reconciliation unavailable"):
            self.log("RECOVERY: khong con lenh thuong dang treo tren symbol "
                     "cua bot -> tu dong unhalt (%s)"
                     % self.state.get("halt_reason"))
            self.state["halted"] = False
            self.state["halt_reason"] = None
        return True

    _STARTUP_HOLDS = {
        "unmanaged open exchange order": "orders",
        "open order reconciliation unavailable": "orders",
        "exchange protection reconciliation mismatch": "protection",
        "exchange protection reconciliation unavailable": "protection",
    }

    def recheck_startup_holds(self, force=False):
        """Kiem tra lai dinh ky cac halt do doi chieu luc khoi dong.

        Truoc day cac check nay chi chay 1 lan khi start: sau khi nguoi van
        hanh don san, halt (persistent, khong het khi sang ngay) van treo den
        khi restart. Tra ve True neu da unhalt."""
        if self.dry_run or not self.state.get("halted"):
            return False
        kind = self._STARTUP_HOLDS.get(self.state.get("halt_reason"))
        if kind is None:
            return False
        now = time.time()
        interval = float(self.cfg.get("startup_hold_recheck_seconds", 60))
        if not force and now - getattr(self, "_last_hold_recheck", 0.0) < interval:
            return False
        self._last_hold_recheck = now
        if kind == "orders":
            self._reconcile_startup_open_orders()
        else:
            try:
                rows = self.get_positions()
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                self.log("WARNING recheck protection: khong doc duoc vi the: "
                         "%s" % binance_safety.redact_body(exc))
                return False
            if self.cfg.get("exchange_protection", False):
                try:
                    self.cleanup_orphan_orders()
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as exc:
                    self.log("WARNING recheck orphan cleanup: %s" %
                             binance_safety.redact_body(exc))
            self._reconcile_exchange_protection(rows)
        return not self.state.get("halted")

    def _fetch_open_algo_orders(self, symbol=None):
        """Return normalized open USD-M Algo Orders from the exchange."""
        query = getattr(self.ex, "fapiPrivateGetOpenAlgoOrders", None)
        if query is None:
            raise RuntimeError("CCXT has no open Algo Order query")
        # Binance charges weight 40 when symbol is omitted (1 with symbol);
        # keeping the all-symbol scan in the governor prevents a false local
        # estimate from hiding the real IP budget.
        if symbol:
            response = self._private_call("private:trade", query,
                                          {"symbol": symbol}, _weight=1)
        else:
            response = self._private_call("private:trade", query, {},
                                          _weight=40)
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

    def _find_open_algo_by_client_id(self, client_algo_id, symbol=None):
        for order in self._fetch_open_algo_orders(symbol):
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
                client_algo_id = (order.get("clientAlgoId")
                                  or info.get("clientAlgoId"))
                if not self._is_bot_algo(symbol, client_algo_id):
                    # Lenh nguoi dung tu dat: bot khong so huu, khong huy,
                    # khong halt (giong cleanup luc dang chay). Neu no dong/
                    # mo vi the, reconcile_positions/detect se bat duoc.
                    seen_foreign = getattr(self, "_foreign_algo_logged", None)
                    if seen_foreign is None:
                        seen_foreign = self._foreign_algo_logged = set()
                    if (symbol, algo_id) not in seen_foreign:
                        seen_foreign.add((symbol, algo_id))
                        self.log("WARNING algo %s %s (clientAlgoId=%s) khong "
                                 "do bot tao tren symbol cua bot -> giu "
                                 "nguyen, khong halt" %
                                 (symbol, algo_id, client_algo_id))
                    continue
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
        if missing:
            # A guard that is no longer open has finished (TP/SL filled while
            # the bot was down) or was cancelled. sync_exchange_protection
            # resolves each one by algoId: book the fill or re-arm.
            self.log("INFO %d guard khong con open khi khoi dong: %s -> "
                     "sync_exchange_protection se doi chieu" %
                     (len(missing), missing))
            self._protection_sync_due = True
            missing = []  # resolved by sync, not a startup halt
        if missing_required:
            # A lot without a guard id is re-armed by retry_protection (a lot
            # missing its SL is closed after the deadline). Halting here would
            # block retry_protection itself (reconcile hold) and leave the
            # lot unguarded indefinitely.
            self.log("WARNING lot thieu guard khi khoi dong: %s -> "
                     "retry_protection dat lai" % missing_required)
            for position in self.state.get("positions", []):
                if self._missing_guards(position):
                    position["protection_status"] = "retrying"
                    position["protection_retry_at"] = 0
            missing_required = []
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
        if (self.state.get("halted") and self.state.get("halt_reason") in (
                "exchange protection reconciliation mismatch",
                "exchange protection reconciliation unavailable")):
            self.state["halted"] = False
            self.state["halt_reason"] = None
            self.log("RECOVERY: protection reconciliation OK - "
                     "tat ca SL/TP tren san khop voi state, tu dong unhalt. "
                     "Thoi gian halt: tu %s" %
                     self.state.get("halted_at", "unknown"))
        return True

    def _is_bot_algo(self, symbol, client_algo_id):
        """clientAlgoId format of _new_client_order_id: b<SYMBOL><L|S><n>."""
        raw = "".join(ch for ch in str(symbol).upper()
                      if ch.isalnum() or ch in "_-")[:18]
        return bool(re.fullmatch(r"b%s[LS]\d+" % re.escape(raw),
                                 str(client_algo_id or "")))

    def cleanup_orphan_orders(self):
        """Cancel bot-created Algo orders that no local lot references.

        Orphans come from guards whose lot is gone (Binance TP/SL are not
        OCO, external closes, old retry duplicates). The old rule only
        cleaned symbols with no position at all, so a grid symbol - which
        almost always has a position - accumulated orphans forever, and each
        one counts toward Binance's conditional-order limit.

        Safety rules: only ids/clientAlgoIds not referenced by any lot;
        only orders created by this bot (clientAlgoId pattern); and only on
        a Hedge leg whose exchange quantity is fully explained by local lots
        (an unmanaged position might rely on that order).  Returns count.
        """
        if self.dry_run:
            return 0
        try:
            rows = self._fetch_open_algo_orders()
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self.log("CLEANUP loi doc algo orders: %s" %
                     binance_safety.redact_body(e))
            return 0
        ref_ids, ref_cids = set(), set()
        for pos in self.state.get("positions", []):
            symbol = str(pos.get("symbol") or "").upper()
            for label in ("sl", "tp"):
                if pos.get("%s_algo_id" % label):
                    ref_ids.add((symbol, str(pos["%s_algo_id" % label])))
                if pos.get("%s_client_algo_id" % label):
                    ref_cids.add(str(pos["%s_client_algo_id" % label]))
        candidates = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            algo_id = row.get("algoId")
            client_algo_id = str(row.get("clientAlgoId") or "")
            if (symbol, str(algo_id)) in ref_ids or client_algo_id in ref_cids:
                continue
            if not self._is_bot_algo(symbol, client_algo_id):
                seen = getattr(self, "_foreign_algo_logged", None)
                if seen is None:
                    seen = self._foreign_algo_logged = set()
                if (symbol, str(algo_id)) not in seen:
                    seen.add((symbol, str(algo_id)))
                    self.log("CLEANUP giu nguyen algo %s %s (clientAlgoId=%s "
                             "khong do bot tao)" % (symbol, algo_id,
                                                    client_algo_id))
                continue
            candidates.append((symbol, algo_id, client_algo_id, row))
        if not candidates:
            return 0
        try:
            exchange = self._aggregate_positions(self._private_call(
                "private:account", self.ex.fetch_positions, _weight=5))
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self.log("CLEANUP loi doc vi the: %s" %
                     binance_safety.redact_body(e))
            return 0
        cleaned = 0
        for symbol, algo_id, client_algo_id, row in candidates:
            leg = str(row.get("positionSide") or "").lower()
            if leg in ("long", "short"):
                key = (symbol, leg)
                local = self._local_qty(symbol, leg)
                actual = exchange.get(key, 0.0)
                if abs(actual - local) > self._qty_tolerance(symbol, local):
                    self.log("CLEANUP hoan huy algo %s %s: leg %s san=%s "
                             "local=%s chua khop" % (symbol, algo_id, leg,
                                                     actual, local))
                    continue
            result = self._cancel_algo_quietly(symbol, algo_id,
                                               None if algo_id else
                                               client_algo_id)
            if result == "cancelled":
                cleaned += 1
                self.log("CLEANUP: da huy lenh mo coi %s algo=%s %s %s"
                         % (symbol, algo_id, row.get("orderType"),
                            row.get("positionSide")))
        return cleaned

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
        still = [p for p in self.state["positions"] if p.get("live")]
        pruned = [p for p in self.state["positions"] if not p.get("live")]
        exchange_keys = set(exchange)
        if pruned:
            self.log("WARNING loai bo %d vi the paper/dry khong phai lenh "
                     "that: ids=%s" % (len(pruned), [p["id"] for p in pruned]))
        gone = [p for p in still if (p["symbol"], p["side"]) not in exchange_keys]
        if gone:
            # Do NOT silently drop them: they were most likely closed by
            # their exchange TP/SL while the bot was down. Protection sync /
            # detect_exchange_closed book the real fill (PnL -> DB); until
            # then reconciliation holds new entries and auto-recovers.
            self.log("WARNING %d lot khong con tren san khi khoi dong: ids=%s "
                     "-> doi chieu qua sync/detect de ghi PnL"
                     % (len(gone), [p["id"] for p in gone]))
        self.state["positions"] = still
        local_keys = {(p["symbol"], p["side"]) for p in still}
        for key in sorted(exchange_keys - local_keys):
            self.log("CRITICAL san co vi the %s qty=%s ma state khong quan ly "
                     "-> halt de doi chieu/close tay" % (key, exchange[key]))
            self.state["halted"] = True
            self.state["halt_reason"] = "unmanaged exchange position"
        self.reconcile_positions(force=True, rows=rows)
        self._reconcile_startup_open_orders()
        # Orphans left by a previous run are cancelled (bot-owned, leg
        # explained by local lots) instead of halting as "unknown".
        if self.cfg.get("exchange_protection", False):
            try:
                cleaned = self.cleanup_orphan_orders()
                if cleaned:
                    self.log("STARTUP: da huy %d lenh mo coi" % cleaned)
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                self.log("WARNING startup orphan cleanup: %s" %
                         binance_safety.redact_body(exc))
        self._reconcile_exchange_protection(rows)
        # vua doi chieu xong: lan kiem tra lai dau tien sau 1 chu ky
        self._last_hold_recheck = time.time()

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
            fee = self._fees(qty * entry)   # thay bang phi that ben duoi
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
                    self._create_exchange_protection(pos)
                    pos["protection_status"] = "armed"
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as protection_error:
                    # Chan da dat thanh cong duoc GIU; chi chan thieu duoc
                    # retry (retry_protection). Thieu SL qua deadline -> dong.
                    self._schedule_protection_retry(pos, protection_error)
            if not self.dry_run:
                self._record_entry_fee(pos)
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

    def _schedule_protection_retry(self, pos, error=None, delay=10.0):
        now = time.time()
        pos["protection_status"] = "retrying"
        pos["protection_retry_at"] = now + delay
        if "sl" in self._missing_guards(pos):
            pos.setdefault("protection_deadline", now + float(
                self.cfg.get("protection_sl_deadline_seconds", 120)))
        else:
            pos.pop("protection_deadline", None)
        if error is not None:
            pos["protection_error"] = binance_safety.redact_body(error)
            self.log("WARNING PROTECTION #%s %s thieu %s: %s -> thu lai sau %.0fs"
                     % (pos.get("id"), pos.get("symbol"),
                        "/".join(l.upper() for l in self._missing_guards(pos)),
                        pos["protection_error"], delay))

    def retry_protection(self, arm_only=False):
        """Arm every missing guard; returns True when state changed.

        arm_only=True (bot dang reconcile hold - state lech san): CHI dat
        chan thieu (lenh dong-only trong Hedge Mode, luon giam rui ro), KHONG
        dong cuong buc theo deadline vi qty local co the sai.

        Covers new lots whose first attempt failed, guards reported lost by
        sync_exchange_protection (cancelled/expired/rejected on Binance),
        lots restored without ids and old lots (backfill).  Only missing legs
        are placed.  A lot WITHOUT a stop-loss past the deadline is closed;
        a lot missing only its TP keeps retrying (the bot's own TP check
        still covers it).
        """
        if self.dry_run or not self.cfg.get("exchange_protection", False):
            return False
        now = time.time()
        changed = False
        for pos in list(self.state.get("positions", [])):
            if not pos.get("live"):
                continue
            missing = self._missing_guards(pos)
            if not missing:
                if pos.get("protection_status") != "armed":
                    pos["protection_status"] = "armed"
                    pos.pop("protection_retry_at", None)
                    pos.pop("protection_deadline", None)
                    pos.pop("protection_error", None)
                    changed = True
                continue
            if pos.get("protection_status") == "armed":
                # Lost a leg after being armed (or restored without ids).
                self._schedule_protection_retry(pos, delay=0)
                changed = True
            if pos.get("protection_status") is None:
                self.log("Backfill protection cho #%s %s" %
                         (pos["id"], pos["symbol"]))
                self._schedule_protection_retry(pos, delay=0)
                changed = True
            deadline = pos.get("protection_deadline")
            if ("sl" in missing and deadline and now >= deadline
                    and arm_only):
                if not pos.get("_hold_sl_warned"):
                    self.log("CRITICAL PROTECTION #%s %s thieu SL qua deadline "
                             "trong luc reconcile hold -> KHONG tu dong (state "
                             "lech san), tiep tuc thu dat SL; can kiem tra tay"
                             % (pos["id"], pos["symbol"]))
                    pos["_hold_sl_warned"] = True
                    changed = True
            elif "sl" in missing and deadline and now >= deadline:
                self.log("CRITICAL PROTECTION #%s %s khong dat duoc SL sau "
                         "deadline -> dong vi the" % (pos["id"], pos["symbol"]))
                rec = None
                try:
                    rec = self.close(pos, pos["entry"], "PROTECTION_FAILED")
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    self.log("CRITICAL khong dong duoc vi the khong SL: %s" % e)
                if rec:
                    if getattr(self, "_pending_close_recs", None) is None:
                        self._pending_close_recs = []
                    self._pending_close_recs.append(rec)
                else:
                    self.state["halted"] = True
                    self.state["halt_reason"] = "unprotected position cannot close"
                changed = True
                continue
            if now < pos.get("protection_retry_at", 0):
                continue
            try:
                self._create_exchange_protection(pos)
                pos["protection_status"] = "armed"
                pos.pop("protection_retry_at", None)
                pos.pop("protection_deadline", None)
                pos.pop("protection_error", None)
                pos.pop("_hold_sl_warned", None)
                self.log("PROTECTION #%s %s du SL/TP tren san (sl=%s tp=%s)"
                         % (pos["id"], pos["symbol"], pos.get("sl_algo_id"),
                            pos.get("tp_algo_id")))
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as e:
                self._schedule_protection_retry(
                    pos, e, delay=10.0 if "sl" in self._missing_guards(pos)
                    else 30.0)
            changed = True
        return changed

    def drain_close_records(self):
        """Close records produced outside the caller's own close() calls."""
        recs = getattr(self, "_pending_close_recs", None) or []
        self._pending_close_recs = []
        return recs

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
            # Other local lots on the same Hedge leg stay open; the exchange
            # aggregate after this close must still contain them.
            remaining_expected = self._local_qty(symbol, side,
                                                 exclude_id=pos.get("id"))
            self._cancel_exchange_protection(pos)
            bside = "sell" if side == "long" else "buy"
            ps = "LONG" if side == "long" else "SHORT"
            if self.dry_run:
                qty = pos["qty"]
            else:
                filters = self._filters_for(symbol)
                if filters is None:
                    raise RuntimeError("unknown_symbol: %s" % symbol)
                step = filters[0]
                # Dong DUNG qty cua lot (da hop le stepSize luc mo). Truoc day
                # qty_for(qty*price, price) lam tron XUONG sau phep nhan/chia
                # float -> ~16% lan dong hut 1 step, de lai bui khong TP/SL
                # tren san; bui tich luy -> reconcile mismatch -> halt.
                qty = normalize_qty(pos["qty"], step)
                if qty <= 0:
                    raise RuntimeError("close qty khong hop le: %s"
                                       % pos["qty"])
                if remaining_expected <= 1e-12:  # khong con lot nao khac
                    qty = self._sweep_leg_dust(symbol, side, qty, step)
            oid, client_order_id = self._place_market(
                symbol, bside, qty, ps, reduce_only=True, ref_price=price
            )
            ex = self._fill_price(symbol, oid, price)
            # Verify vi the da dong that tren san truoc khi xoa khoi state.
            # Neu partial fill -> giu lai de retry, khong danh dau da dong.
            if not self.dry_run:
                import time as _t
                _t.sleep(2)
                actual = self._aggregate_positions(
                    self._private_call("private:account",
                                       self.ex.fetch_positions, _weight=5)
                ).get((symbol, side), 0)
                # Cho phep sai so nho do lam tron. San gop moi lot cung
                # (symbol, side): sau khi dong, phan con lai phai bang tong
                # cac lot khac, khong phai 0.
                tolerance = max(self._qty_tolerance(symbol, pos["qty"]),
                                float(pos["qty"]) * 0.01)
                if actual > remaining_expected + tolerance:
                    raise RuntimeError(
                        f"close partial: san con {actual}, cac lot khac "
                        f"{remaining_expected}, lot nay {pos['qty']} "
                        f"-> se retry")
                if actual < remaining_expected - tolerance:
                    self.log("WARNING close #%s: san con %s < cac lot khac %s; "
                             "reconcile/sync se doi chieu" %
                             (pos["id"], actual, remaining_expected))
            booking = self._book_exit(pos, ex, oid)
            self._apply_booking(booking)
            net = booking["net"]
            rec = {
                "id": pos["id"], "symbol": symbol, "side": side,
                "tag": pos["tag"], "entry": round(pos["entry"], 6),
                "exit": round(ex, 6), "notional": round(pos["notional"], 2),
                "reason": reason,
                "closed_at": int(time.time()),
                "live": True, "dry": self.dry_run, "close_ord": oid,
                "close_client_order_id": client_order_id,
                "exit_source": "bot", "estimated": False,
                **self._booking_fields(booking),
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
        except GuardAlreadyFilled as filled:
            rec = self._finalize_exchange_close(pos, filled.label,
                                                filled.outcome)
            if rec:
                self._mark_action_success(key)
                self.log("CLOSE #%s %s: guard %s da khop tren san truoc "
                         "(yeu cau %s) -> ghi nhan fill san"
                         % (pos["id"], symbol, filled.label.upper(), reason))
            return rec
        except GuardInFlight as busy:
            # Do not halt: the guard is closing the lot right now and the
            # protection sync books it within seconds.
            self._mark_action_failure(key, busy)
            return None
        except Exception as e:
            # A failed close can leave exchange exposure while local state
            # still says the position is open. Do not merely cooldown and let
            # a future risk day resume entries around that unknown exposure.
            self.state["halted"] = True
            self.state["halt_reason"] = "close action requires reconciliation"
            self._mark_action_failure(key, e)
            return None

    def _sweep_leg_dust(self, symbol, side, qty, step):
        """Lot cuoi cua leg: neu san con du <= 2 step (bui tu lan dong cu),
        dong luon phan du trong cung lenh. Khong bao gio dong phan lon hon
        (co the la vi the nguoi dung tu mo). Loi doc san -> giu qty lot."""
        try:
            leg = self._aggregate_positions(self._private_call(
                "private:account", self.ex.fetch_positions, _weight=5)
            ).get((symbol, side), 0.0)
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.log("WARNING close %s %s: khong doc duoc leg truoc khi dong "
                     "(%s) -> dong dung qty lot" %
                     (symbol, side, binance_safety.redact_body(exc)))
            return qty
        leg = normalize_qty(leg, step)
        dust = leg - qty
        if 0 < dust <= float(step) * 2 + 1e-12:
            self.log("CLEANUP close %s %s: san con %s, lot %s -> dong ca bui "
                     "%s" % (symbol, side, leg, qty, normalize_qty(dust, step)))
            return leg
        return qty

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
