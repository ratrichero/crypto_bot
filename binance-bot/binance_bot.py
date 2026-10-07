#!/usr/bin/env python3
"""Multi-symbol Binance USDT-M futures bot: scalp 5m/15m + two-way grid.

Port of trading-bot/bot.py (OKX) to Binance. Strategy rules are identical;
only the venue layer differs (see live_binance.py for Binance specifics).

Price ticks arrive real-time via websocket (miniTicker stream). SL/TP +
grid triggers are checked on a fast loop (~0.5s); candle/strategy work runs
on a slow loop. Kill switch: create file STOP in this dir.

Modes (config "mode"): "data_only" | "dry_run" | "live"
(default "dry_run").
  data_only: public market data/candles and signal calculations only; no
             simulated positions and no authenticated calls.
  dry_run: signals flow through BinanceEngine but ONLY LOGGED, zero
           authenticated calls, no API key needed.
  live:    places real orders on Binance, needs env BINANCE_API_KEY /
           BINANCE_API_SECRET. DOI MODE CAN RESTART BOT.
"""
import json
import os
import signal
import time
import traceback
from datetime import datetime, timezone

import binance_client
import binance_safety
import range_grid
import runtime_config
import scanner as range_scanner
import strategy
import trend_filter
from indicators import atr
from binance_ws import BinanceWS

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json")))
UNI = json.load(open(os.path.join(BASE, CFG.get("universe_file", "universe.json"))))
SYMBOLS = [u["symbol"] for u in UNI]
STATE_P = os.path.join(BASE, "state.json")
TRADES_P = os.path.join(BASE, "trades.jsonl")
LOG_P = os.path.join(BASE, "bot.log")
STOP_P = os.path.join(BASE, "STOP")
# Dashboard "Xoa halt" tao file nay; bot kiem tra an toan roi go halt trong
# RAM (dashboard sua thang state.json se bi bot ghi de / de mat lot moi).
CLEAR_HALT_P = os.path.join(BASE, "CLEAR_HALT")
CLEAR_HALT_RESULT_P = os.path.join(BASE, "clear_halt_result.json")

FAST_POLL = CFG.get("fast_poll_seconds", 0.5)
SLOW_EVERY = 10          # slow tasks every N fast loops (~5s)
CANDLE_PER_SLOW = 2      # symbols refreshed per slow tick
WARMUP_DELAY = float(CFG.get("warmup_delay_seconds", 0.75))
MODE = CFG.get("mode", "dry_run")
DATA_ONLY = MODE == "data_only"
LOCK_P = os.path.join(BASE, "bot.lock")
CIRCUIT_P = os.path.join(BASE, "binance_circuit.json")
SCANNER_P = os.path.join(BASE, "scanner_latest.json")
RUNTIME = None           # runtime_config.RuntimeConfig (tao trong main)
SCANNER = None           # scanner.ScannerRunner (tao trong main)
TREND = None             # trend_filter.TrendFilter (tao trong main)


def make_engine(st):
    if MODE == "data_only":
        from live_binance import DataOnlyEngine
        return DataOnlyEngine(log=log)
    if MODE in ("dry_run", "live"):
        from live_binance import BinanceEngine
        eng = BinanceEngine(CFG, st, dry_run=(MODE == "dry_run"),
                            log=log, symbols=SYMBOLS)
        # Lenh entry LIMIT duoc ghi state TRUOC khi gui (G5).
        eng.persist_cb = lambda: save_state(st)
        # Khoi tao ket noi Postgres de ghi trade truc tiep
        # (khong fail neu DB khong dung duoc; JSONL van la backup)
        try:
            eng.init_db()
        except Exception as e:
            log(f"DB init warning: {e}")
        return eng
    raise SystemExit(
        "config 'mode' khong hop le: %r (chon data_only|dry_run|live)" %
        (MODE,))


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


def utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def default_state():
    return {
        "equity": CFG["start_equity"],
        "positions": [],
        "grids": {},
        "regimes": {},
        "day": utc_day(),
        "day_start_equity": CFG["start_equity"],
        "intraday_peak_equity": CFG["start_equity"],
        "mark_equity": CFG["start_equity"],
        "unrealized_pnl": 0.0,
        "daily_drawdown_pct": 0.0,
        "_risk_initialized": False,
        "signal_bars": {},
        "halted": False,
        "halt_reason": "",
        "cooldown_until": 0,
        "stats": {"trades": 0, "wins": 0, "losses": 0, "fees": 0.0},
        "_pid": 0,
        "_client_nonce": 0,
    }


def load_state():
    if os.path.exists(STATE_P):
        try:
            st = json.load(open(STATE_P))
            d = default_state()
            d.update(st)
            d.setdefault("grids", {})
            d.setdefault("regimes", {})
            d.setdefault("signal_bars", {})
            d.setdefault("intraday_peak_equity", d.get("day_start_equity", d["equity"]))
            d.setdefault("mark_equity", d.get("equity", 0.0))
            d.setdefault("unrealized_pnl", 0.0)
            d.setdefault("daily_drawdown_pct", 0.0)
            d.setdefault("_risk_initialized", False)
            return d
        except Exception:
            pass
    return default_state()


def save_state(st):
    """Atomically persist state; stop opening risk if persistence fails."""
    tmp = STATE_P + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(st, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_P)
    except Exception:
        st["halted"] = True
        st["halt_reason"] = "state persistence failure"
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def record_trade(rec):
    with open(TRADES_P, "a") as f:
        f.write(json.dumps(rec) + "\n")


def rest_tickers_fallback():
    """Low-weight REST fallback; never hide a rate-limit/circuit signal."""
    try:
        rows = binance_client.get_symbol_prices()
        want = set(SYMBOLS)
        return {r["symbol"]: float(r["price"]) for r in rows
                if r["symbol"] in want and r.get("price")}
    except binance_safety.BinanceSafetyStop:
        raise
    except Exception as e:
        log(f"fallback prices failed: {e}")
        return {}


def is_disabled(symbol):
    return CFG.get("disabled_symbols", {}).get(symbol, 0) > time.time()


def _record_close(st, rec):
    """Persist a close and update grid/scalp bookkeeping."""
    try:
        record_trade(rec)
    except Exception:
        st["halted"] = True
        st["halt_reason"] = "state persistence failure"
        raise
    symbol = rec["symbol"]
    g = st["grids"].get(symbol)
    if g:
        for key, pid in list(g.get("taken", {}).items()):
            if pid == rec["id"]:
                g["taken"].pop(key, None)
                break
    log(f"CLOSE #{rec['id']} {symbol} {rec['side']} {rec['tag']} "
        f"{rec['reason']} pnl={rec['pnl']:+.2f} eq={st['equity']:.2f}")
    if rec["tag"] == "scalp" and rec["reason"] == "SL":
        st["cooldown_until"] = (time.time()
                               + CFG["scalp"]["cooldown_after_sl_min"] * 60)
        log("Scalp cooldown 30p sau SL")


def update_positions(engine, st, symbol, price):
    """Tick-driven SL/TP check using the mark price.

    With an armed exchange guard Binance executes the exit itself; the bot
    only falls back to its own market close after a grace period
    (engine.defer_local_exit), so both sides never race the same trigger.
    """
    closed = []
    defer = getattr(engine, "defer_local_exit", None)
    clear = getattr(engine, "clear_local_trigger", None)
    for pos in [p for p in st["positions"] if p["symbol"] == symbol]:
        label = None
        if pos["side"] == "long":
            if pos["sl"] and price <= pos["sl"]:
                label = "sl"
            elif pos["tp"] and price >= pos["tp"]:
                label = "tp"
        else:
            if pos["sl"] and price >= pos["sl"]:
                label = "sl"
            elif pos["tp"] and price <= pos["tp"]:
                label = "tp"
        if label is None:
            if clear:
                clear(pos)
            continue
        if defer and defer(pos, label):
            continue
        rec = engine.close(pos, pos[label], label.upper())
        if rec:
            if clear:
                clear(pos)
            closed.append(rec)
    for rec in closed:
        _record_close(st, rec)
    return bool(closed)


def manage_scalp(engine, st, symbol, price, c5, c15):
    s = CFG["scalp"]
    n_scalp = sum(1 for p in st["positions"] if p["tag"] == "scalp")
    if n_scalp >= s["max_positions"]:
        return False
    if time.time() < st["cooldown_until"]:
        return False
    closed5 = c5[:-1] if len(c5) > 1 else c5
    bar_ts = closed5[-1].get("ts") if closed5 else None
    signal_bars = st.setdefault("signal_bars", {})
    if bar_ts is not None and signal_bars.get(symbol) == bar_ts:
        return False
    if bar_ts is not None:
        # Consume a closed candle once, even if it has no signal or the order
        # is rejected. This prevents re-opening the same breakout after TP.
        signal_bars[symbol] = bar_ts
    sig, info = strategy.scalp_signal(c5, c15, CFG)
    if not sig:
        return False
    if any(p["tag"] == "scalp" and p["symbol"] == symbol and p["side"] == sig
           for p in st["positions"]):
        return False
    if any(p["tag"] == "scalp" and p["symbol"] == symbol and p["side"] != sig
           for p in st["positions"]):
        return False
    notional = CFG["order_margin_usdt"] * CFG["leverage"]
    pos, why = engine.open(symbol, sig, notional, price,
                           s["sl_pct"], s["tp_pct"], "scalp")
    if pos:
        log(f"OPEN #{pos['id']} {symbol} scalp {sig} entry={pos['entry']:.4f} "
            f"sl={pos['sl']:.4f} tp={pos['tp']:.4f} info={info}")
        return True
    if str(why).startswith(("cooldown", "symbol cooldown")):
        return False
    log(f"Scalp {symbol} {sig} rejected: {why}")
    return False


# Dung sach khi nhan SIGINT/SIGTERM (pm2 stop/restart, Ctrl+C): chi dat co,
# vong lap chinh thoat o dau vong ke tiep giong file STOP (luu state, dong WS)
# -> khong bi cat ngang giua luc dat/huy lenh. Tin hieu thu 2 -> dung ngay.
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


def acquire_instance_lock():
    """Prevent two bot copies from sharing the same IP/request budget."""
    try:
        import fcntl
    except ImportError:
        log("WARNING fcntl unavailable; cannot enforce single-instance lock")
        return None
    fh = open(LOCK_P, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit(
            "binance-bot dang chay (lock=%s); khong khoi dong them instance" %
            LOCK_P
        )
    fh.seek(0)
    fh.truncate()
    fh.write("pid=%d\\n" % os.getpid())
    fh.flush()
    return fh


# Symbol co lot dang mo nhung khong (con) nam trong universe: van theo doi
# gia/rui ro, KHONG mo lenh moi (universe build lai theo volume 24h).
MANAGE_ONLY = set()


def include_position_symbols(st):
    """Them symbol cua lot dang mo vao SYMBOLS (manage-only) truoc khi tao
    engine/WS. Truoc day lot cua coin rot khoi universe sau restart khong co
    gia mark -> mat ca basket stop lan SL/TP local."""
    extra = sorted({p.get("symbol") for p in st.get("positions", [])
                    if p.get("symbol")} - set(SYMBOLS))
    for symbol in extra:
        SYMBOLS.append(symbol)
        MANAGE_ONLY.add(symbol)
    if extra:
        log("WARNING lot dang mo tren symbol ngoai universe: %s -> theo doi "
            "rui ro, khong mo lenh moi" % ", ".join(extra))
    return extra


def handle_clear_halt_request(engine, st):
    """Xu ly yeu cau go halt tu dashboard. Tra ve True neu state doi."""
    try:
        os.remove(CLEAR_HALT_P)
    except FileNotFoundError:
        return False
    reason = str(st.get("halt_reason") or "")
    if not st.get("halted"):
        ok, msg = True, "khong halt"
    elif reason.startswith("daily stop") and st.get("daily_stop_day") == utc_day():
        ok, msg = False, ("daily stop trong ngay - tu go sang ngay UTC moi "
                          "(go tay se cho giao dich tiep du da cham tran lo)")
    else:
        try:
            ok, msg = engine.try_clear_halt()
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception as e:
            ok, msg = False, "loi kiem tra: %s" % e
    log("CLEAR_HALT request: halt='%s' -> %s (%s)"
        % (reason, "OK" if ok else "TU CHOI", msg))
    try:
        with open(CLEAR_HALT_RESULT_P, "w") as f:
            json.dump({"ts": time.time(), "ok": ok, "halt_reason": reason,
                       "message": msg}, f)
    except OSError as e:
        log("WARNING khong ghi duoc %s: %s" % (CLEAR_HALT_RESULT_P, e))
    return True


def run_risk_controls(engine, st, mark_prices, prices, reconcile_hold):
    """Moi co che CAT LO chay moi vong, khong phu thuoc halt.

    - Grid basket stop theo symbol + tran lo tong grid (manage_grid_risk).
    - SL/TP local (fallback khi guard san chua khop / chua co).
    - Dang reconcile hold: dat lai SL/TP thieu (arm_only).
    Lenh dong o Hedge Mode luon theo positionSide (chi giam vi the) nen an
    toan ca khi state lech san. Moi buoc boc rieng: 1 buoc loi khong chan
    buoc khac. Tra ve True neu state thay doi."""
    changed = False
    try:
        if manage_grid_risk(engine, st, mark_prices):
            changed = True
    except binance_safety.BinanceSafetyStop:
        raise
    except Exception:
        log("manage_grid_risk loi:\n" + traceback.format_exc())
    try:
        if range_grid_risk(engine, st, mark_prices):
            changed = True
    except binance_safety.BinanceSafetyStop:
        raise
    except Exception:
        log("range_grid_risk loi:\n" + traceback.format_exc())
    symbols = list(dict.fromkeys(
        list(SYMBOLS) + [p.get("symbol") for p in st.get("positions", [])]))
    for symbol in symbols:
        mark = mark_prices.get(symbol, prices.get(symbol))
        if mark is None:
            continue
        try:
            if update_positions(engine, st, symbol, mark):
                changed = True
        except binance_safety.BinanceSafetyStop:
            raise
        except Exception:
            log("update_positions %s loi:\n%s" % (symbol,
                                                  traceback.format_exc()))
    if reconcile_hold:
        # State lech san: khong don lenh/khong ep dong theo deadline, nhung
        # lot thieu SL/TP van phai duoc dat chan (dong-only, giam rui ro).
        if protect_during_hold(engine):
            changed = True
    return changed


def manage_grid_risk(engine, st, mark_prices):
    """Stop a grid basket before an unbounded one-way move consumes equity."""
    limit_pct = float(CFG["risk"].get("grid_basket_max_loss_pct", 0.0))
    if limit_pct <= 0:
        return False
    base_equity = float(st.get("mark_equity", st.get("equity", 0.0)) or 0.0)
    if base_equity <= 0:
        return False
    changed = False
    grid_symbols = set(st.get("grids", {})) | {
        p.get("symbol") for p in st["positions"] if p.get("tag") == "grid"
    }
    for symbol in sorted(s for s in grid_symbols if s):
        grid = st["grids"].setdefault(
            symbol, {"anchor": None, "taken": {}}
        )
        positions = [p for p in st["positions"]
                     if p.get("tag") == "grid" and p.get("symbol") == symbol]
        if grid.get("basket_stopping"):
            # Basket stop da kich hoat: thu lai dong lot con lai moi vong
            # (truoc day lot close() tra None khong bao gio duoc thu lai).
            price = mark_prices.get(symbol)
            if positions and price is not None:
                for pos in list(positions):
                    rec = engine.close(pos, price, "GRID_BASKET_STOP")
                    if rec:
                        _record_close(st, rec)
                        changed = True
            if not [p for p in st["positions"] if p.get("tag") == "grid"
                    and p.get("symbol") == symbol]:
                grid.pop("basket_stopping", None)
                log("GRID BASKET STOP %s: da dong het lot" % symbol)
                changed = True
            continue
        # risk_halted chi chan MO lenh moi (manage_grid); lot con ton tai
        # (dong loi, nhan lai tu lenh mo ho...) van phai duoc danh gia.
        if not positions:
            continue
        price = mark_prices.get(symbol)
        if price is None:
            log("WARNING grid basket %s: khong co gia mark -> khong danh gia "
                "duoc (SL tren san van con)" % symbol)
            continue
        pnl = 0.0
        for pos in positions:
            if pos["side"] == "long":
                pnl += (price - pos["entry"]) * pos["qty"]
            else:
                pnl += (pos["entry"] - price) * pos["qty"]
        loss_limit = base_equity * limit_pct
        if pnl > -loss_limit:
            continue
        grid["risk_halted"] = True
        grid["rebuild_pending"] = True
        log("GRID BASKET STOP %s pnl=%+.2f limit=-%.2f positions=%d" %
            (symbol, pnl, loss_limit, len(positions)))
        for pos in list(positions):
            rec = engine.close(pos, price, "GRID_BASKET_STOP")
            if rec:
                _record_close(st, rec)
        changed = True
        if [p for p in st["positions"] if p.get("tag") == "grid"
                and p.get("symbol") == symbol]:
            grid["basket_stopping"] = True
            log("GRID BASKET STOP %s: con lot chua dong -> thu lai moi vong"
                % symbol)
        grid["taken"] = {}
    if enforce_grid_total_stop(engine, st, mark_prices, base_equity):
        changed = True
    return changed


def enforce_grid_total_stop(engine, st, mark_prices, base_equity):
    """Tran lo TONG cua moi lot grid (moi symbol cong lai), theo % equity.

    Basket stop theo tung symbol khong chan duoc lo tuong quan (thi truong
    sap -> nhieu symbol cung lo). Cham tran -> dong moi lot grid, dung grid
    moi symbol (risk_halted) toi ngay moi. risk.grid_total_max_loss_pct
    (mac dinh 0.10; 0 = tat)."""
    limit_pct = float(CFG["risk"].get("grid_total_max_loss_pct", 0.10))
    if limit_pct <= 0 or base_equity <= 0:
        return False
    positions = [p for p in st["positions"] if p.get("tag") == "grid"]
    if not positions:
        return False
    pnl = 0.0
    for pos in positions:
        price = mark_prices.get(pos["symbol"])
        if price is None:
            continue
        if pos["side"] == "long":
            pnl += (price - pos["entry"]) * pos["qty"]
        else:
            pnl += (pos["entry"] - price) * pos["qty"]
    limit = base_equity * limit_pct
    if pnl > -limit:
        return False
    log("GRID TOTAL STOP pnl=%+.2f limit=-%.2f lots=%d -> dong moi lot grid"
        % (pnl, limit, len(positions)))
    for symbol in sorted({p["symbol"] for p in positions}):
        grid = st["grids"].setdefault(symbol, {"anchor": None, "taken": {}})
        grid["risk_halted"] = True
        grid["rebuild_pending"] = True
        grid["taken"] = {}
        for pos in [p for p in positions if p["symbol"] == symbol]:
            price = mark_prices.get(symbol, pos["entry"])
            rec = engine.close(pos, price, "GRID_TOTAL_STOP")
            if rec:
                _record_close(st, rec)
        if [p for p in st["positions"] if p.get("tag") == "grid"
                and p.get("symbol") == symbol]:
            grid["basket_stopping"] = True     # thu lai moi vong
    return True


def grid_entry_allowed(st, symbol, has_lots):
    """Gioi han mo lot grid moi (khong anh huong quan ly lot dang mo):
    - grid.max_symbols: symbol CHUA co lot khong duoc vao khi so symbol
      dang co lot grid da dat gioi han (0 = khong gioi han).
    - scanner che do filter: chi symbol dat chuan + top K, du lieu het han
      -> chan (fail-closed)."""
    limit = int(CFG["grid"].get("max_symbols") or 0)
    if limit > 0 and not has_lots:
        busy = {p.get("symbol") for p in st["positions"]
                if p.get("tag") == "grid"}
        if len(busy) >= limit:
            return False
    if SCANNER is not None and not SCANNER.allows(symbol, SYMBOLS):
        return False
    return True


_GRID_BLOCK_LOG: dict = {}


def grid_trend_block(symbol, side):
    """Loc chieu xu huong (task 34): ly do chan mo lot grid `side` moi tren
    `symbol`, None = duoc. A = xu huong BTC (moi symbol), C = xu huong rieng
    symbol. Chi chan MO MOI, khong dung toi lot dang mo."""
    if TREND is None:
        return None
    return TREND.blocks(symbol, side)


def grid_side_count(st, side):
    """So lot grid + lenh entry grid dang cho (G5) cung chieu, moi symbol."""
    lots = sum(1 for p in _grid_lots(st) if p.get("side") == side)
    pend = sum(1 for o in _pending_entries(st)
               if o.get("side") == side and o.get("tag", "grid") == "grid"
               and o.get("status") not in ENTRY_TERMINAL)
    return lots + pend


def grid_side_cap_block(st, side):
    """Tran lot grid cung chieu tren TOAN tai khoan (grid.max_same_side,
    0 = tat). Altcoin tuong quan cao: 9 lot long tren 8 coin = 1 lenh cuoc
    lon vao chieu tang -> gioi han so lot cung chieu."""
    cap = int(CFG["grid"].get("max_same_side") or 0)
    if cap <= 0:
        return None
    n = grid_side_count(st, side)
    if n >= cap:
        return "trần %d lot grid %s cùng lúc (đang có %d, gồm lệnh chờ)" % (
            cap, side.upper(), n)
    return None


def _note_block(key, label, side, why):
    """Log 1 lan moi khi ly do chan doi (khong spam moi 0.5s). True = chan."""
    if why:
        short = why.split(" (")[0]
        if _GRID_BLOCK_LOG.get(key) != short:
            log("GRID %s khong mo %s: %s" % (label, side.upper(), why))
            _GRID_BLOCK_LOG[key] = short
        return True
    if _GRID_BLOCK_LOG.pop(key, None) is not None:
        log("GRID %s mo lai phia %s" % (label, side.upper()))
    return False


def grid_side_allowed(st, symbol, side, cap=True):
    """Duoc mo lot grid `side` moi tren `symbol`? cap=False: bo qua tran
    cung chieu (range limit tu phan slot theo chieu trong plan_slots)."""
    if cap and _note_block(("*", side), "(moi symbol)", side,
                           grid_side_cap_block(st, side)):
        return False
    return not _note_block((symbol, side), symbol, side,
                           grid_trend_block(symbol, side))


def manage_grid(engine, st, symbol, price):
    g = CFG["grid"]
    grid = st["grids"].setdefault(symbol, {"anchor": None, "taken": {}})
    grid.setdefault("anchor", None)
    grid.setdefault("taken", {})
    if grid.get("risk_halted"):
        return False
    step = grid.get("step") or g["step_pct"]
    rng = g["range_steps"] * step
    changed = False
    active = [p for p in st["positions"]
              if p.get("tag") == "grid" and p.get("symbol") == symbol]
    if any(str(p.get("level") or "").startswith("r") for p in active):
        # Lot cua range grid (doi grid.engine khi chua flat): khong chong
        # grid classic len; lot cu chay toi TP/SL/basket.
        return False
    # Preserve level ownership across an anchor cycle. Never forget live lots.
    for pos in active:
        if pos.get("level") is not None:
            grid.setdefault("taken", {}).setdefault(pos["level"], pos["id"])
    if grid.get("rebuild_pending") and not active:
        grid["anchor"] = price
        grid["taken"] = {}
        grid["rebuild_pending"] = False
        log(f"Grid {symbol} rebuild anchor={price:.4f}")
        changed = True
    elif grid["anchor"] is None:
        grid["anchor"] = price
        grid["rebuild_pending"] = False
        log(f"Grid {symbol} rebuild anchor={price:.4f}")
        changed = True
    elif abs(price / grid["anchor"] - 1) > rng:
        if active:
            # Existing lots must be flattened or reach their exits before a
            # new anchor is allowed; otherwise old and new grids overlap.
            if not grid.get("rebuild_pending"):
                log("Grid %s trend break; freeze new levels until flat" % symbol)
            grid["rebuild_pending"] = True
            return changed
        grid["anchor"] = price
        grid["taken"] = {}
        grid["rebuild_pending"] = False
        log(f"Grid {symbol} rebuild anchor={price:.4f}")
        changed = True
    if grid.get("rebuild_pending"):
        return changed
    if not grid_entry_allowed(st, symbol, bool(active)):
        return changed
    anchor = grid["anchor"]
    n_grid = sum(1 for p in st["positions"] if p["tag"] == "grid")
    notional = CFG["order_margin_usdt"] * CFG["leverage"]
    # TP/SL moi lot tu config runtime (dashboard). tp_pct = 0 -> TP = 1 step
    # (hanh vi cu). SL tren san truoc day hard-code 3%.
    tp_pct = float(g.get("tp_pct") or 0) or step
    sl_pct = float(g.get("sl_pct") or 0.03)
    entries = 0
    max_entries = int(g.get("max_entries_per_cycle", 1))
    for k in range(1, g["levels_each_side"] + 1):
        if entries >= max_entries or n_grid >= g["max_positions"]:
            break
        if len(st["positions"]) >= CFG["max_total_positions"]:
            break
        bk = f"b{k}"
        if (price <= anchor * (1 - k * step) and bk not in grid["taken"]
                and grid_side_allowed(st, symbol, "long")):
            pos, _ = engine.open(symbol, "long", notional,
                                 anchor * (1 - k * step),
                                 sl_pct, tp_pct, "grid", level=bk)
            if pos:
                grid["taken"][bk] = pos["id"]
                n_grid += 1
                entries += 1
                changed = True
                log(f"OPEN #{pos['id']} {symbol} grid BUY k={k} @ {pos['entry']:.4f}")
        if entries >= max_entries:
            break
        sk = f"s{k}"
        if (price >= anchor * (1 + k * step) and sk not in grid["taken"]
                and grid_side_allowed(st, symbol, "short")):
            pos, _ = engine.open(symbol, "short", notional,
                                 anchor * (1 + k * step),
                                 sl_pct, tp_pct, "grid", level=sk)
            if pos:
                grid["taken"][sk] = pos["id"]
                n_grid += 1
                entries += 1
                changed = True
                log(f"OPEN #{pos['id']} {symbol} grid SELL k={k} @ {pos['entry']:.4f}")
    return changed


def grid_engine():
    """grid.engine: classic (anchor, theo regime 15m) | range (G4)."""
    return str(CFG["grid"].get("engine") or "classic")


def _grid_lots(st, symbol=None):
    return [p for p in st["positions"] if p.get("tag") == "grid"
            and (symbol is None or p.get("symbol") == symbol)]


def range_scan(symbol):
    """Ket qua scanner CON MOI cua symbol (None neu thieu/het han)."""
    if SCANNER is None:
        return None
    res = SCANNER.results.get(symbol)
    if not res or SCANNER.clock() - float(res.get("ts", 0)) > SCANNER.max_age():
        return None
    return res


def range_allowed_symbols():
    """Range grid: symbol DAT chuan + top K, khong phu thuoc scanner.mode
    (range grid can bien cua scanner nen luon loc). Scanner tat / chua co
    ket qua -> rong (fail-closed: khong mo moi)."""
    if SCANNER is None:
        return set()
    sc = SCANNER.scfg()
    if not sc.get("enabled", True):
        return set()
    uni = set(SYMBOLS)
    res = {k: v for k, v in SCANNER.results.items()
           if k in uni and not is_disabled(k) and k not in MANAGE_ONLY}
    g = CFG["grid"]
    thin = set()

    def eligible(r):
        # Dat chuan nhung bien qua hep so voi step -> duoi range_min_levels
        # tang/phia: khong cho chiem cho top K.
        ok = range_grid.range_tradable(r.get("metrics") or {}, g)
        if not ok:
            thin.add(r.get("symbol"))
        return ok
    out = set(range_scanner.allowed_symbols(
        res, int(sc["top_k"]), SCANNER.clock(), SCANNER.max_age(),
        eligible=eligible))
    global _RANGE_THIN
    if thin != _RANGE_THIN:
        added = sorted(s for s in thin - _RANGE_THIN if s)
        if added:
            log("RANGE: bo khoi top K (bien hep, < %d tang/phia): %s"
                % (range_grid.min_levels_required(g), ", ".join(added)))
        _RANGE_THIN = thin
    return out


_RANGE_THIN: set = set()


def range_slot_ok(st, symbol, pending=None):
    """Con slot mo lot range moi cho symbol? pending: {symbol: so lenh cho}
    (G5) - lenh cho tinh nhu lot de du khop het van khong vuot tran."""
    g = CFG["grid"]
    pending = pending or {}
    lots = _grid_lots(st)
    if len(lots) + sum(pending.values()) >= int(g["max_positions"]):
        return False
    n_sym = sum(1 for p in lots if p.get("symbol") == symbol) \
        + pending.get(symbol, 0)
    if n_sym >= int(g.get("max_lots_per_symbol") or 2):
        return False
    limit = int(g.get("max_symbols") or 0)
    busy = {p.get("symbol") for p in lots} | {k for k, v in pending.items()
                                              if v}
    if limit > 0 and symbol not in busy and len(busy) >= limit:
        return False
    return True


def range_grid_risk(engine, st, mark_prices):
    """Bien vo (gia ra ngoai bien qua break_buffer / ADX 1h > trend_exit_adx)
    -> danh dau broken (khong mo moi, huy lenh cho). derisk_on_trend: cat
    lot dang lo > derisk_loss_pct. Chay trong run_risk_controls (bat ke
    halt) vi day la co che giam rui ro."""
    g = CFG["grid"]
    changed = False
    for symbol, grid in st.get("grids", {}).items():
        rng = grid.get("range")
        price = mark_prices.get(symbol)
        if not rng or price is None:
            continue
        if not grid.get("broken"):
            scan = range_scan(symbol)
            why = range_grid.check_break(rng, price,
                                         scan and scan.get("metrics"), g)
            if why:
                grid["broken"] = True
                grid["broken_reason"] = why
                log("RANGE GRID %s VO BIEN: %s -> dung mo moi" % (symbol, why))
                changed = True
        if grid.get("broken"):
            for pos in range_grid.derisk_targets(_grid_lots(st, symbol),
                                                 price, g):
                rec = engine.close(pos, price, "GRID_DERISK")
                if rec:
                    _record_close(st, rec)
                    changed = True
    return changed


def manage_range_grid(engine, st, symbol, price, allowed):
    """Range grid 2 chieu (G4): bien tu scanner, long nua duoi / short nua
    tren, TP = grid.tp_pct, SL bien (khong xa hon grid.sl_pct).

    - Chi dung bien moi khi symbol FLAT + dat chuan top K + co scan moi hon
      bien cu: lot dang mo luon thuoc bien da sinh ra no.
    - Bien vo / roi top K / risk_halted -> khong mo moi (lot cu chay tiep).
    - Lot tag "grid" -> basket / tran tong / daily stop ap dung nhu cu."""
    g = CFG["grid"]
    grid = st["grids"].setdefault(symbol, {"anchor": None, "taken": {}})
    grid.setdefault("taken", {})
    active = _grid_lots(st, symbol)
    ids = {p["id"] for p in active}
    for pos in active:
        if pos.get("level"):
            grid["taken"].setdefault(pos["level"], pos["id"])
    grid["taken"] = {k: v for k, v in grid["taken"].items() if v in ids}
    changed = False
    flat = not active and not _pending_entries(st, symbol)
    if flat and grid.get("rebuild_pending"):
        # basket / tran tong / daily stop: bo bien cu, cho scan moi
        grid["rebuild_pending"] = False
        if grid.get("range") and not grid.get("broken"):
            grid["broken"] = True
            grid["broken_reason"] = "risk stop"
        changed = True
    if grid.get("risk_halted"):
        return changed
    scan = range_scan(symbol)
    rng = grid.get("range")
    if (flat and symbol in allowed and scan
            and (rng is None or float(rng.get("ts", 0))
                 < float(scan.get("ts", 0)))):
        m = scan.get("metrics") or {}
        new = range_grid.build_range(m, g, float(scan["ts"]))
        why = new and range_grid.check_break(new, price, m, g)
        if new and not why:
            grid.update(range=new, broken=False, anchor=None)
            grid.pop("broken_reason", None)
            log("RANGE GRID %s bien [%.6g, %.6g] step=%.2f%% %d tang score=%s"
                % (symbol, new["low"], new["high"], new["step"] * 100,
                   len(new["levels"]), scan.get("score")))
            changed = True
            rng = new
    if (rng is None or grid.get("broken") or symbol not in allowed
            or any(not str(p.get("level") or "").startswith("r")
                   for p in active)):
        return changed
    if limit_mode():
        return changed          # vao lenh bang LIMIT: manage_range_limits
    notional = CFG["order_margin_usdt"] * CFG["leverage"]
    entries = 0
    max_entries = int(g.get("max_entries_per_cycle", 1))
    for lv in range_grid.market_triggers(rng, price, grid["taken"]):
        if entries >= max_entries or not range_slot_ok(st, symbol):
            break
        if len(st["positions"]) >= CFG["max_total_positions"]:
            break
        if not grid_side_allowed(st, symbol, lv["side"]):
            continue
        sl, tp = range_grid.lot_exits(rng, lv["side"], price, g)
        pos, _ = engine.open(symbol, lv["side"], notional, price,
                             abs(sl / price - 1), abs(tp / price - 1),
                             "grid", level=lv["key"])
        if not pos:
            break
        grid["taken"][lv["key"]] = pos["id"]
        entries += 1
        changed = True
        log("OPEN #%s %s range %s %s @ %.6g (bien %.6g-%.6g)" % (
            pos["id"], symbol, lv["key"], lv["side"].upper(), pos["entry"],
            rng["low"], rng["high"]))
    return changed


ENTRY_TERMINAL = ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH",
                  "REJECTED")


def limit_mode():
    """Entry LIMIT post-only chi khi grid.engine=range + entry_mode=limit."""
    g = CFG["grid"]
    return (grid_engine() == "range"
            and str(g.get("entry_mode") or "market") == "limit")


def _pending_entries(st, symbol=None):
    return [o for o in st.get("entry_orders", [])
            if symbol is None or o.get("symbol") == symbol]


def cancel_all_entries(engine, st, reason):
    """Huy moi lenh entry LIMIT dang cho (halt / pause / doi che do)."""
    if not _pending_entries(st) or not hasattr(engine, "cancel_entries"):
        return False
    n = engine.cancel_entries(reason)
    if n:
        log("ENTRY: huy %d lenh cho (%s)" % (n, reason))
    return True


def manage_range_limits(engine, st, prices, allowed):
    """Entry LIMIT post-only cho range grid (G5).

    Moi vong: lap danh sach tang dat duoc (long duoi gia, short tren gia,
    cach >= limit_min_gap_pct) cua moi symbol hop le, phan slot bang
    range_grid.plan_slots (lot + lenh cho <= grid.max_positions,
    max_lots_per_symbol, max_symbols; uu tien giu lenh cu, roi tang gan
    gia). Lenh khong con trong ke hoach -> huy; tang moi -> dat toi da
    grid.max_new_orders_per_cycle lenh/vong."""
    g = CFG["grid"]
    pend = [o for o in _pending_entries(st)
            if o.get("status") not in ENTRY_TERMINAL]
    rows, eligible = [], set()
    trend_blocked = {}           # (symbol, side) -> ly do (loc xu huong)
    gap = float(g.get("limit_min_gap_pct", 0.0005))
    for symbol in SYMBOLS:
        grid = st["grids"].get(symbol) or {}
        rng = grid.get("range")
        px = prices.get(symbol)
        if (not rng or grid.get("broken") or grid.get("risk_halted")
                or symbol not in allowed or px is None
                or is_disabled(symbol) or symbol in MANAGE_ONLY):
            continue
        lots = _grid_lots(st, symbol)
        if any(not str(p.get("level") or "").startswith("r") for p in lots):
            continue
        taken = {p.get("level") for p in lots}
        cands = range_grid.limit_candidates(rng, px, taken, gap)
        have = {c["key"] for c in cands}
        mine = [o for o in pend if o["symbol"] == symbol]
        for o in mine:
            if o.get("level") not in have and o.get("level") not in taken:
                cands.append({"key": o["level"], "side": o["side"],
                              "price": o["price"],
                              "dist": abs(o["price"] / px - 1)})
        for sd in ("long", "short"):
            if not grid_side_allowed(st, symbol, sd, cap=False):
                trend_blocked[(symbol, sd)] = grid_trend_block(symbol, sd)
        cands = [c for c in cands
                 if (symbol, c["side"]) not in trend_blocked]
        rows.append({"symbol": symbol,
                     "score": (range_scan(symbol) or {}).get("score", 0),
                     "lots": len(lots), "candidates": cands,
                     "pending": {o.get("level") for o in mine}})
        eligible.add(symbol)
    others = sum(1 for o in pend if o["symbol"] not in eligible)
    side_room = None
    cap = int(g.get("max_same_side") or 0)
    if cap > 0:
        # Tran cung chieu (B): lot + lenh cho ngoai danh sach eligible tinh
        # truoc; lenh cho cua symbol eligible nam trong candidates.
        side_room = {}
        for sd in ("long", "short"):
            used = (sum(1 for p in _grid_lots(st) if p.get("side") == sd)
                    + sum(1 for o in pend if o["symbol"] not in eligible
                          and o.get("side") == sd))
            side_room[sd] = max(0, cap - used)
    chosen = set(range_grid.plan_slots(rows, g, len(_grid_lots(st)) + others,
                                       side_room=side_room))
    changed = False
    for o in pend:
        if o["symbol"] not in eligible:
            why = "symbol bị chặn / biên vỡ / rời top K"
        elif (o["symbol"], o.get("side")) in trend_blocked:
            why = "lọc xu hướng: %s" % trend_blocked[(o["symbol"],
                                                      o.get("side"))]
        elif (o["symbol"], o.get("level")) not in chosen:
            why = ("hết slot (tổng / mỗi symbol / cùng chiều) / ưu tiên "
                   "tầng gần giá hơn")
        else:
            continue
        engine.cancel_entry(o, why)
        changed = True
    have_keys = {(o["symbol"], o.get("level")) for o in _pending_entries(st)}
    todo = sorted(
        ((c["dist"], r["symbol"], c) for r in rows for c in r["candidates"]
         if (r["symbol"], c["key"]) in chosen
         and (r["symbol"], c["key"]) not in have_keys),
        key=lambda x: (x[0], x[1]))
    notional = CFG["order_margin_usdt"] * CFG["leverage"]
    for _dist, symbol, c in todo[:int(g.get("max_new_orders_per_cycle", 2))]:
        rng = st["grids"][symbol]["range"]
        sl, tp = range_grid.lot_exits(rng, c["side"], c["price"], g)
        rec, _why = engine.place_entry_limit(
            symbol, c["side"], notional, c["price"], abs(sl / c["price"] - 1),
            abs(tp / c["price"] - 1), "grid", level=c["key"])
        if rec:
            changed = True
    return changed


def enforce_daily_stop(engine, st, mark_prices, today):
    """Daily stop MTM: kich hoat 1 lan/ngay UTC, roi THU LAI dong moi lot
    con lai o cac vong sau (close() tu cooldown khi loi).

    Truoc: chi kich hoat khi chua halt (halt vi ly do khac -> lo vuot nguong
    van khong dong vi the) va chi thu dong 1 lan (lot tra None do cooldown/
    guard dang khop -> khong bao gio thu lai). Ly do halt persistent dang co
    duoc giu nguyen (khong ghi de bang 'daily stop').
    Tra ve True neu state thay doi.
    """
    if (not st.get("_risk_initialized") or st.get("day_start_equity", 0) <= 0
            or st.get("day") != today):
        # baseline chua reset sang ngay moi -> dp con la cua hom qua
        return False
    changed = False
    dp = st.get("daily_drawdown_pct", 0.0)
    if (st.get("daily_stop_day") != today
            and dp <= -float(CFG["risk"]["daily_max_loss_pct"])):
        st["daily_stop_day"] = today
        reason = f"daily stop {dp*100:.2f}%"
        if not st.get("halted"):
            st["halt_reason"] = reason
        st["halted"] = True
        for grid in st.get("grids", {}).values():
            grid["risk_halted"] = True
            grid["rebuild_pending"] = True
        changed = True
        log("HALTED %s mark_equity=%.2f day_start=%.2f (halt_reason=%s)" %
            (reason, st.get("mark_equity", 0.0), st["day_start_equity"],
             st.get("halt_reason")))
    if (st.get("daily_stop_day") == today and st.get("halted")
            and st.get("positions")):
        for pos in list(st["positions"]):
            rec = engine.close(
                pos, mark_prices.get(pos["symbol"], pos["entry"]),
                "DAILY_STOP")
            if rec:
                _record_close(st, rec)
                changed = True
        if st.get("positions") and not st.get("_daily_stop_retry_logged"):
            log("DAILY_STOP: con %d lot chua dong duoc -> thu lai moi vong"
                % len(st["positions"]))
            st["_daily_stop_retry_logged"] = True
            changed = True
    elif st.get("_daily_stop_retry_logged"):
        st.pop("_daily_stop_retry_logged", None)
        changed = True
    return changed


def protect_during_hold(engine):
    """Reconcile hold: chi dat lai SL/TP thieu (khong dong, khong don)."""
    try:
        return bool(engine.retry_protection(arm_only=True))
    except binance_safety.BinanceSafetyStop:
        raise
    except Exception as e:
        log(f"retry_protection (hold) loi: {e}")
        return False


def main():
    install_signal_handlers()
    # One process per host/IP.  This lock is held for the lifetime of main.
    lock_handle = acquire_instance_lock()
    binance_client.configure(log, state_path=CIRCUIT_P)
    try:
        binance_safety.ensure_allowed()
    except binance_safety.BinanceSafetyStop as e:
        log("START BLOCKED by Binance safety circuit: %s" % e)
        return

    global RUNTIME, SCANNER, TREND
    try:
        RUNTIME = runtime_config.RuntimeConfig(CFG, log=log)
        RUNTIME.start()
    except Exception:
        # Khong de loi config DB chan bot: chay theo config.json.
        log("RUNTIME CONFIG START ERROR (dung config.json):\n"
            + traceback.format_exc())
    SCANNER = range_scanner.ScannerRunner(
        CFG, binance_client.get_klines, log=log,
        db=RUNTIME.db if RUNTIME else None, path=SCANNER_P,
        fatal=(binance_safety.BinanceSafetyStop,))
    TREND = trend_filter.TrendFilter(
        CFG, binance_client.get_klines, log=log,
        fatal=(binance_safety.BinanceSafetyStop,))

    st = load_state()
    include_position_symbols(st)
    try:
        engine = make_engine(st)
    except binance_safety.BinanceSafetyStop as e:
        log("START STOPPED by Binance safety circuit: %s" % e)
        save_state(st)
        return
    except Exception:
        log("ENGINE START ERROR:\n" + traceback.format_exc())
        save_state(st)
        return
    log(f"Bot start MULTI-TICK (Binance). {len(SYMBOLS)} symbols, "
        f"fast={FAST_POLL}s, equity={st['equity']:.2f} MODE={MODE} "
        f"single_instance={bool(lock_handle)}")

    if MODE == "live":
        try:
            engine.start_user_stream()
        except binance_safety.BinanceSafetyStop as e:
            log("USER WS START STOPPED by Binance safety circuit: %s" % e)
            save_state(st)
            return
        except Exception:
            log("USER WS START ERROR:\n" + traceback.format_exc())
            save_state(st)
            return

    ws = BinanceWS(SYMBOLS, log)
    ws.start()

    candles = {}
    log("Warmup: fetching candles with shared session/rate limiter...")
    for symbol in SYMBOLS:
        try:
            candles[symbol] = {
                "5m": binance_client.get_klines(symbol, "5m", 100),
                "15m": binance_client.get_klines(symbol, "15m", 100),
                "ts": time.time(),
            }
        except binance_safety.BinanceSafetyStop as e:
            log("WARMUP STOPPED by Binance safety circuit: %s" % e)
            ws.stop()
            getattr(engine, "stop_user_stream", lambda: None)()
            save_state(st)
            return
        except Exception as e:
            log(f"warmup {symbol} failed: {e}")
        if shutdown_requested():
            break
        # Stagger symbols so startup does not create a request burst.
        time.sleep(WARMUP_DELAY)
    log(f"Warmup done: {len(candles)}/{len(SYMBOLS)}")

    prices = {}
    mark_prices = {}
    last_rest_px = 0
    last_equity_refresh = 0
    last_heartbeat = 0
    last_cfg_reload = 0
    loop = 0
    while True:
        try:
            if os.path.exists(STOP_P) or shutdown_requested():
                log("STOP file -> shutdown" if os.path.exists(STOP_P)
                    else "signal -> shutdown (da luu state)")
                ws.stop()
                getattr(engine, "stop_user_stream", lambda: None)()
                save_state(st)
                binance_client.close()
                return
            if ws.fatal_error:
                raise binance_safety.BinanceSafetyStop(ws.fatal_error)
            user_error = getattr(engine, "user_stream_error", lambda: None)()
            if user_error:
                raise binance_safety.BinanceSafetyStop(user_error)
            binance_safety.ensure_allowed()

            # ---- prices: routed WS live, low-weight REST fallback ----
            now = time.time()
            if now - last_cfg_reload > 60:
                try:
                    new = json.load(open(os.path.join(BASE, "config.json")))
                    if RUNTIME is not None:
                        # giu override tu DB (dashboard) khi file doi
                        RUNTIME.reload_file(new)
                    else:
                        CFG.clear()
                        CFG.update(new)
                except Exception as e:
                    log(f"cfg reload failed: {e}")
                last_cfg_reload = now
            if RUNTIME is not None:
                RUNTIME.poll()           # version moi tu dashboard (~10s)
                # Dashboard doc state.json: biet bot dang o version nao / DB
                # loi gi ke ca khi bot khong ket noi duoc DB.
                st["runtime_config"] = dict(RUNTIME.status(), ts=now)
            if ws.healthy():
                prices = ws.snapshot()
                marks = ws.mark_snapshot()
                mark_prices = {
                    symbol: marks.get(symbol, price)
                    for symbol, price in prices.items()
                }
            elif now - last_rest_px > 30:
                # /fapi/v2/ticker/price without symbol has weight 2, versus
                # weight 40 for the all-symbol 24h statistics endpoint.
                prices = rest_tickers_fallback()
                mark_prices = dict(prices)
                last_rest_px = now
            if not prices:
                time.sleep(2)
                continue

            dirty = False

            # Mark-to-market equity is the risk source of truth. In live mode
            # this reads Binance account margin balance; dry-run estimates it
            # from local positions and the mark price feed.
            risk_equity_ready = False
            if (DATA_ONLY or
                    now - last_equity_refresh >= float(
                        CFG.get("equity_refresh_seconds", 5))):
                try:
                    if not DATA_ONLY:
                        mtm = engine.mark_to_market_equity(mark_prices)
                        st["mark_equity"] = float(mtm)
                        st["unrealized_pnl"] = float(
                            engine.unrealized(mark_prices)
                        )
                        risk_equity_ready = True
                    last_equity_refresh = now
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    last_equity_refresh = now
                    st["halted"] = True
                    st["halt_reason"] = "equity unavailable"
                    log("CRITICAL mark-to-market equity unavailable: %s" % e)
                    dirty = True
            if DATA_ONLY:
                st["mark_equity"] = st.get("equity", 0.0)
                st["unrealized_pnl"] = 0.0
                risk_equity_ready = True

            today = utc_day()
            if risk_equity_ready and (
                    st.get("day") != today or not st.get("_risk_initialized")):
                persistent_halt = str(st.get("halt_reason", "")).startswith(
                    ("exchange ", "unmanaged ", "position ", "open order ",
                     "ambiguous ", "order fill ", "partial ", "close ",
                     "state persistence ")
                )
                st["day"] = today
                st["day_start_equity"] = st["mark_equity"]
                st["intraday_peak_equity"] = st["mark_equity"]
                st["_risk_initialized"] = True
                st["halted"] = persistent_halt
                if not persistent_halt:
                    st["halt_reason"] = ""
                    for symbol, grid in st.get("grids", {}).items():
                        if not any(p.get("tag") == "grid"
                                   and p.get("symbol") == symbol
                                   for p in st["positions"]):
                            grid["risk_halted"] = False
                            grid["rebuild_pending"] = False
                log(f"New risk day {today} baseline={st['day_start_equity']:.2f}")

            if st.get("_risk_initialized"):
                if st["mark_equity"] > st.get("intraday_peak_equity", 0):
                    st["intraday_peak_equity"] = st["mark_equity"]
                if st["day_start_equity"] > 0:
                    st["daily_drawdown_pct"] = (
                        st["mark_equity"] - st["day_start_equity"]
                    ) / st["day_start_equity"]


            if MODE == "live":
                # Vong doi SL/TP tren san (ALGO_UPDATE + query theo algoId):
                # guard khop -> ghi PnL that tu fill + huy guard con lai;
                # guard bi huy/het han/tu choi -> danh dau dat lai.
                try:
                    for rec in engine.sync_exchange_protection():
                        _record_close(st, rec)
                        dirty = True
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    log(f"sync_exchange_protection loi: {e}")
                # Phat hien vi the bi TP/SL tren san dong ma bot chua biet
                # (VD khi dang halt hoac miss WS): ghi trade uoc tinh truoc
                # khi reconcile, de mismatch tu het thay vi treo halt.
                try:
                    for rec in engine.detect_exchange_closed(mark_prices):
                        _record_close(st, rec)
                        dirty = True
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    log(f"detect_exchange_closed loi: {e}")
            if MODE == "live":
                # Halt do doi chieu luc khoi dong (lenh treo / algo lech) duoc
                # kiem tra lai moi 60s -> tu unhalt khi san da sach.
                try:
                    if engine.resolve_ambiguous_orders():
                        dirty = True
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    log(f"resolve_ambiguous_orders loi: {e}")
                try:
                    if engine.recheck_startup_holds():
                        dirty = True
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    log(f"recheck_startup_holds loi: {e}")
            if os.path.exists(CLEAR_HALT_P):
                try:
                    if handle_clear_halt_request(engine, st):
                        dirty = True
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception as e:
                    log(f"CLEAR_HALT loi: {e}")
            if MODE == "live" and not engine.reconcile_positions():
                if not st["halted"]:
                    st["halted"] = True
                    st["halt_reason"] = "position reconciliation unavailable"
                dirty = True

            # ---- lenh entry LIMIT (G5): cap nhat khop / het han MOI vong,
            # ke ca khi halt (lenh da khop phai thanh lot co SL/TP ngay).
            if not DATA_ONLY and hasattr(engine, "sync_entry_orders"):
                try:
                    if engine.sync_entry_orders(prices):
                        dirty = True
                except binance_safety.BinanceSafetyStop:
                    raise
                except Exception:
                    log("sync_entry_orders loi:\n" + traceback.format_exc())

            # ---- daily stop before every entry path
            # The mark-to-market loss guard must run before FAST and SLOW
            # strategy evaluation. Otherwise an iteration can open a new
            # position and only then discover that the account crossed the
            # daily loss limit.
            if not DATA_ONLY and enforce_daily_stop(engine, st, mark_prices,
                                                    today):
                dirty = True

            # ---- FAST PATH: only trading modes may mutate positions/orders
            reconcile_hold = str(st.get("halt_reason", "")).startswith(
                ("exchange position", "exchange protection reconciliation",
                 "unmanaged ", "position ")
            )
            if not DATA_ONLY:
                # Kiem soat rui ro (basket, tran lo tong grid, SL/TP local)
                # LUON chay, ke ca khi halt/reconcile hold: halt chi chan mo
                # lenh moi, khong duoc tat co che cat lo.
                if run_risk_controls(engine, st, mark_prices, prices,
                                     reconcile_hold):
                    dirty = True
                if not reconcile_hold:
                    # Retry dat protection cho vi the chua co SL/TP tren san
                    try:
                        if engine.retry_protection():
                            dirty = True
                    except binance_safety.BinanceSafetyStop:
                        raise
                    except Exception as e:
                        log(f"retry_protection loi: {e}")
                    # Lot bi dong vi khong dat duoc SL -> van ghi JSONL/DB
                    for rec in getattr(engine, "drain_close_records",
                                       lambda: [])():
                        _record_close(st, rec)
                        dirty = True
                    # Quet don lenh mo coi (weight 40/lan, mac dinh 60s)
                    try:
                        now_ts = time.time()
                        last_cleanup = st.get("_last_orphan_cleanup", 0)
                        if now_ts - last_cleanup >= float(
                                CFG.get("orphan_cleanup_seconds", 60)):
                            cleaned = engine.cleanup_orphan_orders()
                            st["_last_orphan_cleanup"] = now_ts
                            if cleaned:
                                log(f"CLEANUP: da xoa {cleaned} lenh mo coi")
                                dirty = True
                    except Exception as e:
                        log(f"orphan cleanup loi: {e}")
                # Kiem tra PAUSE file - neu co thi khong mo lenh moi
                paused = os.path.exists(os.path.join(BASE, "PAUSE"))
                if st.get("entry_orders") and (
                        st["halted"] or paused or not limit_mode()):
                    # Halt / PAUSE / tat che do LIMIT -> khong de lenh cho
                    # khop them (huy la giam rui ro, luon an toan).
                    try:
                        cancel_all_entries(
                            engine, st, "halt" if st["halted"] else
                            ("pause" if paused else "entry_mode doi"))
                        dirty = True
                    except binance_safety.BinanceSafetyStop:
                        raise
                    except Exception:
                        log("cancel entries loi:\n" + traceback.format_exc())
                if not st["halted"] and not paused:
                    use_range = grid_engine() == "range"
                    allowed = range_allowed_symbols() if use_range else None
                    for symbol in SYMBOLS:
                        px = prices.get(symbol)
                        cc = candles.get(symbol)
                        if px is None or not cc:
                            continue
                        if is_disabled(symbol) or symbol in MANAGE_ONLY:
                            continue
                        if use_range:
                            # Range grid: loc bang scanner (khong theo
                            # regime 15m).
                            if manage_range_grid(engine, st, symbol, px,
                                                 allowed):
                                dirty = True
                            continue
                        regime = st["regimes"].get(symbol, {}).get("regime", "ranging")
                        if regime == "ranging":
                            if manage_grid(engine, st, symbol, px):
                                dirty = True
                    if use_range and limit_mode():
                        try:
                            if manage_range_limits(engine, st, prices,
                                                   allowed):
                                dirty = True
                        except binance_safety.BinanceSafetyStop:
                            raise
                        except Exception:
                            log("manage_range_limits loi:\n"
                                + traceback.format_exc())

            # ---- SLOW PATH: candles + regime + optional scalp entries ----
            if loop % SLOW_EVERY == 0:
                due = [s for s in SYMBOLS
                       if now - candles.get(s, {}).get("ts", 0)
                       > CFG["candle_refresh_seconds"]]
                for symbol in due[:CANDLE_PER_SLOW]:
                    try:
                        candles[symbol] = {
                            "5m": binance_client.get_klines(symbol, "5m", 100),
                            "15m": binance_client.get_klines(symbol, "15m", 100),
                            "ts": time.time(),
                        }
                        time.sleep(0.5)  # them gian request giua cac symbol
                    except binance_safety.BinanceSafetyStop:
                        raise
                    except Exception as e:
                        log(f"candle refresh {symbol} failed: {e}")
                # Scanner di ngang: chay o moi mode (ke ca data_only).
                if SCANNER is not None:
                    try:
                        SCANNER.tick(SYMBOLS, candles)
                    except binance_safety.BinanceSafetyStop:
                        raise
                    except Exception:
                        log("SCANNER loi:\n" + traceback.format_exc())
                # Loc chieu xu huong grid (BTC + tung symbol), moi mode.
                if TREND is not None:
                    try:
                        TREND.tick(SYMBOLS, prices)
                        st["trend"] = TREND.snapshot()
                    except binance_safety.BinanceSafetyStop:
                        raise
                    except Exception:
                        log("TREND loi:\n" + traceback.format_exc())
                if not st["halted"]:
                    for symbol in SYMBOLS:
                        px = prices.get(symbol)
                        cc = candles.get(symbol)
                        if px is None or not cc or not cc.get("15m"):
                            continue
                        rec = st["regimes"].get(symbol, {})
                        regime = rec.get("regime", "ranging")
                        closed15 = cc["15m"][:-1] if len(cc["15m"]) > 1 else cc["15m"]
                        regime_bar_ts = closed15[-1].get("ts") if closed15 else None
                        if regime_bar_ts != rec.get("bar_ts"):
                            previous = rec.get("regime")
                            candidate, av = strategy.detect_regime(
                                cc["15m"],
                                CFG["adx_threshold"],
                                previous=previous,
                                range_threshold=CFG.get("adx_range_threshold"),
                            )
                            candidate_name = rec.get("candidate")
                            candidate_count = int(rec.get("candidate_count", 0))
                            if previous is None:
                                regime = candidate
                                candidate_name = None
                                candidate_count = 0
                            elif candidate == previous:
                                regime = previous
                                candidate_name = None
                                candidate_count = 0
                            elif candidate == candidate_name:
                                candidate_count += 1
                                if candidate_count >= int(CFG.get(
                                        "regime_confirm_bars", 2)):
                                    regime = candidate
                                    candidate_name = None
                                    candidate_count = 0
                            else:
                                candidate_name = candidate
                                candidate_count = 1
                            rec = {
                                "regime": regime,
                                "adx": av,
                                "bar_ts": regime_bar_ts,
                                "candidate": candidate_name,
                                "candidate_count": candidate_count,
                            }
                            st["regimes"][symbol] = rec
                            if regime != previous and previous is not None:
                                log(f"{symbol} regime -> {regime} (adx={av})")
                        # ATR-adaptive grid step cho lan rebuild ke tiep
                        try:
                            a = atr(cc["15m"], 14)
                            apct = a / px if a and px else None
                        except Exception:
                            apct = None
                        g = CFG["grid"]
                        if apct:
                            astep = round(min(g["step_max"],
                                              max(g["step_min"],
                                                  g["step_mult"] * apct)), 6)
                        else:
                            astep = g["step_pct"]
                        st["grids"].setdefault(
                            symbol, {"anchor": None, "taken": {}})["step"] = astep
                        if (not DATA_ONLY and regime == "trending"
                                and not is_disabled(symbol)
                                and symbol not in MANAGE_ONLY):
                            if manage_scalp(engine, st, symbol,
                                            px, cc["5m"], cc["15m"]):
                                dirty = True

            if dirty or loop % 20 == 0:
                save_state(st)
            loop += 1
            if now - last_heartbeat > 600:
                log(f"heartbeat wallet={st['equity']:.2f} "
                    f"mark_equity={st.get('mark_equity', st['equity']):.2f} "
                    f"unreal={st.get('unrealized_pnl', 0.0):+.2f} "
                    f"daily_dd={st.get('daily_drawdown_pct', 0.0)*100:+.2f}% "
                    f"pos={len(st['positions'])} ws={ws.healthy()} mode={MODE}")
                last_heartbeat = now
        except binance_safety.BinanceSafetyStop as e:
            # Critical rule: 429/418/-1003 and a persisted circuit stop all
            # REST/private trading; do not let the 0.5s loop retry it.
            log("SAFETY STOP: %s" % e)
            ws.stop()
            getattr(engine, "stop_user_stream", lambda: None)()
            save_state(st)
            binance_client.close()
            return
        except Exception:
            log("LOOP ERROR:\n" + traceback.format_exc())
        time.sleep(FAST_POLL)


if __name__ == "__main__":
    main()
