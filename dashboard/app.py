"""Dashboard Crypto Bots — LIVE (tien that), PAPER (chay thu), cau hinh bot,
scanner, quan tri tai khoan. Bat buoc dang nhap (bang dashboard_users).

Chay:  DATABASE_URL=postgres://... streamlit run app.py
Tu refresh 60s. Vi the dang mo doc truc tiep tu state file cua bot.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import psycopg
import psycopg.rows
import requests
import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "db"))
import bot_config as bc  # noqa: E402  (config runtime + tai khoan)

try:  # logic thuan dung chung voi bot (so tang range grid moi symbol)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "binance-bot"))
    import range_grid  # noqa: E402
except Exception:  # pragma: no cover - dashboard van chay neu thieu
    range_grid = None

st.set_page_config(page_title="Crypto Bots Dashboard", layout="wide")

# ---------------- cau hinh ----------------
# Goc repo (dashboard/..): moi duong dan mac dinh tinh tu day, khong viet cung
# /home/ubuntu/muse_bot hay ~/workspace (duong dan cu chi con la phuong an cuoi).
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEME_DIR = os.path.join(REPO_DIR, "meme-radar")
DB = os.environ.get("DATABASE_URL")
BINANCE_STATE = os.environ.get("BINANCE_STATE") or os.path.join(
    REPO_DIR, "binance-bot", "state.json")
BINANCE_DIR = os.environ.get("BINANCE_DIR", os.path.dirname(BINANCE_STATE))
# File .env chua API key Binance (chi doc, khong commit secret vao code)
BINANCE_ENV_P = os.environ.get("BINANCE_ENV_P") or os.path.join(REPO_DIR,
                                                                ".env")


def _clear_halt_result(max_age=3600):
    """Ket qua yeu cau 'Xoa halt' bot ghi lai (an sau max_age giay)."""
    try:
        with open(os.path.join(BINANCE_DIR, "clear_halt_result.json")) as f:
            res = json.load(f)
        ts = float(res.get("ts") or 0)
        if time.time() - ts > max_age:
            return None
        res["when"] = datetime.fromtimestamp(ts, TZINFO).strftime("%H:%M:%S")
        return res
    except Exception:
        return None


def first_existing(*paths):
    """Duong dan dau tien ton tai; khong co thi tra ung vien dau (de hien thi)."""
    paths = [p for p in paths if p]
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0] if paths else ""


def resolve_helius_key(env=None, repo=None):
    """-> (key, nguon). Cung thu tu voi radar.py: env HELIUS_API_KEY -> file
    env HELIUS_KEY_FILE -> <repo>/meme-radar/.helius_key -> <repo>/.helius_key
    -> ~/workspace/meme-radar/.helius_key. KHONG bao gio log/in key."""
    env = os.environ if env is None else env
    repo = repo or REPO_DIR
    key = (env.get("HELIUS_API_KEY") or "").strip()
    if key:
        return key, "env HELIUS_API_KEY"
    for path in (env.get("HELIUS_KEY_FILE"),
                 os.path.join(repo, "meme-radar", ".helius_key"),
                 os.path.join(repo, ".helius_key"),
                 os.path.expanduser("~/workspace/meme-radar/.helius_key")):
        if not path:
            continue
        try:
            with open(path) as f:
                key = f.read().strip()
        except (IOError, OSError):
            continue
        if key:
            return key, path
    return "", ""


OKX_STATE = os.environ.get("OKX_STATE") or first_existing(
    os.path.join(REPO_DIR, "trading-bot", "state.json"),
    os.path.expanduser("~/workspace/trading-bot/state.json"))
RADAR_STATE = os.environ.get("RADAR_STATE") or first_existing(
    os.path.join(REPO_DIR, "meme-radar", "radar_state.json"),
    os.path.expanduser("~/workspace/meme-radar/radar_state.json"))
LIVE_CFG_P = os.environ.get("LIVE_CFG_P") or os.path.join(
    MEME_DIR, "config.live.json")
# Vi live cua meme-radar/live_trader.py (khop DEFAULTS.wallet_address).
DEFAULT_SOL_WALLET = "DxYkrsJA6YdS1cqJ9ocPCYRBacd7Xan3DeYWZva89dLd"


def resolve_sol_wallet(env=None, cfg_path=None):
    """Vi hien thi tren dashboard = vi live_trader dang giao dich.

    Thu tu: env SOL_WALLET -> wallet_address trong config.live.json cua
    live_trader -> vi live mac dinh. Chi doc pubkey, khong bao gio doc key.
    """
    env = os.environ if env is None else env
    w = (env.get("SOL_WALLET") or "").strip()
    if w:
        return w
    try:
        with open(cfg_path or LIVE_CFG_P) as f:
            w = (json.load(f).get("wallet_address") or "").strip()
        if w:
            return w
    except Exception:
        pass
    return DEFAULT_SOL_WALLET


SOL_WALLET = resolve_sol_wallet()
LIVE_POS_P = os.environ.get("LIVE_POS_P") or os.path.join(
    MEME_DIR, "live_positions.json")
LIVE_TRADES_P = os.environ.get("LIVE_TRADES_P") or os.path.join(
    MEME_DIR, "live_trades.jsonl")
SOL_MINT = "So11111111111111111111111111111111111111112"

TZ = "Asia/Ho_Chi_Minh"
TZINFO = timezone(timedelta(hours=7))

try:
    frag = st.fragment(run_every=60)
    frag15 = st.fragment(run_every=15)
    frag5 = st.fragment(run_every=5)
except Exception:
    frag = lambda f: f  # noqa: E731
    frag15 = lambda f: f  # noqa: E731
    frag5 = lambda f: f  # noqa: E731

# ---------------- CSS (an toan cho ca light & dark theme) ----------------
st.markdown("""
<style>
.kpi-card{background:rgba(127,127,127,.09);border-radius:10px;
  padding:12px 14px;border-left:4px solid #64748b;height:100%;}
.kpi-label{font-size:11px;letter-spacing:.5px;text-transform:uppercase;
  opacity:.62;margin-bottom:2px;}
.kpi-value{font-size:25px;font-weight:700;line-height:1.15;}
.kpi-sub{font-size:12px;opacity:.6;margin-top:2px;}
.pos{color:#16a34a;}.neg{color:#dc2626;}
.sec{font-size:19px;font-weight:700;margin:22px 0 8px 0;
  padding-bottom:6px;border-bottom:2px solid rgba(127,127,127,.28);}
.sub2{font-size:15px;font-weight:600;margin:14px 0 6px 0;opacity:.92;}
.addr{font-family:monospace;font-size:13px;word-break:break-all;
  padding-top:4px;}
div[data-testid="stTabs"] button{font-size:15px;font-weight:600;}
.small-note{font-size:12px;opacity:.6;}
</style>
""", unsafe_allow_html=True)


# ---------------- data helpers ----------------
@st.cache_resource
def get_conn():
    if not DB:
        return None
    return psycopg.connect(DB, row_factory=psycopg.rows.dict_row,
                           autocommit=True)


def q(sql, params=()):
    for attempt in range(2):
        con = get_conn()
        if con is None:
            return []
        try:
            with con.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        except Exception as e:
            try:
                con.close()
            except Exception:
                pass
            get_conn.clear()
            if attempt == 1:
                st.error(f"Loi query: {e}")
                return []


def day_filter(col="closed_at"):
    return f"date_trunc('day', {col} AT TIME ZONE '{TZ}')"


def metrics(rows):
    """rows: list dict co 'net'. Tra ve (pnl, winrate, pf, expectancy, n)."""
    n = len(rows)
    if not n:
        return 0.0, 0.0, 0.0, 0.0, 0
    nets = [r["net"] for r in rows]
    pnl = sum(nets)
    wins = sum(1 for x in nets if x > 0)
    gw = sum(x for x in nets if x > 0)
    gl = -sum(x for x in nets if x < 0)
    return (pnl, wins / n, gw / gl if gl else 99.0, pnl / n, n)


def daily_df(rows):
    if not rows:
        return pd.DataFrame(columns=["day", "net"])
    df = pd.DataFrame(rows)
    df["day"] = pd.to_datetime(df["day"]).dt.date
    g = df.groupby("day", as_index=False)["net"].sum().sort_values("day")
    g["cum"] = g["net"].cumsum()
    return g


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def fmt_ts(dt):
    if not dt:
        return "chua co"
    try:
        return dt.astimezone(TZINFO).strftime("%d/%m %H:%M")
    except Exception:
        return "?"


@st.cache_data(ttl=180)
def sol_balance():
    """So du SOL cua vi bot; None neu khong doc duoc. Khong bao gio in key."""
    key, _src = resolve_helius_key()
    if not key:
        return None
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={key}",
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                  "params": [SOL_WALLET]},
            timeout=15)
        return r.json()["result"]["value"] / 1e9
    except Exception:
        return None


# ---------------- UI helpers ----------------
def section(title):
    st.markdown(f'<div class="sec">{title}</div>', unsafe_allow_html=True)


def kpi_cards(title, rows):
    st.markdown(f'<div class="sub2">{title}</div>', unsafe_allow_html=True)
    pnl, wr, pf, exp, n = metrics(rows)
    if not n:
        st.info("Chua co lenh dong trong khoang da chon.")
        return
    s = lambda x: "pos" if x > 0 else ("neg" if x < 0 else "")
    items = [
        ("P&L rong", f"{pnl:+.2f} U", s(pnl)),
        ("Winrate", f"{wr:.0%}", ""),
        ("Profit factor", f"{pf:.2f}", ""),
        ("Ky vong / lenh", f"{exp:+.2f} U", s(exp)),
        ("So lenh dong", f"{n}", ""),
    ]
    cols = st.columns(5)
    for col, (label, val, cls) in zip(cols, items):
        col.markdown(
            f'<div class="kpi-card"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value {cls}">{val}</div></div>',
            unsafe_allow_html=True)


def pnl_charts(df, prefix):
    if df.empty:
        st.info(f"{prefix}: chua co du lieu trong khoang da chon.")
        return
    c1, c2 = st.columns(2)
    with c1:
        fig = px.line(df, x="day", y="cum",
                      title=f"{prefix} — P&L cong don (U)")
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, width="stretch")
    with c2:
        d2 = df.copy()
        d2["mau"] = d2["net"].apply(lambda x: "lai" if x >= 0 else "lo")
        fig = px.bar(d2, x="day", y="net", color="mau",
                     color_discrete_map={"lai": "#16a34a", "lo": "#dc2626"},
                     title=f"{prefix} — P&L tung ngay (U)")
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)", showlegend=False)
        st.plotly_chart(fig, width="stretch")


def trades_table(rows, cols_map, title="Lenh dong gan nhat"):
    st.markdown(f'<div class="sub2">{title}</div>', unsafe_allow_html=True)
    if not rows:
        st.info("Chua co du lieu.")
        return
    df = pd.DataFrame(rows)
    if "closed_at" in df.columns:
        df["closed_at"] = pd.to_datetime(df["closed_at"])\
            .dt.tz_convert(TZ).dt.strftime("%m-%d %H:%M")
    st.dataframe(df.rename(columns=cols_map)[list(cols_map.values())],
                 width="stretch")


def last_updates_line():
    parts = []
    for label, tbl in (("Binance", "binance_trades"), ("OKX", "okx_trades"),
                       ("Radar", "radar_trades")):
        r = q(f"SELECT MAX(closed_at) AS m FROM {tbl}")
        m = r[0]["m"] if r and r[0].get("m") else None
        parts.append(f"{label}: {fmt_ts(m)}")
    for label, path in (("State Binance", BINANCE_STATE),
                        ("State OKX", OKX_STATE),
                        ("State radar", RADAR_STATE)):
        try:
            m = datetime.fromtimestamp(os.path.getmtime(path), tz=TZINFO)
            parts.append(f"{label}: {m.strftime('%H:%M')}")
        except OSError:
            parts.append(f"{label}: ?")
    return " · ".join(parts)


# ---------------- tab LIVE ----------------
@st.cache_data(ttl=5)
def binance_all_prices():
    """Gia live tat ca symbols Binance Futures: 1 request duy nhat."""
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price",
                         timeout=8)
        data = r.json()
        return {x["symbol"]: float(x["price"]) for x in data
                if isinstance(x, dict) and x.get("symbol")}
    except Exception:
        return {}


@st.cache_data(ttl=5)
def binance_exchange_positions():
    """Vi the Futures TRUC TIEP tu san Binance (read-only).

    Chi goi cac API doc (fetch_positions, getOpenAlgoOrders) — khong bao gio
    dat/huy lenh. Cache 5 giay. Raise Exception neu khong lay duoc de caller
    fallback ve state.json.
    Tra ve (positions, guards): positions la list dict
    {symbol, side, qty, entry, mark, upnl}, guards la dict
    {symbol: {"tp": float|None, "sl": float|None}} tu algo orders.
    """
    try:
        import ccxt
    except ImportError as e:
        raise RuntimeError("thieu thu vien ccxt: %s" % e)

    key = secret = None
    try:
        with open(BINANCE_ENV_P) as f:
            for line in f:
                line = line.strip()
                if line.startswith("BINANCE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
                elif line.startswith("BINANCE_API_SECRET="):
                    secret = line.split("=", 1)[1].strip().strip("\"'")
    except OSError as e:
        raise RuntimeError("khong doc duoc %s: %s" % (BINANCE_ENV_P, e))
    if not key or not secret:
        raise RuntimeError("thieu BINANCE_API_KEY/SECRET trong %s" % BINANCE_ENV_P)

    ex = ccxt.binanceusdm({"apiKey": key, "secret": secret,
                           "options": {"defaultType": "future"}})
    # Chi doc — khong dat lenh
    raw = ex.fetch_positions()
    positions = []
    for p in raw:
        try:
            qty = float(p.get("contracts") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            continue
        entry = float(p.get("entryPrice") or 0)
        mark = float(p.get("markPrice") or p.get("lastPrice") or 0)
        upnl = p.get("unrealizedPnl")
        upnl = float(upnl) if upnl is not None else 0.0
        side = (p.get("side") or "").lower()
        sym = (p.get("symbol") or "").replace("/", "").replace(":USDT", "")
        positions.append({"symbol": sym, "side": side, "qty": abs(qty),
                          "entry": entry, "mark": mark, "upnl": upnl})

    # TP/SL tu algo orders mo (CONDITIONAL: TAKE_PROFIT_MARKET / STOP_MARKET)
    guards = {}
    try:
        algos = ex.fapiPrivateGetOpenAlgoOrders()
        if isinstance(algos, dict):
            algos = algos.get("orders") or algos.get("data") or []
        for o in algos or []:
            if not isinstance(o, dict):
                continue
            sym = str(o.get("symbol") or "")
            otype = str(o.get("orderType") or o.get("type") or o.get("algoType") or "")
            try:
                trig = float(o.get("triggerPrice") or 0)
            except (TypeError, ValueError):
                trig = 0.0
            if not sym or trig <= 0:
                continue
            g = guards.setdefault(sym, {"tp": None, "sl": None})
            if "TAKE_PROFIT" in otype and g["tp"] is None:
                g["tp"] = trig
            elif "STOP" in otype and "TAKE_PROFIT" not in otype and g["sl"] is None:
                g["sl"] = trig
    except Exception:
        pass  # khong co algo orders thi bo qua, vi the van hien
    return positions, guards


@frag5
def binance_live_positions_block():
    """Bang vi the Binance mo — doc TRUC TIEP tu san, refresh 5s.

    Neu API loi thi fallback ve state.json va hien canh bao du lieu co the cu.
    """
    st.markdown('<div class="sub2">Vi the dang mo (truc tiep tu san, live 5s)</div>',
                unsafe_allow_html=True)
    rows, tot, source_note = [], 0.0, None
    try:
        ex_pos, guards = binance_exchange_positions()
        for p in ex_pos:
            g = guards.get(p["symbol"], {})
            tot += p["upnl"]
            rows.append({
                "Symbol": p["symbol"], "Chieu": p["side"],
                "Qty": p["qty"], "Entry": p["entry"],
                "Gia live": round(p["mark"], 6) if p["mark"] else None,
                "Lãi/lỗ live (U)": round(p["upnl"], 2),
                "SL": g.get("sl"), "TP": g.get("tp"),
            })
    except Exception as e:
        # Fallback: doc tu state.json cua bot, canh bao du lieu co the cu
        source_note = ("⚠️ Không lấy được vị thế từ sàn (%s) — "
                       "dữ liệu có thể cũ (đọc từ state.json)." % e)
        b_st = load_state(BINANCE_STATE)
        if not b_st:
            st.warning("Khong doc duoc file trang thai bot Binance.")
            return
        marks = binance_all_prices()
        for p in b_st.get("positions", []):
            try:
                e0 = float(p.get("entry", 0) or 0)
                n = float(p.get("notional", 0) or 0)
                mk = marks.get((p.get("symbol") or "").upper(), 0) or 0
                sgn = 1 if (p.get("side") or "").lower() == "long" else -1
                upnl = (mk - e0) / e0 * n * sgn if e0 > 0 and n > 0 and mk > 0 else 0.0
            except Exception:
                upnl, mk = 0.0, 0.0
            tot += upnl
            rows.append({
                "ID": p.get("id"), "Symbol": p.get("symbol"),
                "Chieu": p.get("side"), "Loai": p.get("tag"),
                "Entry": p.get("entry"),
                "Gia live": round(mk, 6) if mk else None,
                "Lãi/lỗ live (U)": round(upnl, 2),
                "SL": p.get("sl"), "TP": p.get("tp"),
                "Notional": p.get("notional"),
            })
    if source_note:
        st.warning(source_note)
    if not rows:
        st.caption("0 vi the dang mo (theo san).")
        return
    df = pd.DataFrame(rows)

    def _color(v):
        try:
            return "color:#16a34a" if float(v) >= 0 else "color:#dc2626"
        except Exception:
            return ""
    st.dataframe(df.style.map(_color, subset=["Lãi/lỗ live (U)"]),
                 width="stretch")
    cls = "pos" if tot >= 0 else "neg"
    st.markdown(f'Tong unrealized: <span class="{cls}"><b>{tot:+.2f} U</b>'
                f'</span> · gia cap nhat 5s/lan', unsafe_allow_html=True)


@frag15
def binance_equity_realtime():
    st.markdown('<div class="sub2">⚡ Equity realtime — Binance LIVE</div>',
                unsafe_allow_html=True)
    rows = q("""SELECT ts AT TIME ZONE 'Asia/Ho_Chi_Minh' AS ts, equity
                FROM equity_snapshots WHERE system='binance'
                  AND ts >= now() - interval '6 hours'
                ORDER BY ts""")
    if not rows:
        st.info("Chua co snapshot — doi service muse-binance-equity-snap.")
        return
    eq = float(rows[-1]["equity"])
    cls = "pos" if eq >= 1000 else "neg"
    c1, c2 = st.columns([1, 3])
    with c1:
        st.markdown(f'<div class="kpi-card" style="border-left-color:'
                    f'{"#16a34a" if eq >= 1000 else "#dc2626"}">'
                    f'<div class="kpi-label">Equity Binance (U)</div>'
                    f'<div class="kpi-value {cls}">{eq:.2f} U</div>'
                    f'<div class="kpi-sub">{eq - 1000:+.2f} vs von 1000 · live</div></div>',
                    unsafe_allow_html=True)
    with c2:
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["ts"])
        fig = px.line(df, x="ts", y="equity",
                      title="Equity 6h qua (snapshot 15s)")
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)",
                          xaxis_title=None, yaxis_title="U")
        st.plotly_chart(fig, width="stretch")
    st.caption("Tu refresh 15s · totalMarginBalance thuc tren san.")


@frag
def live_kpi_frag(where, params):
    d = day_filter()

    def tag_where(tag):
        base = where.strip()
        cond = f"tag = '{tag}'"
        if not base:
            return f"WHERE {cond}", params
        return f"{base} AND {cond}", params

    for tag, title in (("grid", "🔲 Grid"), ("scalp", "⚡ Scalp")):
        w, p = tag_where(tag)
        rows = q(f"SELECT {d} AS day, pnl AS net, tag, reason, live, dry "
                 f"FROM binance_trades {w} ORDER BY closed_at", p)
        kpi_cards(f"Hieu suat {title}", rows)
        n = len(rows)
        n_live = sum(1 for r in rows if r.get("live") and not r.get("dry"))
        n_dry = sum(1 for r in rows if r.get("dry"))
        if n:
            st.caption(f"Trong do: {n_live} lenh LIVE tien that, "
                       f"{n_dry} lenh dry-run.")
        pnl_charts(daily_df(rows), f"Binance LIVE {title}")
        st.divider()

    rbn = q("""SELECT closed_at, symbol, side, tag, pnl AS net, reason,
                      live, dry
               FROM binance_trades ORDER BY closed_at DESC LIMIT 50""")
    if rbn:
        dfb = pd.DataFrame(rbn)
        dfb["che_do"] = dfb.apply(
            lambda r: "LIVE" if (r["live"] and not r["dry"]) else "dry-run",
            axis=1)
        trades_table(dfb.to_dict("records"),
                     {"closed_at": "Dong luc", "symbol": "Symbol",
                      "side": "Chieu", "tag": "Loai", "net": "P&L rong (U)",
                      "reason": "Ly do", "che_do": "Che do"},
                     "50 lenh Binance gan nhat")
    else:
        st.info("Binance LIVE chua co lenh dong.")


def tab_live_binance(where, params):
    section("📈 Binance Futures — LIVE (tien that)")
    binance_equity_realtime()
    binance_live_positions_block()
    live_kpi_frag(where, params)


def load_live_trades():
    """Doc live_trades.jsonl, tra ve list dict (moi nhat cuoi)."""
    rows = []
    try:
        with open(LIVE_TRADES_P) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return rows


def live_trade_kpi_rows(trades):
    """Chuyen live trades thanh rows {'day','net'} cho kpi_cards/daily_df."""
    rows = []
    for t in trades:
        try:
            ca = t.get("closed_at")
            if not ca:
                continue
            day = datetime.fromtimestamp(ca, tz=TZINFO).strftime("%Y-%m-%d")
            rows.append({"day": day, "net": float(t.get("realized_usd") or 0)})
        except Exception:
            continue
    return rows


def live_trade_fee(t):
    """(phi_mang_usd, truoc_phi_usd) cua 1 lenh live, hoac (None, None) neu
    bot chua ghi du phi (lenh cu / co leg uoc tinh). realized_usd DA tru phi
    mang (bot tinh tu so du SOL); app vi thuong hien so truoc phi."""
    if not t.get("fee_known") or t.get("fee_usd") is None:
        return None, None
    try:
        fee = float(t["fee_usd"])
        return fee, float(t.get("realized_usd") or 0) + fee
    except (TypeError, ValueError):
        return None, None


def live_fee_summary(trades):
    """-> (so_lenh_co_phi, tong_phi, tong_rong_cua_lenh_do, tong_truoc_phi)."""
    n, fee, net, gross = 0, 0.0, 0.0, 0.0
    for t in trades:
        f, g = live_trade_fee(t)
        if f is None:
            continue
        n += 1
        fee += f
        net += float(t.get("realized_usd") or 0)
        gross += g
    return n, fee, net, gross


def filter_live_trades(trades, days, live_only=False):
    if live_only:
        trades = [t for t in trades if t.get("mode") == "live"]
    if not days:
        return trades
    cutoff = datetime.now(TZINFO) - timedelta(days=days)
    out = []
    for t in trades:
        try:
            ca = datetime.fromtimestamp(t.get("closed_at") or 0, tz=TZINFO)
        except Exception:
            continue
        if ca >= cutoff:
            out.append(t)
    return out


def trade_cum_df(trades):
    """P&L cong don THEO TUNG LENH (moi lenh dong = 1 diem) -> do thi nhich
    ngay khi co lenh dong, khong phai doi sang ngay moi nhu ban theo ngay."""
    rows = []
    for t in trades:
        try:
            ca = float(t.get("closed_at") or 0)
            if ca <= 0:
                continue
            rows.append({"ts": datetime.fromtimestamp(ca, tz=TZINFO),
                         "net": float(t.get("realized_usd") or 0),
                         "symbol": t.get("symbol") or "?",
                         "reason": t.get("reason") or ""})
        except (TypeError, ValueError):
            continue
    if not rows:
        return pd.DataFrame(columns=["ts", "net", "symbol", "reason", "cum"])
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    df["cum"] = df["net"].cumsum()
    return df


def snapshot_summary(rows, now=None, fresh_s=120):
    """rows [{'ts','equity'}] tang dan -> (equity_moi_nhat|None neu cu,
    thay_doi_trong_cua_so, tuoi_giay)."""
    if not rows:
        return None, None, None
    now = now or datetime.now(TZINFO)
    last = rows[-1]
    ts = last["ts"]
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=TZINFO)
    age = (now - ts).total_seconds()
    eq = float(last["equity"])
    change = eq - float(rows[0]["equity"])
    return (eq if age <= fresh_s else None), change, age


@frag15
def live_radar_equity_realtime():
    st.markdown('<div class="sub2">⚡ Equity realtime — ví Solana LIVE</div>',
                unsafe_allow_html=True)
    rows = q("""SELECT ts AT TIME ZONE 'Asia/Ho_Chi_Minh' AS ts, equity
                FROM equity_snapshots WHERE system='radar_live'
                  AND ts >= now() - interval '6 hours'
                ORDER BY ts""")
    eq, change, age = snapshot_summary(rows)
    sub = None
    if eq is not None:
        sub = (f"SOL + token đang giữ · {change:+.2f} U trong 6h · "
               f"snapshot {age:.0f}s trước")
    else:
        bal = sol_balance()
        sol_px = jupiter_marks(SOL_MINT).get(SOL_MINT)
        if bal is not None and sol_px:
            eq = bal * sol_px
            sub = (f"chỉ SOL: {bal:.4f} SOL @ ${sol_px:,.2f} "
                   f"(chưa có snapshot mới)")
    c1, c2 = st.columns([1, 3])
    with c1:
        if eq is None:
            st.warning("Không đọc được số dư SOL / giá SOL.")
        else:
            st.markdown(f'<div class="kpi-card" style="border-left-color:'
                        f'#16a34a"><div class="kpi-label">Equity ví live '
                        f'(USD)</div><div class="kpi-value pos">{eq:,.2f} U'
                        f'</div><div class="kpi-sub">{sub}</div>'
                        f'<div class="addr">{SOL_WALLET}</div></div>',
                        unsafe_allow_html=True)
    with c2:
        if rows:
            df = pd.DataFrame(rows)
            df["ts"] = pd.to_datetime(df["ts"])
            fig = px.line(df, x="ts", y="equity",
                          title="Equity ví 6h qua (snapshot 15s)")
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)",
                              xaxis_title=None, yaxis_title="U")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Chưa có snapshot equity ví — khởi động app "
                    "muse-live-equity: `git up setup --only "
                    "muse-live-equity`.")
    st.caption("Tự refresh 15s · Equity = SOL trong ví × giá SOL + token vị "
               "thế đang mở theo giá Jupiter (app muse-live-equity).")


def live_radar_pnl_charts(trades):
    """Giong tab Binance: P&L cong don + P&L tung ngay; cong don ve THEO
    LENH de cap nhat ngay khi co lenh dong."""
    cum = trade_cum_df(trades)
    if cum.empty:
        st.info("Live Radar: chua co lenh dong trong khoang da chon.")
        return
    c1, c2 = st.columns(2)
    with c1:
        fig = px.line(cum, x="ts", y="cum", markers=True,
                      hover_data={"symbol": True, "net": ":+.2f",
                                  "reason": True},
                      title="Live Radar — P&L cong don theo lenh (U)")
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)",
                          xaxis_title=None, yaxis_title="U")
        fig.add_hline(y=0, line_dash="dot", opacity=0.4)
        st.plotly_chart(fig, width="stretch")
    with c2:
        d2 = daily_df(live_trade_kpi_rows(trades))
        d2["mau"] = d2["net"].apply(lambda x: "lai" if x >= 0 else "lo")
        fig = px.bar(d2, x="day", y="net", color="mau",
                     color_discrete_map={"lai": "#16a34a", "lo": "#dc2626"},
                     title="Live Radar — P&L tung ngay (U)")
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)", showlegend=False,
                          xaxis_title=None, yaxis_title="U")
        st.plotly_chart(fig, width="stretch")


@frag
def live_radar_kpi_frag(days):
    # Chi tinh lenh tien that (mode=live), khong tron dry-run
    trades = filter_live_trades(load_live_trades(), days, live_only=True)
    kpi_cards("Hieu suat live (tien that)", live_trade_kpi_rows(trades))
    live_radar_pnl_charts(trades)
    n_fee, fee, net_f, gross_f = live_fee_summary(trades)
    if n_fee:
        st.caption(
            f"P&L rong = SOL that nhan/chi, DA tru phi mang. {n_fee}/"
            f"{len(trades)} lenh co du lieu phi: rong {net_f:+.2f} U · phi "
            f"mang {fee:.2f} U · truoc phi {gross_f:+.2f} U (app vi thuong "
            "hien so truoc phi). Doi chieu tung tx: meme-radar/"
            "reconcile_wallet.py")
    else:
        st.caption("P&L rong = SOL that nhan/chi, DA tru phi mang (app vi "
                   "thuong hien so truoc phi -> lech vai cent/lenh). Doi "
                   "chieu tung tx: meme-radar/reconcile_wallet.py")
    st.markdown('<div class="sub2">Thong ke ly do thoat lenh — live</div>',
                unsafe_allow_html=True)
    agg = {}
    for t in trades:
        r = t.get("reason") or "?"
        a = agg.setdefault(r, {"n": 0, "pnl": 0.0})
        a["n"] += 1
        try:
            a["pnl"] += float(t.get("realized_usd") or 0)
        except Exception:
            pass
    if agg:
        st.dataframe(pd.DataFrame([{
            "Ly do": k, "Lenh": v["n"], "P&L (U)": round(v["pnl"], 2),
        } for k, v in sorted(agg.items(), key=lambda x: -x[1]["pnl"])]),
            width="stretch")
    else:
        st.info("Chua co du lieu.")


@frag15
def live_radar_positions_block():
    st.markdown('<div class="sub2">Vi the LIVE dang mo (live 15s)</div>',
                unsafe_allow_html=True)
    spos = load_state(LIVE_POS_P) or []
    if not spos:
        st.info("Khong co vi the live nao dang mo.")
        return
    mints = ",".join(dict.fromkeys(
        [p.get("token") for p in spos if p.get("token")]))
    marks = jupiter_marks(mints)
    rows = []
    for p in spos:
        try:
            e = float(p.get("entry", 0) or 0)
            s = float(p.get("size_usd", 0) or 0)
            rem = float(p.get("remaining", 1) or 0)
            mk = marks.get(p.get("token", ""), 0) or 0
            upnl = (mk - e) / e * s * rem if e > 0 and mk > 0 else 0.0
        except Exception:
            upnl, mk = 0.0, 0.0
        rows.append({
            "Symbol": p.get("symbol"),
            "Entry": p.get("entry"),
            "Gia live": round(mk, 8) if mk else None,
            "Lãi/lỗ live (U)": round(upnl, 2),
            "Size $": p.get("size_usd"),
            "Con lai": f"{(p.get('remaining', 1) or 0):.0%}",
            "TP1": "✓" if p.get("tp1") else "",
            "TP2": "✓" if p.get("tp2") else "",
        })
    df = pd.DataFrame(rows)

    def _c(v):
        try:
            return "color:#16a34a" if float(v) >= 0 else "color:#dc2626"
        except Exception:
            return ""
    st.dataframe(df.style.map(_c, subset=["Lãi/lỗ live (U)"]),
                 width="stretch")
    st.caption(f"{len(spos)} vi the · gia live tu Jupiter.")


@frag
def live_radar_trades_frag(days):
    trades = filter_live_trades(load_live_trades(), days, live_only=True)
    rows = []
    for t in sorted(trades, key=lambda x: x.get("closed_at") or 0,
                    reverse=True)[:50]:
        try:
            ca = datetime.fromtimestamp(t.get("closed_at") or 0, tz=TZINFO)
        except Exception:
            ca = None
        rows.append({
            "closed_at": ca,
            "symbol": t.get("symbol"),
            "mode": t.get("mode"),
            "net": round(float(t.get("realized_usd") or 0), 2),
            "fee": (round(live_trade_fee(t)[0], 3)
                    if live_trade_fee(t)[0] is not None else None),
            "gross": (round(live_trade_fee(t)[1], 2)
                      if live_trade_fee(t)[1] is not None else None),
            "reason": t.get("reason"),
        })
    trades_table(rows,
                 {"closed_at": "Dong luc", "symbol": "Symbol",
                  "mode": "Che do", "net": "P&L rong (U)",
                  "fee": "Phi mang (U)", "gross": "Truoc phi (U)",
                  "reason": "Ly do"},
                 "50 lenh live gan nhat")


def tab_live_radar(days):
    section("☀️ Live Radar — Solana (tien that)")
    live_radar_equity_realtime()
    live_radar_kpi_frag(days)
    live_radar_positions_block()
    live_radar_trades_frag(days)


RADAR_DIR = os.environ.get("RADAR_DIR") or MEME_DIR


def live_radar_halt_status(base_dir, cfg_path, today=None):
    """Ly do live_trader dang dung/khong mo lenh moi, doc dung nguon ma
    live_trader dung. Tra ve list (level, message); rong = binh thuong.

    - STOP_LIVE: process tu shutdown, vi the live VAN MO khong ai quan ly.
    - daily (live_state.json): risk_unavailable hoac realized < -pct*base
      cua NGAY UTC hien tai (live_trader reset theo ngay UTC).
    - entry_blocked: reconcile on-chain thay token la/pending BUY.
    PAUSE hien thi o cot ben canh nen khong lap lai o day.
    """
    out = []
    if os.path.exists(os.path.join(base_dir, "STOP_LIVE")):
        out.append(("error", "🛑 STOP_LIVE: live trader đã tắt — vị thế live "
                             "VẪN MỞ, không được quản lý (xoá file + restart "
                             "service để chạy lại)"))
    try:
        with open(os.path.join(base_dir, "live_state.json")) as f:
            ls = json.load(f)
    except Exception:
        ls = None
        out.append(("warning", "⚠️ Không đọc được live_state.json"))
    pct = 0.20
    try:
        with open(cfg_path) as f:
            pct = float(json.load(f).get("daily_stop_pct", pct))
    except Exception:
        pass
    if ls:
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        d = ls.get("daily") or {}
        if d.get("day") == today:
            base = d.get("day_start_portfolio_usd") or 1.0
            realized = float(d.get("realized_usd") or 0.0)
            if d.get("risk_unavailable"):
                out.append(("error", "🛑 Chặn mở mới: chưa đo được portfolio "
                                     "đầu ngày (risk_unavailable)"))
            elif realized < -pct * base:
                out.append(("error", f"🛑 DAILY STOP: realized {realized:+.2f}$ "
                                     f"< -{pct:.0%} × {base:,.2f}$ "
                                     "(mở lại ngày UTC mới)"))
        if ls.get("entry_blocked"):
            out.append(("warning", "⚠️ BLOCKED: "
                        f"{ls.get('block_reason') or 'unknown'} "
                        f"(từ {ls.get('block_since') or '?'} UTC)"))
    return out

PM2_BIN = os.environ.get("PM2_BIN", "pm2")


def process_manager(env=None):
    """'pm2' | 'systemd' - bot chay bang gi (de doc trang thai/log).

    Env PROCESS_MANAGER ghi de; mac dinh: dashboard chay duoi pm2 (deploy/
    ecosystem dat DEPLOY_APP, pm2 dat pm_id) -> pm2, nguoc lai systemd."""
    env = os.environ if env is None else env
    pm = (env.get("PROCESS_MANAGER") or "").strip().lower()
    if pm in ("pm2", "systemd"):
        return pm
    return "pm2" if (env.get("DEPLOY_APP") or "pm_id" in env) else "systemd"


def parse_pm2_jlist(out):
    """Output `pm2 jlist` -> {ten: {status, out_log, err_log, restarts,
    uptime_ms, exit_code}}. Bo module pm2 va dong canh bao truoc JSON."""
    lines = (out or "").splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("["):
            continue
        try:
            data = json.loads("\n".join(lines[i:]))
        except ValueError:
            continue
        if not isinstance(data, list):
            continue
        res = {}
        for p in data:
            env = p.get("pm2_env") or {}
            if env.get("pmx_module"):
                continue
            status = env.get("status")
            # pm2 gan 'waiting restart' ca khi exit code thuoc stop_exit_codes
            # (bot tu dung, KHONG restart that)
            if status == "waiting restart" and not p.get("pid") and \
                    env.get("exit_code") in (env.get("stop_exit_codes") or []):
                status = "stopped"
            res[p.get("name")] = {
                "status": status,
                "out_log": env.get("pm_out_log_path"),
                "err_log": env.get("pm_err_log_path"),
                "restarts": env.get("restart_time", 0),
                "uptime_ms": env.get("pm_uptime"),
                "exit_code": env.get("exit_code"),
            }
        return res
    return {}


def tail_lines(path, n=20):
    """n dong cuoi file (doc toi da 64KB cuoi)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 64 * 1024))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except (OSError, TypeError):
        return []


def service_states(names, manager, runner=None):
    """{ten: (dang_chay, mo_ta)} theo pm2 hoac systemd."""
    import subprocess
    runner = runner or subprocess.run
    res = {}
    if manager == "pm2":
        try:
            r = runner([PM2_BIN, "jlist"], capture_output=True, text=True,
                       timeout=15)
            procs = parse_pm2_jlist(r.stdout)
        except Exception as e:
            return {n: (False, "khong goi duoc pm2: %s" % e) for n in names}
        for n in names:
            p = procs.get(n)
            if not p:
                res[n] = (False, "chua co trong pm2")
            elif p["status"] == "online":
                res[n] = (True, "pm2 online, %s lan restart" % p["restarts"])
            else:
                res[n] = (False, "pm2 %s%s" % (
                    p["status"], "" if p["exit_code"] is None
                    else " (exit %s)" % p["exit_code"]))
        return res
    for n in names:
        try:
            r = runner(["systemctl", "is-active", n], capture_output=True,
                       text=True, timeout=5)
            state = r.stdout.strip()
        except Exception as e:
            state = "loi: %s" % e
        res[n] = (state == "active", "systemd %s" % state)
    return res


def service_logs(name, manager, n=20, runner=None):
    """Log moi nhat cua service (pm2: file log; systemd: journalctl)."""
    import subprocess
    runner = runner or subprocess.run
    if manager == "pm2":
        r = runner([PM2_BIN, "jlist"], capture_output=True, text=True,
                   timeout=15)
        p = parse_pm2_jlist(r.stdout).get(name) or {}
        out = tail_lines(p.get("out_log"), n)
        err = tail_lines(p.get("err_log"), max(5, n // 2))
        if p.get("err_log") and p.get("err_log") == p.get("out_log"):
            err = []
        text = "\n".join(out)
        if err:
            text += "\n--- stderr ---\n" + "\n".join(err)
        return text.strip()
    r = runner(["journalctl", "-u", name, "--since", "10 min ago",
                "--no-pager", "-n", str(n)],
               capture_output=True, text=True, timeout=10)
    lines = []
    for line in (r.stdout or "").strip().split("\n")[-n:]:
        # Cat bo phan dau "Oct 06 22:23:14 ip-... python[xxx]: "
        if "python[" in line:
            line = line.split("python[", 1)[1].split("]: ", 1)[-1]
        lines.append(line)
    return "\n".join(lines).strip()


def tab_monitor():
    """Tab giam sat he thong real: status service + log realtime."""
    st.subheader("🖥️ Giám sát hệ thống REAL")

    services = {
        "muse-binance": "🟡 Binance Futures LIVE",
        "muse-radar": "🦅 Radar paper (Solana)",
        "muse-live-trader": "☀️ Live Trader (Solana tiền thật)",
        "muse-dashboard": "📊 Dashboard",
        "muse-live-equity": "📈 Equity ví live",
    }

    # Status services
    manager = process_manager()
    st.markdown("### Trạng thái service")
    st.caption("Quản lý bằng %s" % manager)
    states = service_states(list(services), manager)
    cols = st.columns(len(services))
    for i, (svc, label) in enumerate(services.items()):
        active, detail = states.get(svc, (False, ""))
        with cols[i]:
            if active:
                # Kiem tra them halt ben trong cho Binance
                extra = ""
                if svc == "muse-binance":
                    try:
                        with open(BINANCE_STATE) as f:
                            bs = json.load(f)
                        halt = bs.get("halt_reason") or bs.get("halted")
                        if halt:
                            st.error(f"🛑 {label}\nHALT: {halt}")
                            continue
                        pos = len(bs.get("positions", []))
                        extra = f" ({pos} vị thế)"
                    except Exception:
                        pass
                # Kiem tra block cho live-trader
                if svc == "muse-live-trader":
                    try:
                        lp = os.path.join(MEME_DIR, "live_state.json")
                        with open(lp) as f:
                            ls = json.load(f)
                        if ls.get("entry_blocked"):
                            reason = ls.get("block_reason", "unknown")
                            since = ls.get("block_since", "")
                            st.warning(f"⚠️ {label}\nBLOCKED: {reason}\nTừ: {since}")
                            continue
                    except Exception:
                        pass
                st.success(f"✅ {label}{extra}")
            else:
                st.error(f"❌ {label}\n{detail}")

    st.divider()

    # Dieu khien bot chu dong: Pause/Resume - TACH RIENG
    st.markdown("### ⏯️ Điều khiển bot")

    # --- Binance Live ---
    st.markdown("#### 🟡 Binance Futures LIVE")
    pause_file = os.path.join(BINANCE_DIR, "PAUSE")
    is_paused = os.path.exists(pause_file)
    col1, col2 = st.columns(2)
    with col1:
        if is_paused:
            st.warning("⏸️ Đang PAUSE")
            if st.button("▶️ Resume", key="resume_binance"):
                try:
                    os.remove(pause_file)
                    st.success("Đã resume")
                    st.rerun()
                except Exception as e:
                    st.error(f"Lỗi: {e}")
        else:
            st.info("▶️ Đang chạy")
            if st.button("⏸️ Pause", key="pause_binance"):
                try:
                    open(pause_file, 'w').write(
                        f"Paused at {datetime.now(TZINFO).isoformat()}\n")
                    st.warning("Đã pause")
                    st.rerun()
                except Exception as e:
                    st.error(f"Lỗi: {e}")
    with col2:
        try:
            with open(BINANCE_STATE) as f:
                bs = json.load(f)
            if bs.get("halted"):
                st.error(f"🛑 HALT: {bs.get('halt_reason')}")
                # KHONG sua thang state.json: bot dang chay giu state trong
                # RAM se ghi de (halt quay lai) va ban ghi cu co the de mat
                # lot vua mo. Gui yeu cau, bot tu kiem tra an toan roi go.
                req_p = os.path.join(BINANCE_DIR, "CLEAR_HALT")
                if os.path.exists(req_p):
                    st.info("⏳ Đã gửi yêu cầu xóa halt, bot đang kiểm tra "
                            "(~10 giây)…")
                elif st.button("✅ Xóa halt", key="clear_halt_binance"):
                    try:
                        with open(req_p, "w") as f:
                            f.write(datetime.now(TZINFO).isoformat() + "\n")
                        st.info("Đã gửi yêu cầu - bot sẽ kiểm tra sàn khớp "
                                "state + đủ SL/TP rồi mới gỡ halt.")
                    except Exception as e:
                        st.error(f"Lỗi: {e}")
            else:
                st.success("✅ Không halt")
            res = _clear_halt_result()
            if res:
                msg = (f"Yêu cầu xóa halt lúc {res['when']}: "
                       f"{res.get('message', '')}")
                (st.success if res.get("ok") else st.warning)(
                    ("✅ " if res.get("ok") else "⛔ Bot từ chối - ") + msg)
        except Exception as e:
            st.info(f"Không đọc được state: {e}")

    st.divider()

    # --- Radar Live (Solana) ---
    st.markdown("#### ☀️ Radar Live (Solana tiền thật)")
    radar_pause = os.path.join(MEME_DIR, "PAUSE")
    radar_paused = os.path.exists(radar_pause)
    rcol1, rcol2 = st.columns(2)
    with rcol1:
        if radar_paused:
            st.warning("⏸️ Đang PAUSE")
            if st.button("▶️ Resume", key="resume_radar"):
                try:
                    os.remove(radar_pause)
                    st.success("Đã resume")
                    st.rerun()
                except Exception as e:
                    st.error(f"Lỗi: {e}")
        else:
            st.info("▶️ Đang chạy")
            if st.button("⏸️ Pause", key="pause_radar"):
                try:
                    open(radar_pause, 'w').write(
                        f"Paused at {datetime.now(TZINFO).isoformat()}\n")
                    st.warning("Đã pause")
                    st.rerun()
                except Exception as e:
                    st.error(f"Lỗi: {e}")
    with rcol2:
        # Trang thai chan/dung THAT cua live_trader (truoc day doc file HALT
        # ma khong module nao ghi -> luon bao "Khong halt").
        issues = live_radar_halt_status(RADAR_DIR, LIVE_CFG_P)
        if not issues:
            st.success("✅ Không halt / không chặn mở mới")
        for level, msg in issues:
            (st.error if level == "error" else st.warning)(msg)

    st.divider()

    # Log realtime
    st.markdown("### Log realtime (20 dòng mới nhất)")
    svc_choice = st.selectbox(
        "Chọn service",
        list(services.keys()),
        format_func=lambda x: services[x])

    try:
        logs = service_logs(svc_choice, manager, 20)
        if logs:
            st.code(logs, language="text")
        else:
            st.info("Chưa có log.")
    except Exception as e:
        st.error(f"Không đọc được log: {e}")

    st.divider()
    st.markdown("### Vị thế LIVE đang mở")
    # Binance positions tu state
    try:
        with open(BINANCE_STATE) as f:
            bstate = json.load(f)
        bpos = bstate.get("positions", [])
        halt = bstate.get("halt_reason")
        if halt:
            st.warning(f"⚠️ Binance HALT: {halt}")
        if bpos:
            df = pd.DataFrame([{
                "Symbol": p.get("symbol"),
                "Side": p.get("side"),
                "Tag": p.get("tag"),
                "Entry": p.get("entry"),
            } for p in bpos])
            st.dataframe(df, use_container_width=True)
        else:
            st.info("Binance: không có vị thế mở.")
    except Exception as e:
        st.error(f"Không đọc được Binance state: {e}")

    # Solana live positions
    try:
        lp = LIVE_POS_P
        with open(lp) as f:
            spos = json.load(f)
        if spos:
            df = pd.DataFrame([{
                "Symbol": p.get("symbol"),
                "Entry": f"{p.get('entry', 0):.2e}",
                "Size $": p.get("size_usd"),
                "Còn lại": f"{p.get('remaining', 1)*100:.0f}%",
                "Manual": "✓" if p.get("manual_add") else "",
            } for p in spos])
            st.dataframe(df, use_container_width=True)
        else:
            st.info("Solana: không có vị thế live.")
    except Exception as e:
        st.error(f"Không đọc được Solana positions: {e}")


# ---------------- tab PAPER ----------------
def okx_positions_block():
    st.markdown('<div class="sub2">Vi the dang mo — OKX</div>', unsafe_allow_html=True)
    okx_st = load_state(OKX_STATE)
    if not okx_st:
        st.warning("Khong doc duoc file trang thai bot OKX.")
        return
    pos = okx_st.get("positions", [])
    st.caption(f"{len(pos)} vi the — equity {okx_st.get('equity', '?')}")
    if pos:
        st.dataframe(pd.DataFrame([{
            "Cap": p.get("inst"), "Chieu": p.get("side"),
            "Loai": p.get("tag"), "Entry": p.get("entry"),
            "Notional": p.get("notional"),
        } for p in pos]), width="stretch")


@st.cache_data(ttl=15)
def jupiter_marks(mints_key):
    """Gia batch Jupiter cho list mint (key = chuoi phan cach boi dau phay)."""
    mints = [m for m in (mints_key or "").split(",") if m]
    if not mints:
        return {}
    try:
        r = requests.get("https://lite-api.jup.ag/price/v3",
                         params={"ids": ",".join(mints)}, timeout=10)
        d = r.json()
        out = {}
        for m in mints:
            px = (d.get(m) or {}).get("usdPrice")
            if px:
                out[m] = float(px)
        return out
    except Exception:
        return {}


@frag15
def radar_positions_block():
    st.markdown('<div class="sub2">Vi the dang mo — Radar (live 15s)</div>',
                unsafe_allow_html=True)
    r_st = load_state(RADAR_STATE)
    if not r_st:
        st.warning("Khong doc duoc file trang thai radar.")
        return
    allp = r_st.get("paper", []) + r_st.get("paper_holder", [])
    mints = ",".join(dict.fromkeys(
        [p.get("token") for p in allp if p.get("token")]))
    marks = jupiter_marks(mints)
    for key, name in (("paper", "Scalp"), ("paper_holder", "Holder")):
        pp = r_st.get(key, [])
        st.caption(f"{name}: {len(pp)} vi the")
        if pp:
            rows = []
            for p in pp:
                try:
                    e = float(p.get("entry", 0) or 0)
                    s = float(p.get("size_usd", 0) or 0)
                    rem = float(p.get("remaining", 1) or 0)
                    mk = marks.get(p.get("token", ""), 0) or 0
                    upnl = (mk - e) / e * s * rem if e > 0 and mk > 0 else 0.0
                except Exception:
                    upnl, mk = 0.0, 0.0
                rows.append({
                    "Symbol": p.get("symbol"),
                    "Vi": (p.get("wallet") or "")[:10],
                    "Entry": p.get("entry"),
                    "Gia live": round(mk, 6) if mk else None,
                    "Lãi/lỗ live (U)": round(upnl, 2),
                    "Size $": p.get("size_usd"),
                    "Con lai": f"{(p.get('remaining', 1) or 0):.0%}",
                })
            df = pd.DataFrame(rows)

            def _c(v):
                try:
                    return "color:#16a34a" if float(v) >= 0 else "color:#dc2626"
                except Exception:
                    return ""
            st.dataframe(df.style.map(_c, subset=["Lãi/lỗ live (U)"]),
                         width="stretch")


def radar_live_equity():
    """Tinh equity radar paper TRUC TIEP luc render (khong qua snapshot):
    tong pnl lenh dong + realized vi the mo.
    KHONG tinh unrealized theo gia live (so ao, khong phan anh thuc te paper)."""
    st_ = load_state(RADAR_STATE)
    if not st_:
        return None, "khong doc duoc radar_state"
    pos = st_.get("paper", []) + st_.get("paper_holder", [])
    # pnl lenh dong tu DB (da sync)
    r = q("SELECT COALESCE(SUM(pnl_usd),0) AS s FROM radar_trades")
    closed = float(r[0]["s"]) if r else 0.0
    real_o = 0.0
    for p in pos:
        try:
            real_o += float(p.get("realized", 0) or 0)
        except Exception:
            pass
    return closed + real_o, f"{len(pos)} vi the mo (chi P&L thuc)"


@frag15
def radar_equity_realtime():
    st.markdown('<div class="sub2">⚡ Equity realtime</div>',
                unsafe_allow_html=True)
    eq, note = radar_live_equity()
    rows = q("""SELECT ts AT TIME ZONE 'Asia/Ho_Chi_Minh' AS ts, equity
                FROM equity_snapshots WHERE system='radar'
                  AND ts >= now() - interval '6 hours'
                ORDER BY ts""")
    if eq is None:
        st.warning(f"Khong tinh duoc equity: {note}")
        return
    cls = "pos" if eq >= 0 else "neg"
    c1, c2 = st.columns([1, 3])
    with c1:
        st.markdown(f'<div class="kpi-card" style="border-left-color:'
                    f'{"#16a34a" if eq >= 0 else "#dc2626"}">'
                    f'<div class="kpi-label">Equity paper (P&L tich luy)</div>'
                    f'<div class="kpi-value {cls}">{eq:+.1f} U</div>'
                    f'<div class="kpi-sub">{note} · live</div></div>',
                    unsafe_allow_html=True)
    with c2:
        if rows:
            df = pd.DataFrame(rows)
            df["ts"] = pd.to_datetime(df["ts"])
            fig = px.line(df, x="ts", y="equity",
                          title="Equity 6h qua (snapshot 15s)")
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)",
                              xaxis_title=None, yaxis_title="U")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Chua co snapshot — doi service muse-equity-snap.")
    st.caption("Tự refresh 15s · P&L thực từ lệnh đã đóng + realized (không tính unrealized ảo theo giá live).")


# okx_trades.pnl da tru phi DONG o bot (paper_engine/live_okx: net = pnl -
# fee_exit; phi MO tru vao equity luc open, khong nam trong pnl). Cot fee do
# sync_jsonl ghi = phi mo + phi dong (2 x 0.05% x notional). Vi vay P&L rong
# = pnl - fee/2 (chi tru them phi mo); "pnl - fee" truoc day tru phi dong 2 lan.
@frag
def okx_kpi_frag(where, params):
    d = day_filter()
    okx = q(f"SELECT {d} AS day, (pnl - COALESCE(fee, 0) / 2.0) AS net, tag, reason "
            f"FROM okx_trades {where} ORDER BY closed_at", params)
    section("Bot OKX — paper trade")
    kpi_cards("Hieu suat", okx)
    pnl_charts(daily_df(okx), "OKX paper")
    okx_positions_block()
    rokx = q(f"""SELECT closed_at, inst AS symbol, side, tag,
                        (pnl - COALESCE(fee, 0) / 2.0) AS net, reason
                 FROM okx_trades {where}
                 ORDER BY closed_at DESC LIMIT 50""", params)
    trades_table(rokx,
                 {"closed_at": "Dong luc", "symbol": "Cap",
                  "side": "Chieu", "tag": "Loai", "net": "P&L rong (U)",
                  "reason": "Ly do"},
                 "50 lenh OKX gan nhat")


@frag
def radar_kpi_frag(where, params):
    d = day_filter()
    scalp = q(f"SELECT {d} AS day, pnl_usd AS net, wallet, reason "
              f"FROM radar_trades {where} AND plan='scalp' ORDER BY closed_at",
              params)
    holder = q(f"SELECT {d} AS day, pnl_usd AS net, wallet, reason "
               f"FROM radar_trades {where} AND plan='holder' ORDER BY closed_at",
               params)
    kpi_cards("Plan scalp", scalp)
    kpi_cards("Plan holder", holder)

    ds, dh = daily_df(scalp), daily_df(holder)
    for df_, nm in ((ds, "Scalp"), (dh, "Holder")):
        df_["he"] = nm
    alld = pd.concat([ds, dh], ignore_index=True)
    st.markdown('<div class="sub2">P&L theo ngay</div>', unsafe_allow_html=True)
    if not alld.empty:
        c1, c2 = st.columns(2)
        with c1:
            fig = px.line(alld, x="day", y="cum", color="he",
                          title="Radar — P&L cong don (U)")
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, width="stretch")
        with c2:
            fig = px.bar(alld, x="day", y="net", color="he",
                         barmode="group",
                         title="Radar — P&L tung ngay (U)")
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, width="stretch")
    else:
        st.info("Chua co du lieu trong khoang da chon.")

    st.markdown('<div class="sub2">Thong ke ly do thoat lenh — scalp</div>',
                unsafe_allow_html=True)
    rb = q(f"""SELECT reason, COUNT(*) AS n, SUM(pnl_usd) AS pnl
               FROM radar_trades {where} AND plan='scalp'
               GROUP BY reason ORDER BY pnl DESC""", params)
    if rb:
        st.dataframe(pd.DataFrame([{
            "Ly do": r["reason"] or "?", "Lenh": r["n"],
            "P&L (U)": round(r["pnl"] or 0, 1),
        } for r in rb]), width="stretch")
    else:
        st.info("Chua co du lieu.")

    st.markdown('<div class="sub2">Xep hang vi (theo P&L paper)</div>',
                unsafe_allow_html=True)
    wl = q(f"""SELECT t.wallet, w.label, COUNT(*) AS n,
                      SUM(t.pnl_usd) AS pnl,
                      AVG(CASE WHEN t.pnl_usd > 0 THEN 1.0 ELSE 0.0 END) AS wr
               FROM radar_trades t LEFT JOIN wallets w
                 ON w.address = t.wallet
               {where} GROUP BY t.wallet, w.label
               ORDER BY pnl DESC""", params)
    if wl:
        st.dataframe(pd.DataFrame([{
            "Vi": (r["wallet"] or "")[:12],
            "Label": r["label"] or "",
            "Lenh": r["n"], "P&L (U)": round(r["pnl"] or 0, 1),
            "Winrate": f"{(r['wr'] or 0):.0%}",
        } for r in wl]), width="stretch")
    else:
        st.info("Chua co du lieu.")


@frag
def radar_trades_frag(where, params):
    rrc = q(f"""SELECT closed_at, symbol, plan, pnl_usd AS net, reason
                FROM radar_trades {where}
                ORDER BY closed_at DESC LIMIT 50""", params)
    trades_table(rrc,
                 {"closed_at": "Dong luc", "symbol": "Symbol",
                  "plan": "Plan", "net": "P&L rong (U)",
                  "reason": "Ly do"},
                 "50 lenh radar gan nhat")


def tab_paper_okx(where, params):
    okx_kpi_frag(where, params)


def tab_paper_radar(where, params):
    section("Radar meme Solana — paper")
    radar_equity_realtime()
    radar_kpi_frag(where, params)
    radar_positions_block()
    radar_trades_frag(where, params)


# ---------------- cau hinh bot + tai khoan (G1/G2) ----------------
# Tham so chien luoc/rui ro luu DB (bot_config_versions); bot kiem version
# moi ~10s va ap dung khong restart. Dashboard bat buoc dang nhap; tai khoan
# luu bang dashboard_users (lan dau chua co user -> tao admin dau tien).
APPLY_LABEL = {"now": "áp dụng ngay", "new_lots": "áp dụng cho lot mới",
               "rebuild": "từ lần dựng lưới kế tiếp"}
FEE_TAKER = 0.0005     # Binance USDT-M VIP0 taker
FEE_MAKER = 0.0002     # Binance USDT-M VIP0 maker
SLIPPAGE = 0.0001      # moi phia, uoc tinh


def widget_spec(p):
    """Thong so number_input cho 1 Param (gia tri hien thi; % neu pct)."""
    mult = 100.0 if p.pct else 1.0
    lo = None if p.lo is None else p.lo * mult
    hi = None if p.hi is None else p.hi * mult
    if p.kind == "int":
        return {"min_value": int(lo) if lo is not None else None,
                "max_value": int(hi) if hi is not None else None,
                "step": 1, "format": "%d"}
    if p.pct:
        return {"min_value": float(lo), "max_value": float(hi),
                "step": 0.05, "format": "%.3f"}
    span = (hi - lo) if (lo is not None and hi is not None) else 100.0
    step = 0.01 if span <= 2 else 0.05 if span <= 10 else \
        0.5 if span <= 100 else 5.0
    fmt = "%.2f" if step < 0.1 else "%.1f" if step < 1 else "%.0f"
    return {"min_value": float(lo) if lo is not None else None,
            "max_value": float(hi) if hi is not None else None,
            "step": step, "format": fmt}


def to_widget(p, value):
    """Gia tri luu (phan so) -> gia tri hien thi tren widget."""
    if p.kind == "int":
        return int(value)
    if p.kind == "float":
        return round(float(value) * (100.0 if p.pct else 1.0), 6)
    return value


def from_widget(p, value):
    """Gia tri widget -> gia tri luu (bo nhieu so thuc)."""
    if p.kind == "int":
        return int(value)
    if p.kind == "float":
        return round(float(value) / (100.0 if p.pct else 1.0), 8)
    return value


def fmt_param(p, value):
    if value is None:
        return "—"
    if p.kind == "bool":
        return "bật" if value else "tắt"
    if p.kind == "float" and p.pct:
        return "%g%%" % round(float(value) * 100, 4)
    if p.kind == "float":
        return "%g" % round(float(value), 6)
    return str(value)


def estimate_trade(margin, leverage, tp_pct, sl_pct, entry_maker=False,
                   fee_taker=FEE_TAKER, fee_maker=FEE_MAKER,
                   slippage=SLIPPAGE):
    """Lai/lo uoc tinh 1 lot (USDT) sau phi + truot gia.

    Mac dinh vao + ra deu market (hanh vi hien tai); entry_maker=True khi
    vao bang limit maker (grid v2)."""
    notional = float(margin) * float(leverage)
    fee_in = fee_maker if entry_maker else fee_taker
    cost = fee_in + fee_taker + 2 * slippage
    return {"notional": notional,
            "net_tp": notional * (float(tp_pct) - cost),
            "net_sl": -notional * (float(sl_pct) + cost),
            "cost_pct": cost}


def apply_status(latest, applied, now=None):
    """(muc do, thong diep) trang thai bot ap dung config.

    muc do: ok | pending | error | unknown."""
    if latest is None:
        if applied and applied.get("status") == "error":
            return "error", ("Chưa có version config nào - bot không tự tạo "
                             "được: %s" % (applied.get("error") or ""))
        return "unknown", ("Chưa có version config nào (bot tự tạo từ config "
                           "đang chạy khi kết nối được DB, thử lại mỗi 60s).")
    if not applied:
        return "unknown", ("Bot chưa báo cáo áp dụng config (bot chưa chạy "
                           "bản mới hoặc chưa kết nối DB).")
    now = now or datetime.now(timezone.utc)
    ver, status = applied.get("version"), applied.get("status")
    at = applied.get("applied_at")
    age = (now - at).total_seconds() if at else None
    if status == "error" and (ver is None or ver >= latest):
        return "error", ("Bot TỪ CHỐI version %s (vẫn chạy config cũ): %s"
                         % (ver, applied.get("error") or ""))
    if ver == latest:
        return "ok", "Bot đang chạy version %s." % latest
    if age is not None and age > 120:
        return "error", ("Bot vẫn ở version %s, chưa nhận version %s sau %d "
                         "giây — kiểm tra bot." % (ver, latest, age))
    return "pending", ("Bot đang ở version %s; version %s sẽ được áp dụng "
                       "trong ~10 giây." % (ver, latest))


def bot_runtime_note(state, now=None):
    """state['runtime_config'] (bot ghi moi vong) -> (muc do, thong diep) de
    chan doan khi DB chua co version / bot chua bao ap dung. None = khong
    co thong tin (bot ban cu / chua chay)."""
    rc = (state or {}).get("runtime_config")
    if not isinstance(rc, dict):
        return None
    now = now if now is not None else time.time()
    age = now - float(rc.get("ts") or 0)
    db = rc.get("db") or "?"
    ver = rc.get("version")
    msg = ("Bot báo (state.json, %s): đang chạy config version %s (nguồn %s); "
           "DB: %s" % ("%ds trước" % age if age < 3600 else "CŨ > 1 giờ",
                       ver if ver is not None else "—",
                       rc.get("source") or "?", db))
    if rc.get("error"):
        msg += "; lỗi config: %s" % rc["error"]
    if age > 300:
        return "warning", msg + " — bot có thể đã dừng."
    return ("ok" if db == "ok" and not rc.get("error") else "error"), msg


def usd(x):
    return ("-$%.2f" if x < 0 else "$%.2f") % abs(x)


def changed_keys(prev_cfg, cfg):
    if not prev_cfg:
        return []
    return [k for k in sorted(set(prev_cfg) | set(cfg))
            if prev_cfg.get(k) != cfg.get(k)]


def _rerun_fragment():
    try:
        st.rerun(scope="fragment")
    except Exception:
        st.rerun()


def db_call(fn, *args, **kwargs):
    """Goi ham bot_config voi ket noi dung chung; mat ket noi -> thu lai 1
    lan. Loi nghiep vu (ValueError/PermissionError) nem cho UI hien thi."""
    for attempt in range(2):
        con = get_conn()
        if con is None:
            raise RuntimeError("Chưa đặt DATABASE_URL")
        try:
            return fn(con, *args, **kwargs)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            try:
                con.close()
            except Exception:
                pass
            get_conn.clear()
            if attempt == 1:
                raise


@st.cache_resource
def _ensure_config_schema():
    db_call(bc.ensure_tables)
    return True


def current_user():
    return st.session_state.get("auth_user")


def is_admin():
    u = current_user()
    return bool(u and u.get("role") == "admin")


# ---- phien dang nhap giu qua F5 (cookie token + bang dashboard_sessions)
SESSION_COOKIE = "mb_session"
SESSION_DAYS = float(os.environ.get("DASHBOARD_SESSION_DAYS", bc.SESSION_DAYS))
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")


def cookie_js(token=None, days=SESSION_DAYS):
    """Script ghi (token) / xoa (None) cookie phien tren trang CHA.

    Streamlit khong cho dat header Set-Cookie nen phai ghi bang JS qua iframe
    components (sandbox allow-same-origin) -> cookie khong the HttpOnly.
    Trang chay HTTPS thi tu them Secure."""
    if token:
        if not _TOKEN_RE.match(token):
            raise ValueError("token phiên không hợp lệ")
        value, age = token, int(days * 86400)
    else:
        value, age = "", 0
    return ("<script>(function(){var w=window.parent;"
            "var c='%s=%s; Max-Age=%d; Path=/; SameSite=Strict';"
            "if(w.location.protocol==='https:'){c+='; Secure';}"
            "w.document.cookie=c;})();</script>"
            % (SESSION_COOKIE, value, age))


def _cookie_token():
    try:
        token = st.context.cookies.get(SESSION_COOKIE)
    except Exception:                    # Streamlit < 1.42: khong doc duoc
        return None
    return (token if isinstance(token, str) and _TOKEN_RE.match(token)
            else None)


def _run_js(html):
    """Chay script trong iframe cung origin (st.iframe o Streamlit moi,
    components.html o ban cu - sap bi bo)."""
    frame = getattr(st, "iframe", None)
    if frame is not None:
        frame(html, height=1)
    else:
        components.html(html, height=0)


def _flush_cookie_cmd():
    """Ghi/xoa cookie da hen o lan chay truoc (st.rerun() cat ngang trang nen
    script phai render o lan chay sau)."""
    cmd = st.session_state.pop("_cookie_cmd", None)
    if cmd is not None:
        _run_js(cookie_js(cmd or None))


def _start_session(user, remember=True):
    st.session_state["auth_user"] = {"username": user["username"],
                                     "role": user["role"]}
    if not remember:
        return
    try:
        token = db_call(bc.create_session, user["username"], SESSION_DAYS)
    except Exception as e:               # phien van chay theo tab
        st.session_state["_session_err"] = str(e)
        return
    st.session_state["_session_token"] = token
    st.session_state["_cookie_cmd"] = token


def _end_session(all_devices=False):
    user = current_user()
    token = st.session_state.pop("_session_token", None) or _cookie_token()
    try:
        if all_devices and user:
            db_call(bc.delete_user_sessions, user["username"])
        else:
            db_call(bc.delete_session, token)
    except Exception:
        pass
    st.session_state.pop("auth_user", None)
    st.session_state["_cookie_cmd"] = ""


def auth_gate():
    """Chan toan bo dashboard toi khi dang nhap. Tra ve user dict."""
    try:
        _ensure_config_schema()
    except Exception as e:
        st.error("Không tạo được bảng tài khoản/config trong DB: %s" % e)
        st.stop()
    _flush_cookie_cmd()
    user = current_user()
    if not user:
        token = _cookie_token()
        restored = db_call(bc.session_user, token) if token else None
        if restored:
            st.session_state["auth_user"] = user = restored
            st.session_state["_session_token"] = token
        elif token:
            _run_js(cookie_js(None))   # phien het han
    if user:
        fresh = db_call(bc.get_user, user["username"])
        if fresh and fresh["is_active"]:
            user = {"username": fresh["username"], "role": fresh["role"]}
            st.session_state["auth_user"] = user
            return user
        _end_session()
        _flush_cookie_cmd()
        st.warning("Phiên đăng nhập đã hết hiệu lực (tài khoản bị khoá/xoá).")
    if db_call(bc.user_count) == 0:
        st.subheader("🔐 Khởi tạo tài khoản quản trị")
        st.info("Chưa có tài khoản nào. Tạo tài khoản **admin đầu tiên** — "
                "chỉ làm được 1 lần; tài khoản sau thêm trong tab Quản trị.")
        with st.form("first_admin"):
            u = st.text_input("Tên đăng nhập")
            p1 = st.text_input("Mật khẩu (≥ 8 ký tự)", type="password")
            p2 = st.text_input("Nhập lại mật khẩu", type="password")
            ok = st.form_submit_button("Tạo admin")
        if ok:
            errs = bc.check_new_credentials(u.strip(), p1, p2)
            if errs:
                for e in errs:
                    st.error(e)
            elif db_call(bc.create_first_admin, u.strip(), p1):
                _start_session({"username": u.strip(), "role": "admin"})
                st.rerun()
            else:
                st.error("Đã có tài khoản khác được tạo trước — hãy đăng nhập.")
        st.stop()
    st.subheader("🔐 Đăng nhập")
    with st.form("login"):
        u = st.text_input("Tên đăng nhập")
        p = st.text_input("Mật khẩu", type="password")
        remember = st.checkbox("Ghi nhớ đăng nhập %g ngày (F5 không phải "
                               "đăng nhập lại)" % SESSION_DAYS, value=True)
        ok = st.form_submit_button("Đăng nhập")
    if ok:
        user, msg = db_call(bc.authenticate, u.strip(), p)
        if user:
            _start_session(user, remember)
            st.rerun()
        st.error(msg)
    st.stop()


def sidebar_account(user):
    with st.sidebar:
        st.markdown("👤 **%s** · %s" % (user["username"], user["role"]))
        if st.button("Đăng xuất", key="logout"):
            _end_session()
            st.rerun()
        if st.button("Đăng xuất mọi thiết bị", key="logout_all",
                     help="Thu hồi mọi phiên ghi nhớ của tài khoản này"):
            _end_session(all_devices=True)
            st.rerun()
        if st.session_state.get("_session_err"):
            st.caption("⚠️ Không lưu được phiên (F5 sẽ phải đăng nhập lại): "
                       + st.session_state["_session_err"])
        with st.expander("Đổi mật khẩu"):
            with st.form("self_pw"):
                p1 = st.text_input("Mật khẩu mới", type="password")
                p2 = st.text_input("Nhập lại", type="password")
                ok = st.form_submit_button("Đổi")
            if ok:
                if p1 != p2:
                    st.error("Không khớp")
                else:
                    try:
                        db_call(bc.reset_password, user["username"],
                                user["username"], p1)
                        # doi mat khau thu hoi moi phien -> cap phien moi
                        # cho thiet bi hien tai neu dang ghi nho
                        if st.session_state.pop("_session_token", None):
                            _start_session(user)
                            _flush_cookie_cmd()
                        st.success("Đã đổi mật khẩu (các thiết bị khác đã "
                                   "bị đăng xuất)")
                    except (ValueError, PermissionError) as e:
                        st.error(str(e))


def _ts_local(ts):
    if ts is None:
        return "—"
    return ts.astimezone(TZINFO).strftime("%d/%m %H:%M:%S")


def _param_widget(p, value, key, disabled):
    label = p.label
    help_ = " · ".join(x for x in (p.help, APPLY_LABEL.get(p.apply, ""))
                       if x)
    if p.kind == "bool":
        return st.checkbox(label, value=bool(value), key=key,
                           disabled=disabled, help=help_)
    if p.kind == "enum":
        idx = p.choices.index(value) if value in p.choices else 0
        return st.selectbox(label, p.choices, index=idx, key=key,
                            disabled=disabled, help=help_)
    spec = widget_spec(p)
    v = to_widget(p, value)
    if spec["min_value"] is not None:
        v = max(spec["min_value"], v)
    if spec["max_value"] is not None:
        v = min(spec["max_value"], v)
    return st.number_input(label, value=v, key=key, disabled=disabled,
                           help=help_, **spec)


def _tab_config():
    admin = is_admin()
    bot = bc.BOT_BINANCE
    row = db_call(bc.load_version, bot)
    applied = db_call(bc.applied, bot)
    latest = row["version"] if row else None
    base = bc.extract({}) if row is None else dict(bc.defaults(),
                                                   **row["config"])
    level, msg = apply_status(latest, applied)
    {"ok": st.success, "pending": st.info, "error": st.error,
     "unknown": st.warning}[level](msg)
    if level != "ok":
        note = bot_runtime_note(load_state(BINANCE_STATE))
        if note:
            {"ok": st.caption, "warning": st.warning,
             "error": st.error}[note[0]](note[1])
        else:
            st.caption("Không có trạng thái config trong state.json của bot "
                       "(bot chưa chạy bản mới hoặc chưa chạy).")
    if row:
        st.caption("Version %s · %s · %s%s" % (
            latest, row["author"], _ts_local(row["created_at"]),
            (" · " + row["note"]) if row.get("note") else ""))
    if not admin:
        st.caption("Chỉ admin được sửa cấu hình.")

    new = {}
    vkey = latest or 0
    for group in bc.GROUPS:
        params = [p for p in bc.PARAMS if p.group == group]
        with st.expander(group, expanded=group in ("Lệnh", "Grid", "Rủi ro")):
            cols = st.columns(3)
            for i, p in enumerate(params):
                with cols[i % 3]:
                    w = _param_widget(p, base.get(p.key, p.default),
                                      "cfg:%s:%s" % (p.key, vkey),
                                      disabled=not admin)
                    new[p.key] = from_widget(p, w)

    est = estimate_trade(new["order_margin_usdt"], new["leverage"],
                         new["grid.tp_pct"] or new["grid.step_min"],
                         new["grid.sl_pct"])
    est_mk = estimate_trade(new["order_margin_usdt"], new["leverage"],
                            new["grid.tp_pct"] or new["grid.step_min"],
                            new["grid.sl_pct"], entry_maker=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Notional mỗi lot", "$%s" % format(round(est["notional"]), ","))
    c2.metric("Net khi chạm TP (vào market)", usd(est["net_tp"]))
    c3.metric("Net khi chạm TP (vào limit maker)", usd(est_mk["net_tp"]))
    c4.metric("Lỗ khi chạm SL sàn", usd(est["net_sl"]))
    st.caption("Ước tính phí taker %.2f%% / maker %.2f%% + trượt giá %.2f%% "
               "mỗi phía. Grid hiện vào lệnh market; vào limit maker là "
               "giai đoạn G5." % (FEE_TAKER * 100, FEE_MAKER * 100,
                                 SLIPPAGE * 100))

    clean, errors = bc.validate(new)
    for e in errors:
        st.error(e)
    if not errors:
        n_side, side_notional = bc.same_side_exposure(clean)
        st.caption("Tối đa cùng một chiều (mọi coin): %d lot grid ≈ $%s "
                   "notional." % (n_side, format(round(side_notional), ",")))
        for w in bc.risk_warnings(clean):
            st.warning("⚠️ " + w)
    changes = bc.diff(base, clean) if not errors else []
    if changes:
        st.markdown("**Thay đổi so với version đang lưu:**")
        st.dataframe(pd.DataFrame([
            {"Tham số": bc.PARAM_BY_KEY[k].label, "Khoá": k,
             "Hiện tại": fmt_param(bc.PARAM_BY_KEY[k], a),
             "Mới": fmt_param(bc.PARAM_BY_KEY[k], b),
             "Áp dụng": APPLY_LABEL.get(bc.PARAM_BY_KEY[k].apply, "")}
            for k, a, b in changes]), hide_index=True,
            use_container_width=True)
    if admin:
        note = st.text_input("Ghi chú (lý do thay đổi)", key="cfg_note:%s"
                             % vkey)
        if st.button("💾 Lưu & áp dụng", type="primary",
                     disabled=bool(errors) or not changes):
            try:
                v = db_call(bc.save_version, clean, current_user()["username"],
                            note, bot)
                st.session_state["cfg_saved"] = v
                _rerun_fragment()
            except (ValueError, PermissionError) as e:
                st.error(str(e))
    if (latest and st.session_state.get("cfg_saved") == latest
            and level == "pending"):
        st.success("Đã lưu version %s — bot áp dụng trong ~10 giây." % latest)

    st.markdown("**Lịch sử (20 version gần nhất)**")
    hist = db_call(bc.history, bot, 20)
    rows = []
    for i, h in enumerate(hist):
        prev = hist[i + 1]["config"] if i + 1 < len(hist) else None
        ks = changed_keys(prev, h["config"])
        rows.append({"Version": h["version"], "Lúc": _ts_local(h["created_at"]),
                     "Người sửa": h["author"], "Ghi chú": h["note"] or "",
                     "Đổi": ", ".join(ks[:6]) + (" …" if len(ks) > 6 else "")})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True,
                     use_container_width=True)
    if admin and len(hist) > 1:
        c1, c2 = st.columns([1, 3])
        pick = c1.selectbox("Khôi phục version", [h["version"]
                                                  for h in hist[1:]],
                            key="cfg_restore")
        if c2.button("↩️ Khôi phục (tạo version mới)"):
            old = next(h for h in hist if h["version"] == pick)["config"]
            old = {k: v for k, v in old.items() if k in bc.PARAM_BY_KEY}
            try:
                v = db_call(bc.save_version, old, current_user()["username"],
                            "khôi phục version %s" % pick, bot)
                st.session_state["cfg_saved"] = v
                _rerun_fragment()
            except (ValueError, PermissionError) as e:
                st.error("Không khôi phục được: %s" % e)


TREND_LABEL = {"down": "🔻 giảm", "up": "🔺 tăng", "neutral": "➖ ngang"}


def trend_table(snap):
    """state['trend'] (bot ghi moi slow tick) -> (bias thi truong, rows)."""
    snap = snap or {}
    mkt = snap.get("market") or "BTCUSDT"
    syms = snap.get("symbols") or {}
    rows = []
    for sym in sorted(syms, key=lambda s: (s != mkt, s)):
        r = syms[sym] or {}
        b = r.get("bias")
        ts = r.get("ts")
        rows.append({
            "Symbol": sym + (" (thị trường)" if sym == mkt else ""),
            "Xu hướng": TREND_LABEL.get(b, b),
            "Chặn mở": {"down": "LONG", "up": "SHORT"}.get(b, "—"),
            "EMA dốc (×ATR)": r.get("slope_atr"),
            "BTC biến động %": r.get("move_pct") if sym == mkt else None,
            "Lý do": r.get("reason") or "",
            "Lấy nến lúc": _ts_local(datetime.fromtimestamp(
                float(ts), timezone.utc)) if ts else "—"})
    return (syms.get(mkt) or {}).get("bias"), rows


def _trend_section(cfg):
    on = [name for key, name in (("trend.market_filter", "xu hướng BTC"),
                                 ("trend.symbol_filter", "xu hướng từng coin"))
          if cfg.get(key)]
    st.markdown("**Lọc xu hướng grid** · %s · trần cùng chiều: %s" % (
        ("bật " + " + ".join(on)) if on else "TẮT",
        cfg.get("grid.max_same_side") or "không giới hạn"))
    snap = (load_state(BINANCE_STATE) or {}).get("trend")
    if not snap or not snap.get("symbols"):
        st.info("Chưa có dữ liệu xu hướng (bot cần chạy bản mới).")
        return
    mbias, rows = trend_table(snap)
    if mbias == "down" and cfg.get("trend.market_filter"):
        st.warning("BTC đang giảm → không mở lot grid LONG mới trên mọi coin.")
    elif mbias == "up" and cfg.get("trend.market_filter"):
        st.warning("BTC đang tăng → không mở lot grid SHORT mới trên mọi "
                   "coin.")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("Chỉ chặn MỞ MỚI phía ngược xu hướng; lot đang mở vẫn chạy tới "
               "TP/SL. Thiếu dữ liệu một symbol → symbol đó không mở mới.")
    st.divider()


def _tab_scanner():
    row = db_call(bc.load_version, bc.BOT_BINANCE)
    cfg = dict(bc.defaults(), **(row["config"] if row else {}))
    _trend_section(cfg)
    mode = cfg["scanner.mode"]
    st.caption("Chế độ: **%s** · top K = %s · quét lại mỗi %s phút. %s" % (
        mode, cfg["scanner.top_k"], cfg["scanner.rescan_minutes"],
        "Grid CHỈ mở lot mới trên top K đạt chuẩn." if mode == "filter"
        else "Chỉ quan sát, chưa ảnh hưởng giao dịch."))
    if not cfg["scanner.enabled"]:
        st.warning("Scanner đang tắt.")
    scans = db_call(bc.latest_scans, bc.BOT_BINANCE, 6)
    if not scans:
        st.info("Chưa có kết quả quét (bot cần chạy bản mới và kết nối DB).")
        return
    gcfg = {k.split(".", 1)[1]: v for k, v in cfg.items()
            if k.startswith("grid.")}
    need = range_grid.min_levels_required(gcfg) if range_grid else None
    rows = []
    for r in scans:
        m = r["metrics"] or {}
        pct = lambda x: None if x is None else round(x * 100, 2)  # noqa
        lv = range_grid.levels_per_side(m, gcfg) if range_grid else None
        ok = "✅" if r["passed"] else "—"
        if r["passed"] and lv is not None and lv < need:
            ok = "⚠️ hẹp"
        rows.append({"Symbol": r["symbol"], "Đạt": ok,
                     "Điểm": r["score"], "Tầng/phía": lv,
                     "ADX 1h": m.get("adx_1h"),
                     "ADX 15m": m.get("adx_15m"), "BB %": pct(m.get("bbw_pct")),
                     "BB pctile": m.get("bbw_pctile"),
                     "Biên %": pct(m.get("range_pct")),
                     "Cắt giữa": m.get("mid_crosses"),
                     "Vị trí": m.get("pos"), "CHOP": m.get("chop"),
                     "ER": m.get("er"),
                     "Lý do": "; ".join(r["reasons"] or []),
                     "Lúc quét": _ts_local(r["ts"])})
    n_ok = sum(1 for r in scans if r["passed"])
    st.markdown("**%d/%d symbol đạt chuẩn đi ngang**" % (n_ok, len(scans)))
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("Vị trí = giá trong biên (0 = đáy, 1 = đỉnh), chỉ cộng điểm. "
               "CHOP cao + ER thấp = dao động qua lại; ADX thấp = không trend.")
    if need is not None:
        st.caption("Tầng/phía = số tầng range grid dựng được từ biên + độ "
                   "giãn hiện tại. ⚠️ hẹp = đạt chuẩn nhưng < %d tầng/phía "
                   "→ range grid bỏ khi chọn top K." % need)


def _tab_admin():
    me = current_user()["username"]
    users = db_call(bc.list_users)
    st.dataframe(pd.DataFrame([
        {"Tên": u["username"], "Vai trò": u["role"],
         "Trạng thái": "hoạt động" if u["is_active"] else "đã khoá",
         "Tạo bởi": u["created_by"], "Tạo lúc": _ts_local(u["created_at"]),
         "Đăng nhập cuối": _ts_local(u["last_login_at"])}
        for u in users]), hide_index=True, use_container_width=True)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Thêm tài khoản**")
        with st.form("add_user", clear_on_submit=True):
            u = st.text_input("Tên đăng nhập")
            p = st.text_input("Mật khẩu (≥ 8 ký tự)", type="password")
            role = st.selectbox("Vai trò", bc.ROLES[::-1],
                                help="viewer chỉ xem; admin sửa config + "
                                     "quản lý tài khoản")
            ok = st.form_submit_button("Thêm")
        if ok:
            try:
                db_call(bc.create_user, me, u.strip(), p, role)
                st.success("Đã thêm %s" % u.strip())
                _rerun_fragment()
            except (ValueError, PermissionError) as e:
                st.error(str(e))
    with c2:
        st.markdown("**Sửa tài khoản**")
        names = [u["username"] for u in users]
        who = st.selectbox("Tài khoản", names, key="adm_who")
        target = next((u for u in users if u["username"] == who), None)
        if target:
            a1, a2 = st.columns(2)
            try:
                if a1.button("Mở khoá" if not target["is_active"] else "Khoá",
                             key="adm_active"):
                    db_call(bc.set_active, me, who, not target["is_active"])
                    _rerun_fragment()
                other = "viewer" if target["role"] == "admin" else "admin"
                if a2.button("Đổi thành %s" % other, key="adm_role"):
                    db_call(bc.set_role, me, who, other)
                    _rerun_fragment()
            except (ValueError, PermissionError) as e:
                st.error(str(e))
            with st.form("adm_pw", clear_on_submit=True):
                p = st.text_input("Mật khẩu mới cho %s" % who, type="password")
                ok = st.form_submit_button("Đặt lại mật khẩu")
            if ok:
                try:
                    db_call(bc.reset_password, me, who, p)
                    st.success("Đã đặt lại mật khẩu %s" % who)
                except (ValueError, PermissionError) as e:
                    st.error(str(e))


try:
    _frag_ui = st.fragment
except AttributeError:
    _frag_ui = lambda f: f  # noqa: E731
tab_config = _frag_ui(_tab_config)
tab_scanner = _frag_ui(_tab_scanner)
tab_admin = _frag_ui(_tab_admin)


# ---------------- main ----------------
def main():
    st.title("📊 Crypto Bots Dashboard")
    if not DB:
        st.error("Chua dat DATABASE_URL. Vi du: "
                 "DATABASE_URL=postgres://user:pass@localhost:5432/cryptobots "
                 "streamlit run app.py")
        st.stop()

    user = auth_gate()
    sidebar_account(user)
    st.caption(f"Du lieu cap nhat lan cuoi — {last_updates_line()}")

    filt = st.radio("Khoang thoi gian", ["7 ngay", "30 ngay", "Tat ca"],
                    horizontal=True, key="rng")
    days = {"7 ngay": 7, "30 ngay": 30, "Tat ca": None}[filt]

    if days is None:
        where, params = "", ()
    else:
        where = "WHERE closed_at >= now() - make_interval(days => %s)"
        params = (days,)

    names = ["🟡 Live Binance", "☀️ Live Radar", "📄 Paper OKX",
             "🦅 Paper Radar", "🖥️ Monitor", "⚙️ Cấu hình", "🧭 Scanner"]
    if user["role"] == "admin":
        names.append("👤 Quản trị")
    tabs = st.tabs(names)
    t1, t2, t3, t4, t5 = tabs[:5]
    with t1:
        tab_live_binance(where, params)
    with t2:
        tab_live_radar(days)
    with t3:
        tab_paper_okx(where, params)
    with t4:
        tab_paper_radar(where, params)
    with t5:
        tab_monitor()
    with tabs[5]:
        tab_config()
    with tabs[6]:
        tab_scanner()
    if user["role"] == "admin":
        with tabs[7]:
            tab_admin()

    st.divider()
    st.markdown('<div class="small-note">Vi the & equity live tu refresh '
                'rieng (5s/15s) · KPI tu refresh 60s · '
                'So lieu paper chi de doi chung, khong phai ket qua tien that.'
                '</div>', unsafe_allow_html=True)


main()
