"""Paper trading engine: simulates futures fills with fees + slippage."""
import time


class PaperEngine:
    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self._pid = state.get("_pid", 0)

    def _next_id(self):
        self._pid += 1
        self.state["_pid"] = self._pid
        return self._pid

    def _fees(self, notional):
        return notional * self.cfg["fee_rate"]

    def used_margin(self):
        return sum(p["notional"] / self.cfg["leverage"]
                   for p in self.state["positions"])

    def open(self, inst, side, notional, price, sl_pct, tp_pct, tag,
             level=None):
        """side: 'long' or 'short'. Returns (position, reason)."""
        slip = self.cfg["slippage"]
        if len(self.state["positions"]) >= self.cfg.get("max_total_positions", 999):
            return None, "max_positions"
        if side == "long":
            entry = price * (1 + slip)
            sl = entry * (1 - sl_pct) if sl_pct else None
            tp = entry * (1 + tp_pct) if tp_pct else None
        else:
            entry = price * (1 - slip)
            sl = entry * (1 + sl_pct) if sl_pct else None
            tp = entry * (1 - tp_pct) if tp_pct else None
        qty = notional / entry
        fee = self._fees(notional)
        margin_need = notional / self.cfg["leverage"]
        if self.state["equity"] - self.used_margin() < margin_need:
            return None, "insufficient_margin"
        total_notional = sum(p["notional"] for p in self.state["positions"])
        if total_notional + notional > self.state["equity"] * self.cfg["risk"]["max_notional_mult"]:
            return None, "exposure_cap"
        pos = {
            "id": self._next_id(),
            "inst": inst,
            "side": side,
            "qty": qty,
            "entry": entry,
            "notional": notional,
            "sl": sl,
            "tp": tp,
            "tag": tag,
            "level": level,
            "opened_at": int(time.time()),
            "fee_entry": fee,
        }
        self.state["equity"] -= fee
        self.state["stats"]["fees"] += fee
        self.state["positions"].append(pos)
        return pos, "ok"

    def close(self, pos, price, reason):
        slip = self.cfg["slippage"]
        if pos["side"] == "long":
            ex = price * (1 - slip)
            pnl = (ex - pos["entry"]) * pos["qty"]
        else:
            ex = price * (1 + slip)
            pnl = (pos["entry"] - ex) * pos["qty"]
        fee = self._fees(pos["notional"])
        net = pnl - fee  # entry fee already deducted at open
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
        }
        self.state["positions"] = [p for p in self.state["positions"]
                                   if p["id"] != pos["id"]]
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
