"""Entry LIMIT post-only (GTX) cho grid (G5) - mixin cua BinanceEngine.

Vong doi 1 lenh cho (state["entry_orders"], persist cung state bot):

  SUBMITTING --(POST ok)--> NEW --> PARTIALLY_FILLED --> FILLED
      |                      |            |
      |  loi mang (mo ho)    |  TTL / huy |  qua partial_fill_timeout
      v                      v            v
   UNKNOWN --(GET theo cid)--+--> CANCELED / EXPIRED (co the da khop 1 phan)

- Ghi state TRUOC khi gui (persist_cb) -> crash giua chung van tra lai duoc
  bang clientOrderId (e<SYM><L|S><n>), khong bao gio gui lai mu.
- GTX bi tu choi vi se khop ngay (EXPIRED / -5022) -> bo lenh + cooldown.
- Ket thuc (FILLED / CANCELED / EXPIRED) voi qty da khop > 0 -> 1 lot that
  qua _register_lot (dat SL/TP tren san ngay). Duoi minNotional -> dong ngay.
- Lenh cho CHIEM slot (max_total_positions) va exposure (max_notional_mult)
  ngay khi dat, de du khop het van khong vuot tran.
- Phan da khop cua lenh chua ket thuc da nam tren san nhung chua thanh lot:
  _local_qty / reconcile cong them, detect_exchange_closed tru di.
- Trang thai: uu tien WS ORDER_TRADE_UPDATE (field c/X/z/ap/i); REST GET
  order chi poll toi da 1 lenh/vong (governor gian order_status 1s).
"""
from __future__ import annotations

import re
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import binance_safety

TERMINAL = ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")
OPEN = ("NEW", "PARTIALLY_FILLED")


def _f(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _dec_str(value) -> str:
    d = Decimal(str(value)).normalize()
    return format(d, "f")


class EntryOrdersMixin:
    # ------------------------------------------------------------ state
    def entry_orders(self):
        return self.state.setdefault("entry_orders", [])

    def _entry_cfg(self, key, default):
        return (self.cfg.get("grid") or {}).get(key, default)

    def _persist(self):
        cb = getattr(self, "persist_cb", None)
        if cb is not None:
            try:
                cb()
            except Exception as exc:     # pragma: no cover - log only
                self.log("WARNING persist entry_orders: %s" % exc)

    def pending_entries(self, symbol=None):
        return [o for o in self.entry_orders()
                if symbol is None or o.get("symbol") == symbol]

    def pending_entry_notional(self):
        return sum(_f(o.get("notional")) for o in self.entry_orders())

    def _pending_filled_qty(self, symbol, side):
        return sum(_f(o.get("filled")) for o in self.entry_orders()
                   if o.get("symbol") == symbol and o.get("side") == side)

    def _minus_pending_fills(self, exchange):
        """Leg san tru phan da khop cua lenh entry chua ket thuc."""
        out = dict(exchange)
        for o in self.entry_orders():
            key = (o.get("symbol"), o.get("side"))
            filled = _f(o.get("filled"))
            if filled > 0 and key in out:
                out[key] = max(0.0, out[key] - filled)
        return out

    def _new_entry_client_id(self, symbol, side):
        self._client_nonce += 1
        self.state["_client_nonce"] = self._client_nonce
        raw = "".join(ch for ch in str(symbol).upper()
                      if ch.isalnum() or ch in "_-")
        return ("e%s%s%s" % (raw[:18], "L" if side == "long" else "S",
                             self._client_nonce))[:36]

    def _is_bot_entry(self, symbol, client_order_id):
        raw = "".join(ch for ch in str(symbol).upper()
                      if ch.isalnum() or ch in "_-")[:18]
        return bool(re.fullmatch(r"e%s[LS]\d+" % re.escape(raw),
                                 str(client_order_id or "")))

    def _entry_price(self, symbol, price, side):
        """Lam tron ve tick: BUY xuong, SELL len (khong bao gio lam lenh
        post-only cat qua gia)."""
        tick = 0.0
        if not self.dry_run:
            if symbol not in self._price_ticks:
                self._filters_for(symbol)
            tick = self._price_ticks.get(symbol) or 0.0
        if not tick:
            return float(price), _dec_str(price)
        step = Decimal(str(tick))
        rounding = ROUND_FLOOR if side == "long" else ROUND_CEILING
        value = (Decimal(str(price)) / step).to_integral_value(
            rounding=rounding) * step
        return float(value), _dec_str(value)

    # ------------------------------------------------------------ place
    def place_entry_limit(self, symbol, side, notional, price, sl_pct,
                          tp_pct, tag, level=None):
        """Dat 1 lenh LIMIT GTX. Tra ve (rec, reason); rec None = khong dat."""
        from live_binance import qty_for
        key = self._action_key("entry", symbol, side, tag, level)
        why = self._symbol_cooldown_reason(symbol) or self._cooldown_reason(key)
        if why:
            return None, why
        if self.state.get("halted"):
            return None, "halted: %s" % self.state.get("halt_reason", "")
        if (len(self.state["positions"]) + len(self.entry_orders())
                >= self.cfg.get("max_total_positions", 999)):
            return None, "max_positions"
        if any(o.get("symbol") == symbol and o.get("level") == level
               and level is not None for o in self.entry_orders()):
            return None, "duplicate_level"
        try:
            free = self.get_balance_usdt()["free"]
            if free < notional / self.cfg["leverage"]:
                self._mark_action_failure(key, "insufficient_margin")
                return None, "insufficient_margin"
            risk_equity = _f(self.state.get("mark_equity",
                                            self.state.get("equity", 0.0))
                             or self.state.get("equity", 0.0))
            used = (sum(p["notional"] for p in self.state["positions"])
                    + self.pending_entry_notional())
            if used + notional > risk_equity * self.cfg["risk"][
                    "max_notional_mult"]:
                self._mark_action_failure(key, "exposure_cap")
                return None, "exposure_cap"
            self._set_leverage(symbol)
            px, px_str = self._entry_price(symbol, price, side)
            if self.dry_run:
                step = 0.000001
                qty = qty_for(notional, px, step)
            else:
                filters = self._filters_for(symbol)
                if filters is None:
                    self._mark_action_failure(key, "unknown_symbol")
                    return None, "unknown_symbol"
                step, minq, minn = filters
                qty = qty_for(notional, px, step, minq, minn)
            if qty is None:
                self._mark_action_failure(key, "size_too_small")
                return None, "size_too_small"
            now = time.time()
            rec = {"cid": self._new_entry_client_id(symbol, side),
                   "symbol": symbol, "side": side, "level": level, "tag": tag,
                   "price": px, "qty": qty, "notional": qty * px,
                   "sl_pct": sl_pct, "tp_pct": tp_pct,
                   "status": "SUBMITTING", "order_id": None, "filled": 0.0,
                   "avg": None, "created": now, "updated": now,
                   "dry": bool(self.dry_run)}
            self.entry_orders().append(rec)
            self._persist()          # ghi TRUOC khi gui
            if self.dry_run:
                self._dry_n += 1
                rec.update(status="NEW", order_id="dry-entry-%d" % self._dry_n)
                self.log("DRY_RUN dat LIMIT GTX %s %s %s qty=%s @ %s cid=%s"
                         % (symbol, side, level, qty, px_str, rec["cid"]))
                self._mark_action_success(key)
                return rec, "ok"
            params = {
                "symbol": symbol, "side": "BUY" if side == "long" else "SELL",
                "positionSide": "LONG" if side == "long" else "SHORT",
                "type": "LIMIT", "timeInForce": "GTX",
                "quantity": _dec_str(qty), "price": px_str,
                "newClientOrderId": rec["cid"], "newOrderRespType": "RESULT",
            }
            try:
                od = self._private_call("private:trade",
                                        self.ex.fapiPrivatePostOrder, params)
            except binance_safety.BinanceSafetyStop:
                raise
            except Exception as exc:
                text = str(exc)
                low = text.lower()
                if any(w in low for w in ("timeout", "timed out", "network",
                                          "connection")):
                    rec["status"] = "UNKNOWN"
                    rec["updated"] = time.time()
                    self.log("WARNING lenh LIMIT %s %s khong ro da vao san "
                             "(cid=%s) -> tra lai theo clientOrderId"
                             % (symbol, level, rec["cid"]))
                    return None, "entry_unknown"
                self._drop_entry(rec)
                self._mark_action_failure(key, exc)
                if "-5022" in text:
                    return None, "post_only_rejected"
                self.log("ENTRY LIMIT %s %s bi tu choi: %s" % (
                    symbol, level, binance_safety.redact_body(exc)))
                return None, "entry_rejected"
            self._apply_entry_update(rec, od or {})
            if rec["status"] in ("EXPIRED", "EXPIRED_IN_MATCH") \
                    and _f(rec["filled"]) <= 0:
                # GTX se khop ngay (gia da chay qua) -> bo, cooldown
                self._drop_entry(rec)
                self._mark_action_failure(key, "post_only_expired")
                return None, "post_only_expired"
            if rec["status"] == "SUBMITTING":
                rec["status"] = "NEW"
            self._mark_action_success(key)
            self.log("LIVE dat LIMIT GTX %s %s %s qty=%s @ %s cid=%s ord=%s"
                     % (symbol, side, level, qty, px_str, rec["cid"],
                        rec["order_id"]))
            return rec, "ok"
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self._mark_action_failure(key, exc)
            return None, "action_failed: %s" % exc

    def _drop_entry(self, rec):
        orders = self.entry_orders()
        if rec in orders:
            orders.remove(rec)

    def _apply_entry_update(self, rec, data):
        """Cap nhat tu REST (status/executedQty/avgPrice/orderId) hoac WS
        (X/z/ap/i). Qty da khop chi tang (event cu den tre khong lam giam)."""
        status = str(data.get("status") or data.get("X") or "").upper()
        if status == "CANCELLED":
            status = "CANCELED"
        filled = _f(data.get("executedQty", data.get("z")), -1.0)
        avg = _f(data.get("avgPrice", data.get("ap")))
        oid = data.get("orderId", data.get("i"))
        if oid not in (None, ""):
            rec["order_id"] = str(oid)
        if filled >= 0 and filled >= _f(rec.get("filled")):
            rec["filled"] = filled
        if avg > 0:
            rec["avg"] = avg
        if status and rec.get("status") not in TERMINAL:
            rec["status"] = status
        if rec["status"] == "PARTIALLY_FILLED" and not rec.get("partial_since"):
            rec["partial_since"] = time.time()
        rec["updated"] = time.time()

    def on_entry_event(self, order):
        """Goi tu on_user_event (thread WS) cho ORDER_TRADE_UPDATE."""
        cid = str(order.get("c") or "")
        if not cid.startswith("e"):
            return
        with self._order_condition:
            events = getattr(self, "_entry_events", None)
            if events is None:
                events = self._entry_events = {}
            events[cid] = dict(order)
            if len(events) > 2048:
                for _ in range(512):
                    events.pop(next(iter(events)))

    # ------------------------------------------------------------ query
    def _query_entry(self, rec):
        """GET order theo clientOrderId. dict | None (khong ton tai) |
        False (loi khac - thu lai sau)."""
        try:
            return self._private_call(
                "private:order_status", self.ex.fapiPrivateGetOrder,
                {"symbol": rec["symbol"], "origClientOrderId": rec["cid"]})
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            text = str(exc)
            if "-2013" in text or "order does not exist" in text.lower():
                return None
            self.log("WARNING query entry %s: %s" % (
                rec["cid"], binance_safety.redact_body(exc)))
            return False

    def cancel_entry(self, rec, reason):
        """Huy 1 lenh cho. -2011 (khong con tren san) -> GET lai de biet da
        khop chua; ket qua duoc chot o sync_entry_orders."""
        if rec.get("status") in TERMINAL:
            return True
        now = time.time()
        if now - _f(rec.get("cancel_tried")) < 5:
            return False
        rec["cancel_tried"] = now
        rec.setdefault("cancel_reason", reason)
        if self.dry_run:
            rec["status"] = "CANCELED"
            self.log("DRY_RUN huy LIMIT %s %s (%s)" % (rec["symbol"],
                                                       rec["level"], reason))
            return True
        try:
            od = self._private_call(
                "private:trade", self.ex.fapiPrivateDeleteOrder,
                {"symbol": rec["symbol"], "origClientOrderId": rec["cid"]})
            self._apply_entry_update(rec, od or {})
            if rec["status"] not in TERMINAL:
                rec["status"] = "CANCELED"
            self.log("HUY LIMIT %s %s cid=%s (%s) da khop=%s" % (
                rec["symbol"], rec["level"], rec["cid"], reason,
                rec["filled"]))
            return True
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            text = str(exc)
            if "-2011" in text or "unknown order" in text.lower():
                data = self._query_entry(rec)
                if data:
                    self._apply_entry_update(rec, data)
                elif data is None:
                    rec["status"] = "UNKNOWN"
                return rec.get("status") in TERMINAL
            self.log("WARNING huy LIMIT %s that bai: %s" % (
                rec["cid"], binance_safety.redact_body(exc)))
            return False

    def cancel_entries(self, reason, symbol=None, keep=()):
        n = 0
        keep = set(keep)
        for rec in list(self.pending_entries(symbol)):
            if rec["cid"] in keep:
                continue
            if self.cancel_entry(rec, reason):
                n += 1
        return n

    # ------------------------------------------------------------- sync
    def sync_entry_orders(self, prices=None, force_poll=False):
        """Cap nhat moi lenh cho; tra ve danh sach lot moi da dang ky."""
        orders = self.entry_orders()
        if not orders:
            return []
        now = time.time()
        prices = prices or {}
        events = getattr(self, "_entry_events", None) or {}
        for rec in orders:
            with self._order_condition:
                ev = events.pop(rec["cid"], None)
            if ev:
                self._apply_entry_update(rec, ev)
        if self.dry_run:
            for rec in orders:
                px = prices.get(rec["symbol"])
                if rec["status"] in OPEN and px is not None and (
                        (rec["side"] == "long" and px <= rec["price"])
                        or (rec["side"] == "short" and px >= rec["price"])):
                    rec.update(status="FILLED", filled=rec["qty"],
                               avg=rec["price"], updated=now)
        else:
            poll_every = float(self._entry_cfg("entry_poll_seconds", 20))
            due = [r for r in orders if r["status"] not in TERMINAL and (
                force_poll or r["status"] in ("UNKNOWN", "SUBMITTING")
                or now - _f(r.get("polled"), _f(r.get("created")))
                >= poll_every)]
            due.sort(key=lambda r: _f(r.get("polled")))
            for rec in due if force_poll else due[:1]:
                if (rec["status"] == "SUBMITTING" and not force_poll
                        and now - _f(rec.get("created")) < 5):
                    continue
                rec["polled"] = now
                data = self._query_entry(rec)
                if data:
                    self._apply_entry_update(rec, data)
                    if rec["status"] in ("UNKNOWN", "SUBMITTING"):
                        rec["status"] = "NEW"
                elif data is None:
                    expire = float(self._entry_cfg(
                        "entry_unknown_expire_seconds", 60))
                    if now - _f(rec.get("created")) >= expire:
                        self.log("ENTRY %s cid=%s khong ton tai tren san sau "
                                 "%ds -> bo" % (rec["symbol"], rec["cid"],
                                                int(expire)))
                        rec["status"] = "EXPIRED"
                    elif rec["status"] == "SUBMITTING":
                        rec["status"] = "UNKNOWN"
        ttl = float(self._entry_cfg("entry_ttl_minutes", 60)) * 60
        pto = float(self._entry_cfg("partial_fill_timeout_seconds", 60))
        for rec in orders:
            if rec["status"] not in OPEN:
                continue
            if ttl > 0 and now - _f(rec.get("created")) >= ttl:
                self.cancel_entry(rec, "ttl")
            elif (rec["status"] == "PARTIALLY_FILLED" and pto > 0
                  and now - _f(rec.get("partial_since"), now) >= pto):
                self.cancel_entry(rec, "partial_timeout")
        new = []
        for rec in [r for r in orders if r["status"] in TERMINAL]:
            self._drop_entry(rec)
            if _f(rec.get("filled")) > 0:
                pos = self._finalize_entry_fill(rec)
                if pos:
                    new.append(pos)
            else:
                self.log("ENTRY %s %s cid=%s ket thuc %s, khong khop" % (
                    rec["symbol"], rec["level"], rec["cid"], rec["status"]))
        return new

    def _finalize_entry_fill(self, rec):
        """Lenh ket thuc co qty khop -> lot that (SL/TP tren san ngay)."""
        from live_binance import normalize_qty
        symbol, side = rec["symbol"], rec["side"]
        qty = _f(rec["filled"])
        entry = _f(rec.get("avg")) or _f(rec["price"])
        minn = 0.0
        if not self.dry_run:
            filters = self._filters_for(symbol)
            if filters:
                qty = normalize_qty(qty, filters[0])
                minn = float(filters[2] or 0)
        try:
            pos = self._register_lot(symbol, side, qty, entry, qty * entry,
                                     rec["sl_pct"], rec["tp_pct"], rec["tag"],
                                     rec["level"], rec.get("order_id"),
                                     rec["cid"])
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as exc:
            self.state["halted"] = True
            self.state["halt_reason"] = "order fill registration failed"
            self.log("CRITICAL khong dang ky duoc lot tu LIMIT %s qty=%s: %s "
                     "-> halt doi chieu" % (rec["cid"], qty, exc))
            return None
        pos["maker_entry"] = True
        if self.dry_run:
            # dry-run: phi maker thay vi taker uoc tinh trong _register_lot
            maker = qty * entry * float(self.cfg.get("fee_maker", 0.0002))
            delta = maker - _f(pos.get("fee_entry"))
            pos["fee_entry"] = maker
            self.state["equity"] = _f(self.state.get("equity")) - delta
            self.state["stats"]["fees"] = _f(
                self.state["stats"].get("fees")) + delta
        self.log("%s KHOP LIMIT #%d %s %s %s qty=%s/%s @ %s sl=%s tp=%s" % (
            "DRY_RUN" if self.dry_run else "LIVE", pos["id"], symbol, side,
            rec["level"], qty, rec["qty"], entry, pos["sl"], pos["tp"]))
        if minn and qty * entry < minn:
            self.log("WARNING lot #%d tu khop 1 phan %.4f USDT < minNotional "
                     "%.2f -> dong ngay" % (pos["id"], qty * entry, minn))
            rec2 = self.close(pos, entry, "ENTRY_DUST")
            if rec2:
                if getattr(self, "_pending_close_recs", None) is None:
                    self._pending_close_recs = []
                self._pending_close_recs.append(rec2)
        return pos

    # ------------------------------------------------------- startup
    def _startup_entry_orders(self):
        """Khoi dong live: bo lenh dry con sot, tra lai trang thai moi lenh
        tu san (REST) truoc khi doi chieu vi the."""
        orders = self.entry_orders()
        dry = [o for o in orders if o.get("dry")]
        for o in dry:
            orders.remove(o)
        if dry:
            self.log("WARNING bo %d lenh cho dry-run khi chay live" % len(dry))
        if orders:
            self.sync_entry_orders(force_poll=True)
