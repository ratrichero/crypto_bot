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

    def __init__(self, cfg, state, dry_run=False, log=None):
        self.cfg = cfg
        self.state = state
        self.dry_run = dry_run
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
        return binance_safety.call_private(
            endpoint,
            fn,
            *args,
            exchange=self.ex,
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
            return dict(self._order_events[key])

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
            # MARKET_LOT_SIZE filter; fall back to LOT_SIZE for symbols that
            # do not publish the market-specific filter.
            selected = market_lot or lot
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
                    "priceProtect": (
                        "TRUE" if self.cfg.get("protection_price_protect", False)
                        else "FALSE"
                    ),
                }
                response = self._private_call(
                    "private:trade",
                    self.ex.fapiPrivatePostAlgoOrder,
                    params,
                )
                algo_id = response.get("algoId")
                if not algo_id:
                    raise RuntimeError("Binance algo order missing algoId")
                orders[label] = algo_id
            return orders
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception:
            # If the second protection order fails, remove the first one so
            # the position is not left with only half of its intended guard.
            for algo_id in orders.values():
                if algo_id:
                    try:
                        self._private_call(
                            "private:trade",
                            self.ex.fapiPrivateDeleteAlgoOrder,
                            {"symbol": pos["symbol"], "algoId": algo_id},
                        )
                    except binance_safety.BinanceSafetyStop:
                        raise
                    except Exception as cancel_error:
                        self.log("CRITICAL protection cleanup failed algo=%s: %s"
                                 % (algo_id, binance_safety.redact_body(cancel_error)))
            raise

    def _cancel_exchange_protection(self, pos):
        if self.dry_run:
            return
        if (not self.cfg.get("exchange_protection", False)
                and not pos.get("sl_algo_id") and not pos.get("tp_algo_id")):
            return
        for key in ("sl_algo_id", "tp_algo_id"):
            algo_id = pos.get(key)
            if not algo_id:
                continue
            try:
                self._private_call(
                    "private:trade",
                    self.ex.fapiPrivateDeleteAlgoOrder,
                    {"symbol": pos["symbol"], "algoId": algo_id},
                )
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                # An already-triggered/canceled algo order is harmless. Any
                # other error is logged; the market close remains idempotent.
                self.log("WARNING cancel protection algo=%s failed: %s"
                         % (algo_id, binance_safety.redact_body(exc)))

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
        )

    def get_balance_usdt(self):
        """So du USDT futures (read-only)."""
        if self.dry_run:
            eq = self.state.get("equity", 0.0)
            return {"total": eq, "free": eq - self.used_margin()}
        bal = self._private_call(
            "private:account",
            self.ex.fetch_balance,
        )
        u = bal.get("USDT", {})
        return {"total": float(u.get("total", 0) or 0),
                "free": float(u.get("free", 0) or 0)}

    def refresh_equity(self):
        if self.dry_run:
            return
        try:
            self.state["equity"] = self.get_balance_usdt()["total"]
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self.log("WARNING refresh_equity that bai: %s (giu equity cu)" % e)

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
            exchange = self._aggregate_positions(
                self.get_positions() if rows is None else rows
            )
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
            self.log("CRITICAL POSITION RECONCILE mismatch=%s; halt new entries"
                     % mismatches)
            return False
        return True

    def _reconcile_startup(self):
        """Remove paper ghosts, then validate aggregate exchange quantities."""
        try:
            rows = self.get_positions()
            exchange = self._aggregate_positions(rows)
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self.log("WARNING khong doc duoc vi the san de doi chieu: %s" % e)
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
                    self.log("ORDER reconciled after transport failure "
                             "clientOrderId=%s orderId=%s"
                             % (client_order_id, existing.get("orderId")))
                    self._last_order_response = existing
                    return existing.get("orderId"), client_order_id
            raise RuntimeError("dat lenh %s %s that bai: %s"
                               % (symbol, side, e))
        self._last_order_response = od
        return od.get("id"), od.get("clientOrderId") or client_order_id

    def _fill_price(self, symbol, order_id, ref_price):
        if self.dry_run:
            return ref_price

        response = self._last_order_response or {}
        self._last_order_response = None
        if response.get("average") and float(response["average"] or 0) > 0:
            return float(response["average"])
        if response.get("avgPrice") and float(response["avgPrice"] or 0) > 0:
            return float(response["avgPrice"])

        # Prefer the ordered private stream. This removes the old six-request
        # polling burst when ORDER_TRADE_UPDATE is healthy.
        if self._user_ws is not None and self._user_ws.running:
            event_order = self._wait_order_event(
                order_id,
                float(self.cfg.get("order_event_timeout_seconds", 8)),
            )
            if event_order:
                avg = event_order.get("ap") or event_order.get("avgPrice")
                if avg and float(avg) > 0:
                    return float(avg)

        ccxt_symbol = self._ccxt_symbol(symbol)
        if not ccxt_symbol:
            raise RuntimeError("unknown_symbol: %s" % symbol)
        px = None
        # REST is now a bounded fallback, not the normal order-status path.
        for _ in range(2):
            try:
                od = self._private_call(
                    "private:order_status",
                    self.ex.fetch_order,
                    order_id,
                    ccxt_symbol,
                )
                if od.get("average"):
                    px = float(od["average"])
                if od.get("status") == "closed":
                    break
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as e:
                self.log("WARNING poll order %s: %s" % (order_id, e))
            time.sleep(1)
        if px is None:
            self.log("WARNING khong lay duoc avgPx cho %s, dung gia ref %s"
                     % (order_id, ref_price))
            return ref_price
        return px

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
            if (total_notional + notional
                    > self.state["equity"] * self.cfg["risk"]["max_notional_mult"]):
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
                    # Keep the already-created position in state, stop new
                    # entries, and let local SL/TP remain the fallback.
                    pos["protection_status"] = "failed"
                    pos["protection_error"] = binance_safety.redact_body(
                        protection_error
                    )
                    self.state["halted"] = True
                    self.state["halt_reason"] = (
                        "exchange protection failed for %s" % symbol
                    )
                    self.log("CRITICAL UNPROTECTED position #%s %s: %s" %
                             (pos["id"], symbol,
                              binance_safety.redact_body(protection_error)))
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
            if not self.dry_run:
                self.refresh_equity()
            return rec
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
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
