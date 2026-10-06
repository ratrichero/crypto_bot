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
  3. SL/TP like the OKX bot: NO exchange-side conditional orders. The fast
     loop (~0.5s) watches WS prices and closes with reduce-only market
     orders. Same trade-off: if this process dies, no protective stop rests
     on the exchange. Mitigations: watchdog/systemd restart, daily stop,
     small sizes. Add exchange-side stops before scaling up.

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
        self._filters = {}   # symbol -> (step_size, min_qty, min_notional)
        self._lev_done = set()
        self._dry_n = 0
        # Failure state is keyed by symbol/order action.  A transient API or
        # validation error must not be retried by the 0.5s market loop.
        self._action_failures = {}
        self._action_cooldowns = {}
        self._symbol_cooldowns = {}
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
                "options": {"defaultType": "future"},
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
            markets = self._private_call(
                "private:exchange_info",
                self.ex.load_markets,
            )
            if symbol not in markets:
                # Coin bi delist / khong co tren futures -> bo qua, khong crash
                self._filters[symbol] = None
                return None
            m = markets[symbol]
            step, minq, minn = 0.0, 0.0, 0.0
            for f in m["info"].get("filters", []):
                ft = f.get("filterType")
                if ft == "LOT_SIZE":
                    step = float(f["stepSize"])
                    minq = float(f["minQty"])
                elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                    minn = float(f.get("notional", f.get("minNotional", 0)))
            if not step:
                raise RuntimeError("khong doc duoc LOT_SIZE cho %s" % symbol)
            self._filters[symbol] = (step, minq, minn)
        return self._filters[symbol]

    def _set_leverage(self, symbol):
        if symbol in self._lev_done:
            return
        if self.dry_run:
            self.log("DRY_RUN set-leverage %s lev=%s cross" %
                     (symbol, self.cfg["leverage"]))
        else:
            try:
                self._private_call(
                    "private:trade",
                    self.ex.set_margin_mode,
                    "cross",
                    symbol,
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
                symbol,
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

    def _reconcile_startup(self):
        """Loai paper-ghost khoi state; canh bao vi the san khong quan ly."""
        try:
            ex = set()
            for p in self.get_positions():
                amt = float(p.get("contracts", 0) or 0)
                if amt == 0:
                    continue
                ex.add((p.get("symbol"), p.get("side")))
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self.log("WARNING khong doc duoc vi the san de doi chieu: %s" % e)
            return
        kept = [p for p in self.state["positions"] if p.get("live")]
        pruned = [p for p in self.state["positions"] if not p.get("live")]
        still = []
        for p in kept:
            if (p["symbol"], p["side"]) in ex:
                still.append(p)
            else:
                pruned.append(p)
        if pruned:
            self.log("WARNING loai bo %d vi the khong ton tai tren san: ids=%s"
                     % (len(pruned), [p["id"] for p in pruned]))
        self.state["positions"] = still
        for k in sorted(ex - {(p["symbol"], p["side"]) for p in still}):
            self.log("WARNING san co vi the %s ma state khong quan ly "
                     "-> tu dong tay hoac xoa state.json" % (k,))

    def _place_market(self, symbol, side, qty, position_side,
                      reduce_only=False, ref_price=None):
        """side: 'buy'/'sell'. Returns (order_id, fill_price_or_None)."""
        params = {"positionSide": position_side}
        if reduce_only:
            params["reduceOnly"] = True
        if self.dry_run:
            self._dry_n += 1
            oid = "dryrun-%d" % self._dry_n
            self.log("DRY_RUN dat lenh: %s %s qty=%s %s" %
                     (symbol, side, qty, json.dumps(params)))
            return oid, ref_price
        fn = (self.ex.create_market_buy_order if side == "buy"
              else self.ex.create_market_sell_order)
        try:
            od = self._private_call(
                "private:trade",
                fn,
                symbol,
                qty,
                params,
            )
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            raise RuntimeError("dat lenh %s %s that bai: %s"
                               % (symbol, side, e))
        return od.get("id"), None

    def _fill_price(self, symbol, order_id, ref_price):
        if self.dry_run:
            return ref_price
        px = None
        for _ in range(6):
            try:
                od = self._private_call(
                    "private:order_status",
                    self.ex.fetch_order,
                    order_id,
                    symbol,
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
            oid, _ = self._place_market(symbol, bside, qty, ps,
                                        ref_price=price)
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
            }
            self.state["equity"] -= fee
            self.state["stats"]["fees"] += fee
            self.state["positions"].append(pos)
            self._mark_action_success(key)
            self.log("%s OPEN #%d %s %s entry=%s sl=%s tp=%s ord=%s" %
                     ("DRY_RUN" if self.dry_run else "LIVE",
                      pos["id"], symbol, side, entry, sl, tp, oid))
            return pos, "ok"
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            self._mark_action_failure(key, e)
            return None, "action_failed: %s" % e

    # ---------------------------------------------------------------- close
    def close(self, pos, price, reason):
        """Dong bang market reduce-only, with per-position cooldown."""
        symbol = pos["symbol"]
        side = pos["side"]
        key = self._action_key("close", symbol, side, pos.get("tag"),
                               pos.get("id"))
        cooldown = self._cooldown_reason(key)
        if cooldown:
            return None
        try:
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
            oid, _ = self._place_market(symbol, bside, qty, ps,
                                        reduce_only=True, ref_price=price)
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
