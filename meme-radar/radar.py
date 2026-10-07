#!/usr/bin/env python3
"""Meme radar: track smart money Solana qua GMGN + paper-track copy trade.

- Moi 60s: lay smartmoney buys moi -> signals.jsonl
- Cluster (>=2 vi khac nhau mua 1 token trong 30p) va whale (>=$2000)
  -> alerts.jsonl
- Paper: mo vi the copy 100U tai gia hien tai, chup P&L o +5/+15/+60/+240p,
  dong o 240p -> paper_trades.jsonl
- Kill switch: file STOP trong thu muc nay.
"""
import copy
import json
import os
import queue as queue_mod
import signal
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone

import requests

import feasibility as feas
from dexscreener import pick_pair

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json")))
# Helius key: uu tien env HELIUS_API_KEY, roi file .helius_key (chmod 600), roi config
if not CFG.get("helius_api_key"):
    CFG["helius_api_key"] = os.environ.get("HELIUS_API_KEY", "")
if not CFG.get("helius_api_key"):
    try:
        CFG["helius_api_key"] = open(os.path.join(BASE, ".helius_key")).read().strip()
    except FileNotFoundError:
        pass
STATE_P = os.path.join(BASE, "radar_state.json")
SIG_P = os.path.join(BASE, "signals.jsonl")
ALERT_P = os.path.join(BASE, "alerts.jsonl")
TRADES_P = os.path.join(BASE, "paper_trades.jsonl")
# Moi quyet dinh mo paper (opened / skipped_low_liquidity ...) -> report tinh
# % lenh bi loc thanh khoan.
ENTRIES_P = os.path.join(BASE, "paper_entries.jsonl")
LOG_P = os.path.join(BASE, "radar.log")
STOP_P = os.path.join(BASE, "STOP")
HCFG = CFG.get("holder", {})
HOLDER_TRADES_P = os.path.join(BASE, HCFG.get("log_file", "paper_trades_holder.jsonl"))
HOLDER_WALLETS = set(HCFG.get("wallets", [])) if HCFG.get("enabled") else set()


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}Z] {msg}"
    try:
        if os.path.exists(LOG_P) and os.path.getsize(LOG_P) > 5 * 1024 * 1024:
            with open(LOG_P, "rb") as f:
                f.seek(-1024 * 1024, os.SEEK_END)
                tail = f.read()
            with open(LOG_P, "wb") as f:
                f.write(tail)
    except Exception:
        pass
    with open(LOG_P, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_state():
    d = {"seen": [], "paper": [], "paper_holder": [], "alerted_clusters": []}
    if os.path.exists(STATE_P):
        try:
            d.update(json.load(open(STATE_P)))
        except Exception:
            pass
    return d


def save_state(st):
    st["seen"] = st["seen"][-5000:]
    tmp = STATE_P + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_P)


def append(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def run_cli(args):
    p = subprocess.run(["gmgn-cli"] + args, capture_output=True,
                       text=True, timeout=90)
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "")[:300]
        raise RuntimeError(f"gmgn-cli failed: {err}")
    return p.stdout


def fetch_smartmoney():
    out = run_cli(["track", "smartmoney", "--chain", CFG["chain"],
                   "--side", "buy", "--limit", str(CFG["smartmoney_limit"]),
                   "--raw"])
    data = json.loads(out)
    if isinstance(data, dict):
        for k in ("list", "data", "trades", "result"):
            if isinstance(data.get(k), list):
                return data[k]
        return []
    return data if isinstance(data, list) else []


def norm(t):
    def f(k, d=0.0):
        try:
            return float(t.get(k) or d)
        except (TypeError, ValueError):
            return d
    price_usd = f("price_usd")
    return {
        "tid": str(t.get("id") or t.get("transaction_hash") or ""),
        "wallet": t.get("maker") or "",
        "token": t.get("base_address") or "",
        "symbol": (t.get("base_symbol") or t.get("symbol") or "?").upper(),
        "amount_usd": f("amount_usd"),
        "price_usd": price_usd,
        "price_now": f("price_now", price_usd),
        "ts": int(t.get("timestamp") or time.time()),
        "tx": t.get("transaction_hash") or "",
    }


_price_cache = {}


def ds_info(addr):
    """Pair DexScreener tot nhat cua addr (dict price/symbol/mcap/
    liquidity_usd/volume_m5/volume_h24) hoac None, cache 60s."""
    now = time.time()
    ck = "info:" + addr
    c = _price_cache.get(ck)
    if c and now - c[0] < 60:
        return c[1]
    try:
        r = requests.get(f"https://api.dexscreener.com/tokens/v1/solana/{addr}",
                         timeout=12)
        r.raise_for_status()
        d = r.json()
        # Khong lay pair dau tien: co the la pair ma addr la QUOTE (vd XYZ/SOL khi
        # hoi gia SOL -> gia SOL sai -> amount_usd moi lenh WS/Helius sai)
        # hoac pool rac. pick_pair chon pair dung (xem dexscreener.py).
        res = pick_pair(d, addr)
    except Exception:
        res = None
    _price_cache[ck] = (now, res)
    return res


def ds_token(addr):
    """Tra ve (price_usd, symbol, mcap_usd) tu DexScreener, cache 60s."""
    p = ds_info(addr)
    if not p:
        return (None, "?", 0)
    return (p["price"], (p["symbol"] or "?").upper(), float(p["mcap"] or 0))


def ds_price(addr):
    px, _, _ = ds_token(addr)
    return px


SOL_MINT = "So11111111111111111111111111111111111111112"


def ingest(raw_list, seen, now):
    """Loc + dedupe + ghi signals. Dung chung cho poll va ws."""
    out = []
    for s in raw_list:
        if not s.get("tid") or s["tid"] in seen:
            continue
        seen.add(s["tid"])
        if s["amount_usd"] < CFG["min_amount_usd"]:
            continue
        if s["symbol"] in CFG["exclude_symbols"]:
            continue
        if s.get("liquidity_usd") is None:
            # thanh khoan luc phat hien (DexScreener, cache) -> paper loc kha
            # thi + live ghi lai de doi chieu
            info = ds_info(s["token"]) if s.get("token") else None
            s["liquidity_usd"] = (info or {}).get("liquidity_usd")
        sig = {"detected_at": int(now), **s}
        append(SIG_P, sig)
        out.append(sig)
    return out


def wallet_price(sol_spent, tokens, sol_usd):
    """Gia vi nguon khop (USD/token) tu tx; None neu khong tinh duoc."""
    try:
        t = float(tokens or 0)
        px = float(sol_spent) * float(sol_usd) / t if t > 0 else 0
    except (TypeError, ValueError):
        return None
    return px if px > 0 and px == px and px != float("inf") else None


def enrich_ws(raw, sol_usd):
    """Bo sung gia/symbol/mcap/thanh khoan + gia vi nguon khop cho tin ws."""
    info = ds_info(raw["token"]) or {}
    price_usd = info.get("price")
    return {
        "tid": raw["tid"], "wallet": raw["wallet"], "token": raw["token"],
        "symbol": (info.get("symbol") or "?").upper(),
        "amount_usd": round(raw["sol_spent"] * sol_usd, 1),
        "price_usd": price_usd or 0, "price_now": price_usd or 0,
        "mcap_usd": float(info.get("mcap") or 0),
        "liquidity_usd": info.get("liquidity_usd"),
        "wallet_price_usd": wallet_price(raw["sol_spent"], raw.get("tokens"),
                                         sol_usd),
        "wallet_tokens": raw.get("tokens"), "sol_spent": raw["sol_spent"],
        "ts": raw["ts"], "tx": raw["tid"], "src": "ws",
    }


def size_for_mcap(mcap_usd):
    """Coin mong -> vao it; coin day -> vao du. Tiers giam dan theo nguong."""
    for threshold, sz in CFG.get("size_by_mcap", [[0, 100]]):
        if (mcap_usd or 0) >= threshold:
            return sz
    return CFG.get("size_by_mcap", [[0, 100]])[-1][1]



def build_params(main=True):
    if main:
        return {"key": "paper", "use_tp_ladder": True,
                "tp1_pct": CFG.get("tp1_pct", 0.50), "tp1_frac": CFG.get("tp1_frac", 0.34),
                "tp2_pct": CFG.get("tp2_pct", 1.0), "tp2_frac": CFG.get("tp2_frac", 0.33),
                "trailing_pct": CFG.get("trailing_pct", 0.30),
                "sl_pct": CFG.get("sl_pct", 0.25),
                "time_stop_min": CFG.get("time_stop_min", 480),
                "ts_keep_pct": CFG.get("time_stop_keep_pct", 0.20),
                "ts_keep_frac": CFG.get("time_stop_keep_frac", 0.50),
                "trades_path": TRADES_P, "closed_key": "closed_keys"}
    return {"key": "paper_holder", "use_tp_ladder": False,
            "trailing_pct": HCFG.get("trailing_pct", 0.40),
            "trail_arm_pct": HCFG.get("trail_arm_pct", 0.20),
            "sl_pct": HCFG.get("sl_pct", 0.50),
            "time_stop_min": HCFG.get("time_stop_min", 1440),
            "ts_keep_pct": HCFG.get("time_stop_keep_pct", 0.20),
            "ts_keep_frac": HCFG.get("time_stop_keep_frac", 0.50),
            "trades_path": HOLDER_TRADES_P, "closed_key": "closed_keys_holder"}


def paper_decide(p, P, px, now):
    """Quyet dinh thoat cho 1 vi the paper o gia px (THUAN: chi mutate p --
    co tp1/tp2/ts_done/..., peak, remaining). Tra ve (actions, reason) voi
    actions = [(frac, why)]. manage_positions chay thu tren ban sao de kiem
    tra kha nang thoat truoc khi ap dung that.
    LUU Y: logic exit cho plan scalp PHAI DONG BO voi strategy.py::decide_exits
    (live_trader dung strategy.py). Moi thay doi exit rules cho scalp thi sua
    strategy.py truoc, roi dong bo vao day."""
    actions = []
    entry = p["entry"]
    ret = px / entry - 1
    p["peak"] = max(p.get("peak", entry), px)
    el_min = (now - p["opened_at"]) / 60
    reason = None

    def _sell_frac(frac, why):
        actions.append((frac, why))
        p["remaining"] = p.get("remaining", 1.0) - frac

    # TP ladder (chi plan scalp)
    if P["use_tp_ladder"]:
        if not p.get("tp1") and ret >= P["tp1_pct"]:
            p["tp1"] = True
            _sell_frac(P["tp1_frac"], "TP1")
        if p.get("tp1") and not p.get("tp2") and ret >= P["tp2_pct"]:
            p["tp2"] = True
            _sell_frac(P["tp2_frac"], "TP2")
    # trailing: scalp -> sau TP2/ts_keep; holder -> khi dinh lai >= trail_arm_pct
    rem = p.get("remaining", 1.0)
    if P["use_tp_ladder"]:
        trail_arm = p.get("tp2") or p.get("ts_keep")
    else:
        trail_arm = p["peak"] >= entry * (1 + P["trail_arm_pct"])
    if trail_arm and rem > 0 and px <= p["peak"] * (1 - P["trailing_pct"]):
        _sell_frac(rem, "TRAIL")
        reason = "trailing"
    # SL
    rem = p.get("remaining", 1.0)
    if not reason and rem > 0 and ret <= -P["sl_pct"]:
        _sell_frac(rem, "SL")
        reason = "stop_loss"
    # smart exit: dan qua bay
    rem = p.get("remaining", 1.0)
    if not reason and rem > 0 and p.get("smart_exit"):
        _sell_frac(rem, "SMART_EXIT")
        reason = "smart_exit"
    # copy exit: chinh vi nguon da xa phan lon (dong bo strategy.COPY_EXIT)
    rem = p.get("remaining", 1.0)
    if not reason and rem > 0 and p.get("copy_exit"):
        _sell_frac(rem, "COPY_EXIT")
        reason = "copy_exit"
    # holder: vi goc xa -> chot 1/2 theo
    rem = p.get("remaining", 1.0)
    if not reason and rem > 0 and p.get("holder_sell"):
        p["holder_sell"] = False
        _sell_frac(rem * 0.5, "HOLDER_SELL")
    # time stop: het gio -> neu lai tot giu 1 phan du song, khong thi cat het
    rem = p.get("remaining", 1.0)
    if (not reason and rem > 0 and not p.get("ts_done")
            and el_min >= P["time_stop_min"]):
        p["ts_done"] = True
        if ret >= P["ts_keep_pct"]:
            _sell_frac(rem * P["ts_keep_frac"], "TIME_KEEP")
            p["ts_keep"] = True
        else:
            _sell_frac(rem, "TIME")
            reason = "time_stop"
    return actions, reason


def manage_positions(st, P, now, price_fn=None):
    """Exit engine tham so hoa cho ca plan scalp va holder + kiem tra kha thi
    (feasibility.py): leg ban lon so voi volume 5 phut -> exit_constrained,
    hoan toi da max_exit_waits vong roi thoat voi slippage toi da
    (exit_failed_liquidity). Moi leg ghi ca ret goc (khop tai gia DexScreener)
    va ret_adj (sau slippage vao + ra)."""
    F = feas.params(CFG)
    enabled = bool(F["feasibility_enabled"])
    price_fn = price_fn or ds_info
    positions = st.setdefault(P["key"], [])
    for p in list(positions):
        info = price_fn(p["token"]) or {}
        px = info.get("price")
        el_min = (now - p["opened_at"]) / 60
        for h in CFG["horizons_min"]:
            if el_min >= h and str(h) not in p["snaps"] and px:
                p["snaps"][str(h)] = round(px / p["entry"] - 1, 4)
        if not px:
            continue
        entry = p["entry"]
        ret = px / entry - 1
        forced = False
        if enabled:
            probe = copy.deepcopy(p)
            planned, _ = paper_decide(probe, P, px, now)
            if planned:
                leg_usd = (sum(f for f, _ in planned) * p["size_usd"]
                           * (1 + ret))
                if feas.exit_constrained(leg_usd, info.get("volume_m5"), F):
                    p["exit_constrained"] = True
                    p["exit_waits_total"] = p.get("exit_waits_total", 0) + 1
                    waits = p.get("exit_waits", 0) + 1
                    if waits <= int(F["max_exit_waits"]):
                        p["exit_waits"] = waits
                        p["peak"] = probe["peak"]
                        log(f"PAPER[{P['key']}] exit_constrained "
                            f"{p['symbol']} {'/'.join(w for _, w in planned)}"
                            f" leg ${leg_usd:.0f} vs vol5m "
                            f"${float(info.get('volume_m5') or 0):.0f} -> "
                            f"cho ({waits}/{int(F['max_exit_waits'])})")
                        continue
                    forced = True
        actions, reason = paper_decide(p, P, px, now)
        if not actions:
            continue
        liq = info.get("liquidity_usd") or p.get("liquidity_usd")
        entry_eff = p.get("entry_eff") or entry
        for frac, why in actions:
            leg_usd = frac * p["size_usd"] * (1 + ret)
            if not enabled:
                slip = 0.0
            elif forced:
                slip = float(F["max_slippage_pct"])
            else:
                slip = feas.slippage_pct(leg_usd, liq, F)
            ret_adj = feas.apply_sell(px, slip) / entry_eff - 1
            leg = {"frac": round(frac, 4), "ret": round(ret, 4),
                   "at": int(now), "why": why,
                   "slip_pct": round(slip, 3), "ret_adj": round(ret_adj, 4)}
            if forced:
                leg["exit_failed_liquidity"] = True
            p["legs"].append(leg)
            p["realized"] = p.get("realized", 0.0) + frac * p["size_usd"] * ret
            p["realized_adj"] = (p.get("realized_adj", 0.0)
                                 + frac * p["size_usd"] * ret_adj)
            log(f"PAPER[{P['key']}] {why} {p['symbol']} {ret:+.1%} "
                f"(adj {ret_adj:+.1%}, slip {slip:.1f}%, ban {frac:.0%})"
                + (" exit_failed_liquidity" if forced else ""))
            if why == "TIME_KEEP":
                log(f"PAPER[{P['key']}] TIME_KEEP {p['symbol']} giu "
                    f"{1 - P['ts_keep_frac']:.0%} du song (ret {ret:+.1%})")
        p["exit_waits"] = 0
        if forced:
            p["exit_failed_liquidity"] = True
        if p.get("remaining", 1.0) <= 1e-9:
            total_ret = p["realized"] / p["size_usd"]
            total_adj = p.get("realized_adj", 0.0) / p["size_usd"]
            ckey = f"{p['token']}:{p['opened_at']}:{p['wallet']}"
            cks = st.setdefault(P["closed_key"], [])
            if ckey not in cks:
                append(P["trades_path"], {
                    "token": p["token"], "symbol": p["symbol"],
                    "wallet": p["wallet"], "opened_at": p["opened_at"],
                    "closed_at": int(now), "entry": p["entry"],
                    "exit": px, "size_usd": p["size_usd"],
                    "snaps": p["snaps"], "legs": p["legs"],
                    "final_ret": round(total_ret, 4),
                    "final_ret_adj": round(total_adj, 4),
                    "realized_adj_usd": round(p.get("realized_adj", 0.0), 4),
                    "entry_slip_pct": p.get("entry_slip_pct"),
                    "liquidity_usd": p.get("liquidity_usd"),
                    "exit_constrained": bool(p.get("exit_constrained")),
                    "exit_waits_total": p.get("exit_waits_total", 0),
                    "exit_failed_liquidity":
                        bool(p.get("exit_failed_liquidity")),
                    "feasibility": enabled,
                    "reason": reason or "ladder_done",
                })
                cks.append(ckey)
                if len(cks) > 2000:
                    del cks[:1000]
            positions.remove(p)
            log(f"PAPER[{P['key']}] close {p['symbol']} final={total_ret:+.1%}"
                f" adj={total_adj:+.1%} reason={reason or 'ladder_done'}")


def record_entry(s, plan, decision, now, **extra):
    """Ghi 1 quyet dinh mo paper vao paper_entries.jsonl (opened / skipped_*)
    -> report tinh ty le lenh bi loc kha thi."""
    append(ENTRIES_P, {"ts": int(now), "plan": plan, "decision": decision,
                       "token": s.get("token"), "symbol": s.get("symbol"),
                       "wallet": s.get("wallet"), "tid": s.get("tid"),
                       "mcap_usd": s.get("mcap_usd"), **extra})


def open_paper(st, s, now, info_fn=None):
    """Mo (hoac scale-in) vi the paper cho 1 signal. Tra ve "full" khi plan
    scalp het slot (caller dung vong lap nhu truoc), nguoc lai None.
    Kha thi: thanh khoan < size * min_liquidity_mult -> skipped_low_liquidity;
    gia khop = gia DexScreener * (1 + slippage) (entry_eff). Trigger TP/SL van
    tinh theo gia tho `entry` (dong bo live va du lieu cu)."""
    F = feas.params(CFG)
    enabled = bool(F["feasibility_enabled"])
    info_fn = info_fn or ds_info
    entry = s.get("price_now") or s.get("price_usd")
    is_holder = s["wallet"] in HOLDER_WALLETS
    plan = "paper_holder" if is_holder else "paper"
    if not is_holder:
        if len(st["paper"]) >= CFG["max_paper_positions"]:
            return "full"
    if not entry:
        return None
    liq = s.get("liquidity_usd")
    if liq is None and enabled:
        liq = (info_fn(s["token"]) or {}).get("liquidity_usd")

    if is_holder:
        # scale-in: vi mua them token dang om -> cong size
        for p in st.get("paper_holder", []):
            if (p["token"] == s["token"] and p["wallet"] == s["wallet"]
                    and p.get("remaining", 1.0) > 0
                    and p.get("scale_ins", 0) < 2):
                add = p["size_usd"] * HCFG.get("scale_in_frac", 0.5)
                slip = 0.0
                if enabled:
                    ok, why = feas.liquidity_ok(add, liq, F)
                    if not ok:
                        record_entry(s, plan, why + "_scale_in", now,
                                     size_usd=add, liquidity_usd=liq)
                        log(f"PAPER[paper_holder] SCALE_IN {s['symbol']} bo "
                            f"qua: {why} (liq ${float(liq or 0):,.0f})")
                        return None
                    slip = feas.slippage_pct(add, liq, F)
                old_eff = p.get("entry_eff") or p["entry"]
                p["entry_eff"] = ((old_eff * p["size_usd"]
                                   + feas.apply_buy(entry, slip) * add)
                                  / (p["size_usd"] + add))
                p["entry"] = (p["entry"] * p["size_usd"]
                              + entry * add) / (p["size_usd"] + add)
                p["size_usd"] = p["size_usd"] + add
                p["scale_ins"] = p.get("scale_ins", 0) + 1
                p["peak"] = max(p["peak"], entry)
                record_entry(s, plan, "scaled_in", now, size_usd=add,
                             liquidity_usd=liq, slip_pct=round(slip, 3))
                log(f"PAPER[paper_holder] SCALE_IN {s['symbol']} "
                    f"+${add:.0f} (lan {p['scale_ins']})")
                return None
        if len(st.get("paper_holder", [])) >= HCFG.get("max_positions", 10):
            return None
    else:
        # Dedup: khong mo vi the moi neu da co token nay dang mo
        # (khac holder co scale-in, scalp chi giu 1 vi the/token)
        if any(p.get("token") == s["token"] and p.get("remaining", 1.0) > 0
               for p in st["paper"]):
            return None

    size = size_for_mcap(s.get("mcap_usd", 0))
    slip = 0.0
    if enabled:
        ok, why = feas.liquidity_ok(size, liq, F)
        if not ok:
            record_entry(s, plan, why, now, size_usd=size, liquidity_usd=liq,
                         need_usd=size * float(F["min_liquidity_mult"]))
            log(f"PAPER[{plan}] {why} {s.get('symbol')} size=${size} "
                f"liq=${float(liq or 0):,.0f} (can >= "
                f"${size * float(F['min_liquidity_mult']):,.0f})")
            return None
        slip = feas.slippage_pct(size, liq, F)
    pos = {
        "token": s["token"], "symbol": s["symbol"],
        "wallet": s["wallet"], "opened_at": int(now),
        "entry": entry, "size_usd": size,
        "mcap_usd": s.get("mcap_usd", 0),
        "peak": entry, "remaining": 1.0, "realized": 0.0,
        "realized_adj": 0.0, "liquidity_usd": liq,
        "entry_slip_pct": round(slip, 3),
        "entry_eff": feas.apply_buy(entry, slip),
        "src_remaining": 1.0, "snaps": {}, "legs": [],
    }
    if is_holder:
        pos["scale_ins"] = 0
    else:
        pos.update(tp1=False, tp2=False)
    st.setdefault(plan, []).append(pos)
    record_entry(s, plan, "opened", now, size_usd=size, liquidity_usd=liq,
                 slip_pct=round(slip, 3))
    if is_holder:
        log(f"PAPER[paper_holder] open {s['symbol']} @{entry} "
            f"size=${size} slip {slip:.1f}% (holder {s['wallet'][:8]}...)")
    else:
        log(f"PAPER open {s['symbol']} @{entry} size=${size} slip {slip:.1f}%"
            f" (mcap~${s.get('mcap_usd', 0):,.0f}, liq~"
            f"${float(liq or 0):,.0f}, copy {s['wallet'][:8]}...)")
    return None


def handle_sells(st, new_sells, now):
    """Lenh ban cua vi theo doi:
      - dedup theo tid (ws + poll cung 1 tx)
      - moi lenh co sold_frac -> alert `wallet_sell` (live_trader copy exit)
        + paper copy exit (cong don % vi nguon da xa, >= copy_exit_min_sold_frac)
      - lenh >= min_sol_spent -> sell_events (sell_cluster) + holder nhu cu."""
    evs = st.setdefault("sell_events", [])
    seen = st.setdefault("seen_sells", [])
    seen_set = set(seen)
    min_sol = float(CFG.get("min_sol_spent", 0.3))
    need = float(CFG.get("copy_exit_min_sold_frac", 0.5))
    copy_on = CFG.get("copy_exit", True)
    for r in new_sells:
        tid = r.get("tid")
        if tid:
            if tid in seen_set:
                continue
            seen_set.add(tid)
            seen.append(tid)
        frac = r.get("sold_frac")
        if frac is not None:
            append(ALERT_P, {"ts": int(r.get("ts") or now),
                             "type": "wallet_sell", "token": r["token"],
                             "wallet": r["wallet"], "sold_frac": frac,
                             "tid": tid, "sol": r.get("sol_amount")})
            if copy_on:
                for p in st.get("paper", []):
                    if (p["token"] != r["token"]
                            or p["wallet"] != r["wallet"]
                            or p.get("remaining", 1.0) <= 0
                            or p.get("copy_exit")
                            # lenh ban cu hon vi the (ws/poll tre) -> bo
                            or (r.get("ts") or now) < p["opened_at"] - 120):
                        continue
                    src = p.get("src_remaining", 1.0) * (1 - float(frac))
                    p["src_remaining"] = max(0.0, src)
                    if 1 - p["src_remaining"] >= need - 1e-9:
                        p["copy_exit"] = True
                        log(f"COPY_EXIT vi {r['wallet'][:8]} da xa "
                            f"{1 - p['src_remaining']:.0%} {p['symbol']} "
                            f"-> paper thoat theo")
        if float(r.get("sol_amount") or 0) < min_sol - 1e-12:
            continue   # lenh nho: chi cho copy exit, khong tinh cluster
        evs.append({"token": r["token"], "wallet": r["wallet"],
                    "ts": r["ts"], "amount_usd": r.get("amount_usd", 0)})
        if r["wallet"] in HOLDER_WALLETS:
            for p in st.get("paper_holder", []):
                if (p["token"] == r["token"] and p["wallet"] == r["wallet"]
                        and p.get("remaining", 1.0) > 0):
                    p["holder_sell"] = True
                    log(f"HOLDER vi {r['wallet'][:8]} xa "
                        f"{p['symbol']} -> chot 1/2 theo")
    st["seen_sells"] = seen[-3000:]
    st["sell_events"] = evs[-2000:]
    win_s = CFG.get("sell_cluster_window_min", 30) * 60
    by_tok = {}
    for e in evs:
        if now - e["ts"] <= win_s:
            by_tok.setdefault(e["token"], []).append(e)
    for tok, ss in by_tok.items():
        wallets = {x["wallet"] for x in ss}
        key = "sell:" + tok + ":" + str(int(now // win_s))
        if (len(wallets) >= CFG.get("sell_cluster_min_wallets", 2)
                and key not in st["alerted_clusters"]):
            st["alerted_clusters"].append(key)
            st["alerted_clusters"] = st["alerted_clusters"][-500:]
            for p in st["paper"] + st.get("paper_holder", []):
                if p["token"] == tok and p.get("remaining", 1.0) > 0:
                    p["smart_exit"] = True
            append(ALERT_P, {
                "ts": int(now), "type": "sell_cluster",
                "token": tok, "wallets": sorted(wallets),
                "n_wallets": len(wallets),
            })
            log(f"ALERT sell_cluster {tok[:10]}.. {len(wallets)} vi "
                f"xa -> smart exit")


# Dung sach khi nhan SIGINT/SIGTERM (pm2 stop/restart, Ctrl+C): chi dat co,
# thoat GIUA 2 vong lap -> khong cat ngang swap/ghi state. Lan 2 -> dung ngay.
_SHUTDOWN = {"signal": None}


def _request_shutdown(signum, frame):
    if _SHUTDOWN["signal"] is not None:
        raise KeyboardInterrupt
    _SHUTDOWN["signal"] = signum
    try:
        name = signal.Signals(signum).name
    except Exception:
        name = str(signum)
    log("%s -> dung sau vong lap hien tai (gui lan nua de dung ngay)" % name)


def install_signal_handlers():
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_shutdown)
        except (ValueError, OSError):      # khong o main thread
            pass


def shutdown_requested():
    return _SHUTDOWN["signal"] is not None


def sleep_unless_shutdown(seconds):
    """Ngu tung nhip ngan de dung nhanh khi co tin hieu."""
    end = time.time() + max(0.0, float(seconds))
    while not shutdown_requested():
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(0.5, left))


def main():
    install_signal_handlers()
    st = load_state()
    seen = set(st["seen"])
    log(f"Radar start. chain={CFG['chain']} poll={CFG['poll_seconds']}s PAPER MODE")
    # kenh real-time websocket (poll RPC van chay lam fallback)
    ws_q = queue_mod.Queue()
    ws_stop = threading.Event()
    if CFG.get("ws_enabled", True) and CFG.get("helius_api_key"):
        try:
            from sources.ws_feed import WSFeed
            WSFeed(CFG, BASE, ws_q, log, ws_stop).start()
        except Exception as e:
            log(f"ws feed khong khoi dong duoc: {e}")
    while True:
        try:
            if os.path.exists(STOP_P) or shutdown_requested():
                log("STOP -> shutdown" if os.path.exists(STOP_P)
                    else "signal -> shutdown (da luu state)")
                ws_stop.set()
                save_state(st)
                return
            src = CFG.get("source", "helius")
            now = time.time()
            new_sigs = []
            new_sells = []
            # 1) tin real-time tu websocket (buy + sell)
            drained = []
            try:
                while True:
                    drained.append(ws_q.get_nowait())
            except queue_mod.Empty:
                pass
            if drained:
                sol_px, _, _ = ds_token(SOL_MINT)
                buys_ws = [r for r in drained if r.get("side", "buy") == "buy"]
                new_sells += [r for r in drained if r.get("side") == "sell"]
                new_sigs += ingest([enrich_ws(r, sol_px or 150.0)
                                    for r in buys_ws], seen, now)
            if src == "helius":
                # --- nguon mien phi: doc truc tiep tx cac vi qua Helius ---
                if not CFG.get("helius_api_key"):
                    log("FATAL: chua co HELIUS_API_KEY -> dung, doi key")
                    return
                try:
                    from sources.helius import poll_wallet_txs
                    sol_px, _, _ = ds_token(SOL_MINT)
                    buys, sells = poll_wallet_txs(CFG, st, log, ds_token,
                                                  sol_px or 150.0, BASE)
                    new_sigs += ingest(buys, seen, now)
                    new_sells += sells
                except Exception as e:
                    log(f"helius poll failed: {e}")
                    time.sleep(CFG["poll_seconds"])
                    continue
            else:
                # --- nguon GMGN (can API key tra phi) ---
                try:
                    raw = fetch_smartmoney()
                except RuntimeError as e:
                    log(str(e))
                    if "config" in str(e).lower() or "api key" in str(e).lower():
                        log("FATAL: chua co GMGN API key -> dung, doi key")
                        return
                    time.sleep(CFG["poll_seconds"])
                    continue
                gmgn_sigs = []
                for t in raw:
                    s = norm(t)
                    if not s["token"]:
                        continue
                    gmgn_sigs.append(s)
                new_sigs += ingest(gmgn_sigs, seen, now)

            # cluster: >=N vi khac nhau mua 1 token trong window
            if new_sigs:
                win = CFG["cluster_window_min"] * 60
                recent = [s for s in new_sigs]  # + doc file neu can
                by_tok = {}
                for s in recent:
                    by_tok.setdefault(s["token"], []).append(s)
                # nap them tu file de du window
                try:
                    with open(SIG_P) as f:
                        for line in f:
                            try:
                                o = json.loads(line)
                            except Exception:
                                continue
                            if now - o.get("detected_at", 0) <= win:
                                by_tok.setdefault(o["token"], []).append(o)
                except FileNotFoundError:
                    pass
                for tok, ss in by_tok.items():
                    wallets = {x["wallet"] for x in ss}
                    key = tok + ":" + str(int(now // (win)))
                    if (len(wallets) >= CFG["cluster_min_wallets"]
                            and key not in st["alerted_clusters"]):
                        st["alerted_clusters"].append(key)
                        st["alerted_clusters"] = st["alerted_clusters"][-500:]
                        rep = ss[0]
                        append(ALERT_P, {
                            "ts": int(now), "type": "cluster", "token": tok,
                            "symbol": rep["symbol"],
                            "wallets": sorted(wallets),
                            "n_wallets": len(wallets),
                            "total_usd": round(sum(x["amount_usd"] for x in ss), 1),
                            "price_usd": rep["price_now"] or rep["price_usd"],
                        })
                        log(f"ALERT cluster {rep['symbol']} {len(wallets)} vi "
                            f"${sum(x['amount_usd'] for x in ss):.0f}")
                # whale: 1 lenh lon
                for s in new_sigs:
                    if s["amount_usd"] >= CFG["alert_min_amount_usd"]:
                        append(ALERT_P, {
                            "ts": int(now), "type": "whale", "token": s["token"],
                            "symbol": s["symbol"], "wallet": s["wallet"],
                            "amount_usd": s["amount_usd"],
                            "price_usd": s["price_now"] or s["price_usd"],
                        })
                        log(f"ALERT whale {s['symbol']} ${s['amount_usd']:.0f} "
                            f"vi {s['wallet'][:8]}...")

            # sell events -> wallet_sell/copy exit + smart exit (sell_cluster)
            if new_sells:
                handle_sells(st, new_sells, now)

            # paper copy: mo tai gia hien tai (+slippage), size theo mcap,
            # loc thanh khoan; vi holder -> chi vao plan holder
            for s in new_sigs:
                if open_paper(st, s, now) == "full":
                    break

            # exit engine (tham so hoa): plan scalp + plan holder
            manage_positions(st, build_params(main=True), now)
            if HCFG.get("enabled"):
                manage_positions(st, build_params(main=False), now)
            st["seen"] = list(seen)
            save_state(st)
        except Exception:
            log("LOOP ERROR:\n" + traceback.format_exc())
        sleep_unless_shutdown(CFG["poll_seconds"])


if __name__ == "__main__":
    main()
