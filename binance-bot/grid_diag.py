"""Vi sao grid KHONG mo lot? (task 42) - CHI DOC, khong dat lenh.

Phan lon dieu kien chan mo lot grid classic la IM LANG (regime trending,
scanner filter, max_symbols, risk_halted, gia chua cham tang...). Bot chay
du lau ma khong co lot nao thi log khong noi duoc vi sao. Module nay dung
lai DUNG chuoi dieu kien cua binance_bot.manage_grid cho tung symbol va tra
ve ly do dang chan + khoang cach toi tang gan nhat.

- explain_classic(): ham thuan, bot goi moi GRID_DIAG_SECONDS -> log
  "GRID WAIT ..." + st["grid_diag"] (dashboard hien thi).
- CLI (VPS, khong can dung bot):
      .venv/bin/python binance-bot/grid_diag.py            # bang tung coin
      .venv/bin/python binance-bot/grid_diag.py --json
      .venv/bin/python binance-bot/grid_diag.py --no-fetch # khong goi mang
  Doc state.json + scanner_latest.json + config.json + cache config runtime.
  Gia: 1 request PUBLIC /fapi/v1/ticker/price (weight 2), khong dung key.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Callable, Dict, Iterable, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

# code -> nhan ngan (log/dashboard). Thu tu = thu tu kiem tra trong bot.
LABELS = {
    "no_data": "chưa có giá/nến",
    "disabled": "tạm tắt",
    "manage_only": "chỉ quản lý (ngoài universe)",
    "regime": "regime trending",
    "risk_halted": "khoá rủi ro tới ngày UTC mới",
    "range_lots": "còn lot range cũ",
    "frozen": "đóng băng chờ flat",
    "max_symbols": "đủ max_symbols",
    "scanner": "scanner chặn",
    "full": "đủ max_positions",
    "levels_full": "đã lấp hết tầng",
    "side_blocked": "chạm tầng nhưng bị chặn chiều",
    "wait_price": "chờ giá chạm tầng",
    "ready": "sẵn sàng mở",
}


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _grid_lots(st, symbol=None):
    return [p for p in st.get("positions", []) if p.get("tag") == "grid"
            and (symbol is None or p.get("symbol") == symbol)]


def _next_level(grid: dict, anchor: float, step: float, levels: int,
                side: str):
    """Tang chua lay gan nhat cua `side` -> (key, gia) hoac None."""
    taken = grid.get("taken") or {}
    for k in range(1, levels + 1):
        key = ("b%d" if side == "long" else "s%d") % k
        if key in taken:
            continue
        px = anchor * (1 - k * step) if side == "long" else \
            anchor * (1 + k * step)
        return key, px
    return None


def explain_classic(st: dict, cfg: dict, symbols: Iterable[str],
                    prices: Dict[str, float], *, paused: bool = False,
                    has_data: Optional[Callable[[str], bool]] = None,
                    scanner_block: Optional[Callable[[str], Optional[str]]]
                    = None,
                    trend_block: Optional[Callable[[str, str], Optional[str]]]
                    = None,
                    side_cap_block: Optional[Callable[[str], Optional[str]]]
                    = None,
                    manage_only: Iterable[str] = (),
                    now: Optional[float] = None) -> dict:
    """Mo phong 1 vong grid classic, KHONG sua st. Tra ve:
    {ts, engine, global: [ly do chan moi symbol], symbols: [{symbol, code,
    detail, long_gap_pct, short_gap_pct}], counts: {code: n}, nearest}.
    gap_pct > 0: gia con phai di them bao nhieu % moi cham tang."""
    now = time.time() if now is None else now
    g = cfg.get("grid") or {}
    out = {"ts": now, "engine": str(g.get("engine") or "classic"),
           "global": [], "symbols": [], "counts": {}, "nearest": None}
    if out["engine"] != "classic":
        out["global"].append("engine=%s: xem log RANGE / tab Scanner"
                             % out["engine"])
        return out
    if st.get("halted"):
        out["global"].append("bot HALT: %s" % (st.get("halt_reason") or "?"))
    if paused:
        out["global"].append("có file PAUSE")
    lots = _grid_lots(st)
    n_grid = len(lots)
    max_pos = int(g.get("max_positions") or 0)
    if n_grid >= max_pos:
        out["global"].append("đủ grid.max_positions (%d/%d)" % (n_grid,
                                                                 max_pos))
    max_total = int(cfg.get("max_total_positions") or 0)
    if len(st.get("positions", [])) >= max_total:
        out["global"].append("đủ max_total_positions (%d/%d)" % (
            len(st.get("positions", [])), max_total))
    busy = {p.get("symbol") for p in lots}
    max_syms = int(g.get("max_symbols") or 0)
    levels = int(g.get("levels_each_side") or 0)
    disabled = cfg.get("disabled_symbols") or {}
    manage_only = set(manage_only)
    regimes = st.get("regimes") or {}
    grids = st.get("grids") or {}
    side_cap = {s: (side_cap_block(s) if side_cap_block else None)
                for s in ("long", "short")}
    for sym in symbols:
        row = {"symbol": sym, "code": None, "detail": "",
               "long_gap_pct": None, "short_gap_pct": None}
        out["symbols"].append(row)
        px = prices.get(sym)
        active = [p for p in lots if p.get("symbol") == sym]
        grid = grids.get(sym) or {}
        if not px or (has_data is not None and not has_data(sym)):
            row["code"] = "no_data"
            continue
        if _f(disabled.get(sym)) > now:
            row["code"] = "disabled"
            continue
        if sym in manage_only:
            row["code"] = "manage_only"
            continue
        reg = regimes.get(sym) or {}
        if reg.get("regime", "ranging") != "ranging":
            row["code"] = "regime"
            row["detail"] = "ADX 15m=%s" % reg.get("adx")
            if reg.get("candidate") == "ranging":
                row["detail"] += " (đang xác nhận về ranging %s nến)" % (
                    reg.get("candidate_count"))
            continue
        if grid.get("risk_halted"):
            row["code"] = "risk_halted"
            continue
        if any(str(p.get("level") or "").startswith("r") for p in active):
            row["code"] = "range_lots"
            continue
        # Nhu bot: tang cua lot dang mo luon tinh la da lay.
        grid = dict(grid, taken=dict(grid.get("taken") or {}))
        for p in active:
            if p.get("level") is not None:
                grid["taken"].setdefault(p["level"], p.get("id"))
        step = _f(grid.get("step")) or _f(g.get("step_pct"), 0.01)
        rng = _f(g.get("range_steps"), 6) * step
        anchor = grid.get("anchor")
        if grid.get("rebuild_pending") and active:
            row["code"] = "frozen"
            row["detail"] = "trend break, còn %d lot" % len(active)
            continue
        if (anchor is None or grid.get("rebuild_pending")
                or abs(px / _f(anchor, px) - 1) > rng):
            if active:
                row["code"] = "frozen"
                row["detail"] = "giá ra khỏi biên anchor, còn %d lot" % (
                    len(active))
                continue
            anchor, grid = px, {}       # bot se dat lai anchor = gia hien tai
        anchor = _f(anchor, px)
        if max_syms > 0 and not active and len(busy) >= max_syms:
            row["code"] = "max_symbols"
            row["detail"] = "%d/%d symbol có lot" % (len(busy), max_syms)
            continue
        why = scanner_block(sym) if scanner_block else None
        if why:
            row["code"] = "scanner"
            row["detail"] = why
            continue
        if n_grid >= max_pos or len(st.get("positions", [])) >= max_total:
            row["code"] = "full"
            continue
        hit, blocked, notes = [], [], []
        for side in ("long", "short"):
            lv = _next_level(grid, anchor, step, levels, side)
            if lv is None:
                continue
            gap = (px - lv[1]) / px if side == "long" else (lv[1] - px) / px
            row[side + "_gap_pct"] = round(gap * 100, 3)
            block = side_cap[side] or (trend_block(sym, side)
                                       if trend_block else None)
            if gap <= 0:
                (blocked if block else hit).append(side)
                if block:
                    notes.append("%s: %s" % (side.upper(), block))
            elif block:
                notes.append("%s sẽ bị chặn: %s" % (side.upper(),
                                                     block.split(" (")[0]))
        if row["long_gap_pct"] is None and row["short_gap_pct"] is None:
            row["code"] = "levels_full"
        elif hit:
            row["code"] = "ready"
        elif blocked:
            row["code"] = "side_blocked"
        else:
            row["code"] = "wait_price"
        gaps = []
        for side, word in (("long", "giảm"), ("short", "tăng")):
            v = row[side + "_gap_pct"]
            if v is not None and v > 0:
                gaps.append("%s cần %s %.2f%%" % (side.upper(), word, v))
        row["detail"] = "; ".join(gaps + notes)
        if row["code"] == "wait_price":
            for side in ("long", "short"):
                v = row[side + "_gap_pct"]
                if v is None or v <= 0 or side_cap[side] or (
                        trend_block and trend_block(sym, side)):
                    continue
                best = out["nearest"]
                if best is None or v < best["gap_pct"]:
                    out["nearest"] = {"symbol": sym, "side": side,
                                      "gap_pct": v}
    for row in out["symbols"]:
        out["counts"][row["code"]] = out["counts"].get(row["code"], 0) + 1
    return out


def summary_line(diag: dict) -> str:
    """1 dong log: 'GRID WAIT ...'."""
    parts = []
    if diag.get("global"):
        parts.append("TOAN CUC: " + "; ".join(diag["global"]))
    order = list(LABELS)
    counts = diag.get("counts") or {}
    cnt = ", ".join("%s %d" % (LABELS.get(k, k), counts[k])
                    for k in sorted(counts, key=lambda k: order.index(k)
                                    if k in order else 99))
    if cnt:
        parts.append(cnt)
    near = diag.get("nearest")
    if near:
        parts.append("gần nhất %s %s cách %.2f%%" % (
            near["symbol"], near["side"].upper(), near["gap_pct"]))
    blocked = [r for r in diag.get("symbols", [])
               if r["code"] in ("side_blocked", "ready")][:3]
    for r in blocked:
        parts.append("%s %s: %s" % (r["symbol"], LABELS[r["code"]],
                                    r["detail"][:120]))
    return "GRID WAIT %d symbol | %s" % (len(diag.get("symbols", [])),
                                         " | ".join(parts) or "-")


# --------------------------------------------------------------- CLI (VPS)
def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def effective_config(base: str) -> tuple:
    """config.json + version config dang chay (runtime_config.cache.json,
    bot ghi lai moi lan ap dung version tu DB). -> (cfg, version|None)."""
    cfg = _load_json(os.path.join(base, "config.json"))
    if cfg is None:
        raise SystemExit("khong thay %s/config.json" % base)
    cache = _load_json(os.path.join(base, "runtime_config.cache.json"), {})
    flat = (cache or {}).get("config") or {}
    if flat:
        db_dir = os.path.join(os.path.dirname(base), "db")
        if db_dir not in sys.path:
            sys.path.insert(0, db_dir)
        import bot_config
        clean, _errors = bot_config.validate(flat)
        bot_config.apply(cfg, clean)
    return cfg, (cache or {}).get("version")


def fetch_prices(symbols) -> Dict[str, float]:
    import requests
    r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price",
                     timeout=10)
    r.raise_for_status()
    want = set(symbols)
    return {x["symbol"]: float(x["price"]) for x in r.json()
            if x.get("symbol") in want}


def side_cap_reason(st: dict, cfg: dict, side: str) -> Optional[str]:
    """Ban sao chi-doc cua binance_bot.grid_side_cap_block."""
    cap = int((cfg.get("grid") or {}).get("max_same_side") or 0)
    if cap <= 0:
        return None
    term = ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")
    n = sum(1 for p in _grid_lots(st) if p.get("side") == side)
    n += sum(1 for o in st.get("entry_orders", []) if o.get("side") == side
             and o.get("tag", "grid") == "grid" and o.get("status") not in term)
    if n >= cap:
        return "trần %d lot grid %s cùng lúc (đang có %d, gồm lệnh chờ)" % (
            cap, side.upper(), n)
    return None


def _age(ts, now):
    if not ts:
        return "?"
    s = max(0, now - float(ts))
    return "%dm" % (s // 60) if s < 7200 else "%.1fh" % (s / 3600)


def run_cli(argv=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default=HERE, help="thu muc binance-bot")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-fetch", action="store_true",
                    help="khong goi Binance; gia lay tu snapshot trend")
    ap.add_argument("--symbol", action="append", help="chi xem symbol nay")
    a = ap.parse_args(argv)
    base = os.path.abspath(a.base)
    now = time.time()
    cfg, version = effective_config(base)
    uni = _load_json(os.path.join(base, cfg.get("universe_file",
                                                "universe.json")), [])
    symbols = [u["symbol"] for u in uni]
    st = _load_json(os.path.join(base, "state.json"))
    if st is None:
        raise SystemExit("khong thay state.json (bot chua chay?)")
    for p in _grid_lots(st):
        if p.get("symbol") not in symbols:
            symbols.append(p["symbol"])
    manage_only = set(symbols) - {u["symbol"] for u in uni}
    if a.symbol:
        want = {s.upper() for s in a.symbol}
        symbols = [s for s in symbols if s in want]
    trend_snap = st.get("trend") or {}
    if a.no_fetch:
        prices = {s: _f(r.get("price")) for s, r in
                  (trend_snap.get("symbols") or {}).items() if r.get("price")}
    else:
        prices = fetch_prices(symbols)

    from scanner import ScannerRunner
    from trend_filter import TrendFilter
    scan_path = os.path.join(base, "scanner_latest.json")
    scanner = ScannerRunner(cfg, fetch=None, path=scan_path,
                            log=lambda *_: None, clock=lambda: now)
    trend = TrendFilter(cfg, fetch=None, log=lambda *_: None,
                        clock=lambda: now)
    trend.status = dict(trend_snap.get("symbols") or {})
    universe = [u["symbol"] for u in uni]
    diag = explain_classic(
        st, cfg, symbols, prices,
        paused=os.path.exists(os.path.join(base, "PAUSE")),
        scanner_block=lambda s: scanner.block_reason(s, universe),
        trend_block=trend.blocks,
        side_cap_block=lambda side: side_cap_reason(st, cfg, side),
        manage_only=manage_only, now=now)
    if a.json:
        out(json.dumps(diag, ensure_ascii=False, indent=1))
        return 0

    g = cfg.get("grid") or {}
    sc = scanner.scfg()
    scan_file = _load_json(scan_path, {}) or {}
    passed = [r["symbol"] for r in scan_file.get("results", [])
              if r.get("passed")]
    state_ts = os.path.getmtime(os.path.join(base, "state.json"))
    out("== GRID DIAG %s (chi doc) ==" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("state.json ghi %s truoc · config version %s · engine %s · mode %s"
        % (_age(state_ts, now), version, g.get("engine", "classic"),
           cfg.get("mode", "?")))
    out("halted=%s %s · PAUSE=%s · lot grid %d/%d · max_symbols %s · "
        "max_same_side %s" % (
            st.get("halted"), st.get("halt_reason") or "",
            os.path.exists(os.path.join(base, "PAUSE")), len(_grid_lots(st)),
            int(g.get("max_positions") or 0), g.get("max_symbols"),
            g.get("max_same_side")))
    out("grid: step %s (ATR %s-%s) · %s tang/phia · range_steps %s · ADX "
        "trending > %s, ve ranging < %s" % (
            g.get("step_pct"), g.get("step_min"), g.get("step_max"),
            g.get("levels_each_side"), g.get("range_steps"),
            cfg.get("adx_threshold"), cfg.get("adx_range_threshold")))
    out("scanner: enabled=%s mode=%s top_k=%s · file %s · dat chuan %d: %s"
        % (sc.get("enabled"), sc.get("mode"), sc.get("top_k"),
           _age(scan_file.get("ts"), now) if scan_file else "KHONG CO",
           len(passed), ", ".join(passed[:10])))
    t = trend.tcfg()
    mrec = trend.status.get(trend.market_symbol()) or {}
    out("trend: market_filter=%s symbol_filter=%s · %s bias=%s (%s) · "
        "snapshot %s" % (t.get("market_filter"), t.get("symbol_filter"),
                         trend.market_symbol(), mrec.get("bias"),
                         mrec.get("reason", ""), _age(trend_snap.get("ts"),
                                                      now)))
    out("")
    out("%-14s %-30s %8s %8s  %s" % ("SYMBOL", "LY DO", "LONG%", "SHORT%",
                                     "CHI TIET"))
    for r in diag["symbols"]:
        gl = "" if r["long_gap_pct"] is None else "%.2f" % r["long_gap_pct"]
        gs = "" if r["short_gap_pct"] is None else "%.2f" % r["short_gap_pct"]
        out("%-14s %-30s %8s %8s  %s" % (r["symbol"], LABELS.get(r["code"],
                                         r["code"]), gl, gs, r["detail"]))
    out("")
    out(summary_line(diag))
    out("(LONG%/SHORT%: gia con phai giam/tang bao nhieu % moi cham tang "
        "ke tiep; <=0 = da cham)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    sys.exit(run_cli())
