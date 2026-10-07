"""Dashboard Crypto Bots — 2 tab: LIVE (tien that) va PAPER (chay thu). Tieng Viet.

Chay:  DATABASE_URL=postgres://... streamlit run app.py
Tu refresh 60s. Vi the dang mo doc truc tiep tu state file cua bot.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import psycopg
import psycopg.rows
import requests
import streamlit as st

st.set_page_config(page_title="Crypto Bots Dashboard", layout="wide")

# ---------------- cau hinh ----------------
DB = os.environ.get("DATABASE_URL")
BINANCE_STATE = os.environ.get("BINANCE_STATE",
                               "/home/ubuntu/muse_bot/binance-bot/state.json")
# File .env chua API key Binance (chi doc, khong commit secret vao code)
BINANCE_ENV_P = os.environ.get("BINANCE_ENV_P",
                               "/home/ubuntu/muse_bot/.env")
OKX_STATE = os.environ.get("OKX_STATE",
                           os.path.expanduser("~/workspace/trading-bot/state.json"))
RADAR_STATE = os.environ.get("RADAR_STATE",
                             os.path.expanduser("~/workspace/meme-radar/radar_state.json"))
_HK = os.environ.get("HELIUS_KEY_FILE")
if not _HK:
    _HK = ("/home/ubuntu/muse_bot/.helius_key"
           if os.path.exists("/home/ubuntu/muse_bot/.helius_key")
           else os.path.expanduser("~/workspace/meme-radar/.helius_key"))
HELIUS_KEY_FILE = _HK
LIVE_CFG_P = os.environ.get("LIVE_CFG_P",
                            "/home/ubuntu/muse_bot/meme-radar/config.live.json")
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
LIVE_POS_P = os.environ.get("LIVE_POS_P",
                            "/home/ubuntu/muse_bot/meme-radar/live_positions.json")
LIVE_TRADES_P = os.environ.get("LIVE_TRADES_P",
                               "/home/ubuntu/muse_bot/meme-radar/live_trades.jsonl")
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
    try:
        with open(HELIUS_KEY_FILE) as f:
            key = f.read().strip()
    except Exception:
        return None
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


@frag15
def live_radar_equity_realtime():
    st.markdown('<div class="sub2">⚡ Equity live realtime</div>',
                unsafe_allow_html=True)
    bal = sol_balance()
    sol_px = jupiter_marks(SOL_MINT).get(SOL_MINT)
    if bal is None or not sol_px:
        st.warning("Khong doc duoc so du SOL hoac gia SOL/USD.")
        return
    eq = bal * sol_px
    c1, c2 = st.columns([1, 3])
    with c1:
        st.markdown(f'<div class="kpi-card" style="border-left-color:#16a34a">'
                    f'<div class="kpi-label">Equity live (USD)</div>'
                    f'<div class="kpi-value pos">{eq:,.2f} U</div>'
                    f'<div class="kpi-sub">{bal:.4f} SOL @ ${sol_px:,.2f} · live</div>'
                    f'<div class="addr">{SOL_WALLET}</div></div>',
                    unsafe_allow_html=True)
    with c2:
        df = daily_df(live_trade_kpi_rows(
            filter_live_trades(load_live_trades(), None, live_only=True)))
        if not df.empty:
            fig = px.line(df, x="day", y="cum",
                          title="Live Radar — P&L cong don, lenh tien that (U)")
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)",
                              xaxis_title=None, yaxis_title="U")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("Chua co lenh live nao da dong.")
    st.caption("Tự refresh 15s · Equity = số dư SOL ví × giá SOL/USD (Jupiter).")


@frag
def live_radar_kpi_frag(days):
    # Chi tinh lenh tien that (mode=live), khong tron dry-run
    trades = filter_live_trades(load_live_trades(), days, live_only=True)
    kpi_cards("Hieu suat live (tien that)", live_trade_kpi_rows(trades))
    df = daily_df(live_trade_kpi_rows(trades))
    pnl_charts(df, "Live Radar")

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
            "reason": t.get("reason"),
        })
    trades_table(rows,
                 {"closed_at": "Dong luc", "symbol": "Symbol",
                  "mode": "Che do", "net": "P&L rong (U)",
                  "reason": "Ly do"},
                 "50 lenh live gan nhat")


def tab_live_radar(days):
    section("☀️ Live Radar — Solana (tien that)")
    live_radar_equity_realtime()
    live_radar_kpi_frag(days)
    live_radar_positions_block()
    live_radar_trades_frag(days)


RADAR_DIR = os.environ.get("RADAR_DIR", "/home/ubuntu/muse_bot/meme-radar")


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

def tab_monitor():
    """Tab giam sat he thong real: status service + log realtime."""
    import subprocess

    st.subheader("🖥️ Giám sát hệ thống REAL")

    services = {
        "muse-binance": "🔴 Binance Futures LIVE",
        "muse-radar": "🦅 Radar paper (Solana)",
        "muse-live-trader": "☀️ Live Trader (Solana tiền thật)",
        "muse-dashboard": "📊 Dashboard",
    }

    # Status services
    st.markdown("### Trạng thái service")
    cols = st.columns(len(services))
    for i, (svc, label) in enumerate(services.items()):
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5)
            active = r.stdout.strip() == "active"
        except Exception:
            active = False
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
                        lp = "/home/ubuntu/muse_bot/meme-radar/live_state.json"
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
                st.error(f"❌ {label}")

    st.divider()

    # Dieu khien bot chu dong: Pause/Resume - TACH RIENG
    st.markdown("### ⏯️ Điều khiển bot")

    # --- Binance Live ---
    st.markdown("#### 🔴 Binance Futures LIVE")
    pause_file = "/home/ubuntu/muse_bot/binance-bot/PAUSE"
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
                if st.button("✅ Xóa halt", key="clear_halt_binance"):
                    bs["halted"] = False
                    bs["halt_reason"] = None
                    bs.pop("halted_at", None)
                    with open(BINANCE_STATE, 'w') as f:
                        json.dump(bs, f)
                    st.success("Đã xóa halt")
                    st.rerun()
            else:
                st.success("✅ Không halt")
        except Exception as e:
            st.info(f"Không đọc được state: {e}")

    st.divider()

    # --- Radar Live (Solana) ---
    st.markdown("#### ☀️ Radar Live (Solana tiền thật)")
    radar_pause = "/home/ubuntu/muse_bot/meme-radar/PAUSE"
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
        r = subprocess.run(
            ["journalctl", "-u", svc_choice, "--since", "10 min ago",
             "--no-pager", "-n", "20"],
            capture_output=True, text=True, timeout=10)
        logs = r.stdout.strip()
        if logs:
            # Chi lay phan message, bo timestamp systemd
            lines = []
            for line in logs.split("\n")[-20:]:
                # Cat bo phan dau "Oct 06 22:23:14 ip-... python[xxx]: "
                if "python[" in line:
                    line = line.split("python[", 1)[1].split("]: ", 1)[-1]
                lines.append(line)
            st.code("\n".join(lines), language="text")
        else:
            st.info("Chưa có log trong 10 phút qua.")
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
        lp = "/home/ubuntu/muse_bot/meme-radar/live_positions.json"
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


# ---------------- main ----------------
def main():
    st.title("📊 Crypto Bots Dashboard")
    if not DB:
        st.error("Chua dat DATABASE_URL. Vi du: "
                 "DATABASE_URL=postgres://user:pass@localhost:5432/cryptobots "
                 "streamlit run app.py")
        st.stop()

    st.caption(f"Du lieu cap nhat lan cuoi — {last_updates_line()}")

    filt = st.radio("Khoang thoi gian", ["7 ngay", "30 ngay", "Tat ca"],
                    horizontal=True, key="rng")
    days = {"7 ngay": 7, "30 ngay": 30, "Tat ca": None}[filt]

    if days is None:
        where, params = "", ()
    else:
        where = "WHERE closed_at >= now() - make_interval(days => %s)"
        params = (days,)

    t1, t2, t3, t4, t5 = st.tabs([
        "🔴 Live Binance", "☀️ Live Radar",
        "📄 Paper OKX", "🦅 Paper Radar",
        "🖥️ Monitor"])
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

    st.divider()
    st.markdown('<div class="small-note">Vi the & equity live tu refresh '
                'rieng (5s/15s) · KPI tu refresh 60s · '
                'So lieu paper chi de doi chung, khong phai ket qua tien that.'
                '</div>', unsafe_allow_html=True)


main()
