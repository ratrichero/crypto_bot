#!/usr/bin/env python3
"""Live trading engine for OKX USDT-SWAP perpetual futures.

Same interface as PaperEngine (open/close/unrealized) so bot.py swaps engines
via config "mode": "paper" | "dry_run" | "live" (default "paper").

DESIGN CHOICE -- SL/TP handling:
    We do NOT place exchange-side conditional (algo) stop orders. Instead the
    bot's fast loop (~0.5s) watches live websocket prices and closes positions
    with a market order, exactly like the paper engine does. Rationale:
      1. One code path for SL/TP across paper/dry_run/live -> fewer bugs.
      2. No algo-order lifecycle to manage (place/amend/cancel across
         restarts, partial-fill edge cases, orphaned stops).
      3. Reaction time is bounded by the WS tick loop (~0.5s), comparable to
         exchange conditional orders for this strategy's horizons.
    TRADE-OFF (read this): if this process dies, NO protective stop rests on
    the exchange. Mitigations in place: watchdog restarts the bot within
    minutes; daily-stop halts trading; position sizes are small. Before
    scaling up, add exchange-side conditional stops as hardening.

SAFETY:
    - Credentials live ONLY in .okx_key (chmod 600), never in code/config/logs.
    - dry_run needs NO key and makes ZERO authenticated calls (not even reads).
    - live refuses to start without the key file (fail closed, clear message).
    - Secrets are never printed or logged; only masked key prefixes appear.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(BASE, ".okx_key")
API_BASE = "https://www.okx.com"
TIMEOUT = 15
MGN_MODE = "cross"  # cross-margin for every bot position


# ---------------------------------------------------------------- credentials
def load_credentials(path=KEY_PATH):
    """Read {api_key, api_secret, passphrase} from a chmod-600 JSON file."""
    if not os.path.exists(path):
        raise RuntimeError(
            "Thieu file credentials OKX: %s\n"
            "Tao file JSON: {\"api_key\": \"...\", \"api_secret\": \"...\", "
            "\"passphrase\": \"...\"} roi chay: chmod 600 %s\n"
            "Key phai co quyen Read+Trade, TAT quyen Withdraw. "
            "Khong bao gio dan key vao chat." % (path, path))
    try:
        with open(path) as f:
            creds = json.load(f)
    except Exception as e:
        raise RuntimeError("Khong doc duoc %s: %s" % (path, e))
    for k in ("api_key", "api_secret", "passphrase"):
        if not creds.get(k):
            raise RuntimeError("%s thieu truong %r" % (path, k))
    try:
        if os.stat(path).st_mode & 0o077:
            print("WARNING: %s dang doc duoc boi user/group khac; "
                  "nen chmod 600" % path, flush=True)
    except Exception:
        pass
    return creds


def _mask(s):
    s = str(s or "")
    return (s[:3] + "..." + s[-2:]) if len(s) > 6 else "***"


# ------------------------------------------------------------------ signing
def iso_timestamp():
    """OKX timestamp: ISO8601 UTC with milliseconds, e.g. 2020-12-08T09:08:57.715Z"""
    ms = int(time.time() * 1000)
    return (datetime.fromtimestamp(ms / 1000, timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000))


def sign(timestamp, method, request_path, body, secret):
    """OKX v5 signature: base64(HMAC_SHA256(secret, ts + METHOD + path + body))."""
    msg = "%s%s%s%s" % (timestamp, method.upper(), request_path, body or "")
    mac = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


class OKXAuthClient:
    """Minimal signed REST client. Raises RuntimeError on API/HTTP errors."""

    def __init__(self, creds):
        self.creds = creds
        self.session = requests.Session()

    def _headers(self, method, path, body):
        ts = iso_timestamp()
        return {
            "OK-ACCESS-KEY": self.creds["api_key"],
            "OK-ACCESS-SIGN": sign(ts, method, path, body,
                                   self.creds["api_secret"]),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.creds["passphrase"],
            "Content-Type": "application/json",
        }

    def request(self, method, path, body_obj=None):
        # NOTE: for GET, query params must already be inside `path` (OKX signs
        # the full path, not a separate params dict).
        body = (json.dumps(body_obj, separators=(",", ":"))
                if body_obj is not None else "")
        headers = self._headers(method, path, body)
        try:
            r = self.session.request(method, API_BASE + path,
                                     data=body or None,
                                     headers=headers, timeout=TIMEOUT)
        except Exception as e:
            raise RuntimeError("OKX %s %s: loi mang: %s" % (method, path, e))
        try:
            j = r.json()
        except Exception:
            raise RuntimeError("OKX %s %s: HTTP %s (khong phai JSON)"
                               % (method, path, r.status_code))
        if j.get("code") != "0":
            raise RuntimeError("OKX %s %s: code=%s msg=%s"
                               % (method, path, j.get("code"), j.get("msg")))
        return j.get("data", [])


# ------------------------------------------------------------ contract sizing
def contracts_for(notional, price, ct_val, lot_sz, min_sz):
    """Convert USD notional -> OKX contract size string (round DOWN to lotSz).

    Returns None when the result is below minSz (order would be rejected).
    Pure function: no network, easy to unit-test.
    """
    raw = Decimal(str(notional)) / (Decimal(str(price)) * Decimal(str(ct_val)))
    lot = Decimal(str(lot_sz))
    sz = (raw / lot).to_integral_value(rounding=ROUND_DOWN) * lot
    if sz <= 0 or sz < Decimal(str(min_sz)):
        return None
    return format(sz.normalize(), "f")


# ------------------------------------------------------------------- engine
class LiveEngine:
    """Drop-in replacement for PaperEngine backed by real OKX orders."""

    def __init__(self, cfg, state, dry_run=False, log=None):
        self.cfg = cfg
        self.state = state
        self.dry_run = dry_run
        self.log = log or (lambda m: print(m, flush=True))
        self._pid = state.get("_pid", 0)
        self._inst_cache = {}
        self._dry_n = 0
        if dry_run:
            self.client = None
            self.pos_mode = "net_mode"  # unused in dry_run
            self.log("LiveEngine DRY_RUN: khong can key, KHONG goi bat ky "
                     "API nao (ke ca read-only). Chi log lenh SE dat.")
        else:
            creds = load_credentials()
            self.client = OKXAuthClient(creds)
            self.log("LiveEngine LIVE: da nap key %s (da che). "
                     "MOI LENH DAT LA TIEN THAT." % _mask(creds["api_key"]))
            self.pos_mode = self._detect_pos_mode()
            self.log("OKX posMode=%s" % self.pos_mode)
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

    def _detect_pos_mode(self):
        data = self.client.request("GET", "/api/v5/account/config")
        return (data[0].get("posMode", "net_mode") if data else "net_mode")

    def get_positions(self):
        """Query open positions on the exchange (read-only)."""
        if self.dry_run:
            return []
        return self.client.request(
            "GET", "/api/v5/account/positions?instType=SWAP")

    def get_balance_usdt(self):
        """Query USDT balance (read-only). Returns dict with totalEq/availBal."""
        if self.dry_run:
            return {"totalEq": self.state.get("equity", 0.0), "availBal": 0.0}
        data = self.client.request("GET", "/api/v5/account/balance?ccy=USDT")
        d = data[0] if data else {}
        det = (d.get("details") or [{}])[0]
        return {"totalEq": float(d.get("totalEq", 0) or 0),
                "availBal": float(det.get("availBal", 0) or 0)}

    def refresh_equity(self):
        """Sync state equity from the real account (best-effort)."""
        if self.dry_run:
            return
        try:
            self.state["equity"] = self.get_balance_usdt()["totalEq"]
        except Exception as e:
            self.log("WARNING refresh_equity that bai: %s (giu equity cu)" % e)

    def _reconcile_startup(self):
        """Drop paper-ghost positions; warn about unmanaged exchange ones."""
        try:
            ex = set()
            for p in self.get_positions():
                if float(p.get("pos", 0) or 0) == 0:
                    continue
                if self.pos_mode == "long_short_mode":
                    side = p.get("posSide")
                else:  # net_mode: posSide="net" -> suy side tu dau cua pos
                    side = "long" if float(p["pos"]) > 0 else "short"
                ex.add((p.get("instId"), side))
        except Exception as e:
            self.log("WARNING khong doc duoc vi the san de doi chieu: %s" % e)
            return
        kept, pruned = [], []
        for p in self.state["positions"]:
            if not p.get("live"):
                pruned.append(p)  # ghost tu paper mode
                continue
            key = (p["inst"], p["side"])
            (kept if key in ex else pruned).append(p)
        if pruned:
            ids = [p["id"] for p in pruned]
            self.log("WARNING loai bo %d vi the khong ton tai tren san "
                     "(paper ghost / da dong tay): ids=%s" % (len(pruned), ids))
            self.state["positions"] = kept
        state_keys = {(p["inst"], p["side"]) for p in kept}
        for k in sorted(ex - state_keys):
            self.log("WARNING san co vi the %s ma state khong quan ly "
                     "-> tu dong tay hoac xoa state.json" % (k,))

    def get_instrument(self, inst):
        if inst not in self._inst_cache:
            data = self.client.request(
                "GET", "/api/v5/public/instruments?instType=SWAP&instId=" + inst)
            if not data:
                raise RuntimeError("khong lay duoc instrument %s" % inst)
            d = data[0]
            self._inst_cache[inst] = {
                "ctVal": d["ctVal"], "lotSz": d["lotSz"], "minSz": d["minSz"]}
        return self._inst_cache[inst]

    def _set_leverage(self, inst):
        if self.dry_run:
            self.log("DRY_RUN set-leverage %s lev=%s mgnMode=%s"
                     % (inst, self.cfg["leverage"], MGN_MODE))
            return
        self.client.request("POST", "/api/v5/account/set-leverage", {
            "instId": inst, "lever": str(self.cfg["leverage"]),
            "mgnMode": MGN_MODE})

    def _place_market(self, inst, side, sz, pos_side=None,
                      reduce_only=False, ref_price=None):
        """side: 'buy'/'sell'. Returns (ord_id, fill_price_or_None)."""
        body = {"instId": inst, "tdMode": MGN_MODE, "side": side,
                "ordType": "market", "sz": sz}
        if reduce_only:
            body["reduceOnly"] = "true"
        if self.pos_mode == "long_short_mode" and pos_side:
            body["posSide"] = pos_side
        if self.dry_run:
            self._dry_n += 1
            oid = "dryrun-%d" % self._dry_n
            self.log("DRY_RUN dat lenh: %s" % json.dumps(body))
            return oid, ref_price
        data = self.client.request("POST", "/api/v5/trade/order", body)
        if not data:
            raise RuntimeError("dat lenh khong tra ve ordId: %s" % inst)
        return data[0].get("ordId"), None

    def _fill_price(self, inst, ord_id, ref_price):
        """Poll order status for avgPx; fall back to ref_price on failure."""
        if self.dry_run:
            return ref_price
        px = None
        for _ in range(6):
            try:
                data = self.client.request(
                    "GET", "/api/v5/trade/order?instId=%s&ordId=%s"
                    % (inst, ord_id))
                if data:
                    d = data[0]
                    if d.get("avgPx"):
                        px = float(d["avgPx"])
                    if d.get("state") == "filled":
                        break
            except Exception as e:
                self.log("WARNING poll order %s: %s" % (ord_id, e))
            time.sleep(1)
        if px is None:
            self.log("WARNING khong lay duoc avgPx cho %s, dung gia ref %s"
                     % (ord_id, ref_price))
            return ref_price
        return px

    # ----------------------------------------------------------------- open
    def open(self, inst, side, notional, price, sl_pct, tp_pct, tag,
             level=None):
        """side: 'long' or 'short'. Returns (position, reason)."""
        if len(self.state["positions"]) >= self.cfg.get("max_total_positions", 999):
            return None, "max_positions"
        margin_need = notional / self.cfg["leverage"]
        if self.dry_run:
            free = self.state["equity"] - self.used_margin()
        else:
            try:
                free = self.get_balance_usdt()["availBal"]
            except Exception as e:
                return None, "balance_query_failed: %s" % e
        if free < margin_need:
            return None, "insufficient_margin"
        total_notional = sum(p["notional"] for p in self.state["positions"])
        if total_notional + notional > self.state["equity"] * self.cfg["risk"]["max_notional_mult"]:
            return None, "exposure_cap"

        self._set_leverage(inst)
        if self.dry_run:
            sz = contracts_for(notional, price, 1, 1, 0.0001)
            qty = notional / price
        else:
            info = self.get_instrument(inst)
            sz = contracts_for(notional, price, info["ctVal"],
                               info["lotSz"], info["minSz"])
            if sz is None:
                return None, "size_too_small"
            qty = float(sz) * float(info["ctVal"])
        okx_side = "buy" if side == "long" else "sell"
        pos_side = side if self.pos_mode == "long_short_mode" else None
        ord_id, _ = self._place_market(inst, okx_side, sz,
                                       pos_side=pos_side, ref_price=price)
        entry = self._fill_price(inst, ord_id, price)
        if side == "long":
            sl = entry * (1 - sl_pct) if sl_pct else None
            tp = entry * (1 + tp_pct) if tp_pct else None
        else:
            sl = entry * (1 + sl_pct) if sl_pct else None
            tp = entry * (1 - tp_pct) if tp_pct else None
        fee = self._fees(notional)
        pos = {
            "id": self._next_id(),
            "inst": inst, "side": side, "qty": qty, "entry": entry,
            "notional": notional, "sl": sl, "tp": tp, "tag": tag,
            "level": level, "opened_at": int(time.time()), "fee_entry": fee,
            "live": True, "dry": self.dry_run, "ord_id": ord_id,
        }
        self.state["equity"] -= fee  # uoc tinh; live se sync lai tu san
        self.state["stats"]["fees"] += fee
        self.state["positions"].append(pos)
        self.log("%s OPEN #%d %s %s entry=%s sl=%s tp=%s ord=%s" %
                 ("DRY_RUN" if self.dry_run else "LIVE",
                  pos["id"], inst, side, entry, sl, tp, ord_id))
        return pos, "ok"

    # ---------------------------------------------------------------- close
    def close(self, pos, price, reason):
        """Close via reduce-only market order. price = trigger price."""
        okx_side = "sell" if pos["side"] == "long" else "buy"
        pos_side = pos["side"] if self.pos_mode == "long_short_mode" else None
        # tinh sz tu qty de dong het vi the
        if self.dry_run:
            sz = "1"  # placeholder, khong goi API
        else:
            info = self.get_instrument(pos["inst"])
            sz = contracts_for(pos["qty"] * price, price,
                               info["ctVal"], info["lotSz"], info["minSz"])
            if sz is None:  # vi the qua nho -> thu dong voi minSz
                sz = str(info["minSz"])
        ord_id, _ = self._place_market(pos["inst"], okx_side, sz,
                                       pos_side=pos_side,
                                       reduce_only=True, ref_price=price)
        ex = self._fill_price(pos["inst"], ord_id, price)
        if pos["side"] == "long":
            pnl = (ex - pos["entry"]) * pos["qty"]
        else:
            pnl = (pos["entry"] - ex) * pos["qty"]
        fee = self._fees(pos["notional"])
        net = pnl - fee  # fee_entry da tru luc open
        self.state["equity"] += net
        self.state["stats"]["fees"] += fee
        self.state["stats"]["trades"] += 1
        if net > 0:
            self.state["stats"]["wins"] += 1
        else:
            self.state["stats"]["losses"] += 1
        rec = {
            "id": pos["id"], "inst": pos["inst"], "side": pos["side"],
            "tag": pos["tag"], "entry": round(pos["entry"], 4),
            "exit": round(ex, 4), "notional": round(pos["notional"], 2),
            "pnl": round(net, 2), "reason": reason,
            "closed_at": int(time.time()),
            "live": True, "dry": self.dry_run, "close_ord": ord_id,
        }
        self.state["positions"] = [p for p in self.state["positions"]
                                   if p["id"] != pos["id"]]
        self.log("%s CLOSE #%d %s %s %s pnl=%+.2f ord=%s" %
                 ("DRY_RUN" if self.dry_run else "LIVE",
                  rec["id"], rec["inst"], rec["side"], reason, net, ord_id))
        if not self.dry_run:
            self.refresh_equity()  # equity that = so du san
        return rec

    def unrealized(self, prices):
        u = 0.0
        for p in self.state["positions"]:
            px = prices.get(p["inst"])
            if px is None:
                continue
            if p["side"] == "long":
                u += (px - p["entry"]) * p["qty"]
            else:
                u += (p["entry"] - px) * p["qty"]
        return u
