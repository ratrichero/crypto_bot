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
SOL_WALLET = "DxYkrsJA6YdS1cqJ9ocPCYRBacd7Xan3DeYWZva89dLd"

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


@frag5
def binance_live_positions_block():
    """Bang vi the Binance mo — cot Lãi/lỗ live theo gia thuc, refresh 5s."""
    st.markdown('<div class="sub2">Vi the dang mo (live 5s)</div>',
                unsafe_allow_html=True)
    b_st = load_state(BINANCE_STATE)
    if not b_st:
        st.warning("Khong doc duoc file trang thai bot Binance.")
        return
    bpos = b_st.get("positions", [])
    if not bpos:
        st.caption("0 vi the dang mo.")
        return
    marks = binance_all_prices()
    rows, tot = [], 0.0
    for p in bpos:
        try:
            e = float(p.get("entry", 0) or 0)
            n = float(p.get("notional", 0) or 0)
            mk = marks.get((p.get("symbol") or "").upper(), 0) or 0
            sgn = 1 if (p.get("side") or "").lower() == "long" else -1
            upnl = (mk - e) / e * n * sgn if e > 0 and n > 0 and mk > 0 else 0.0
        except Exception:
            upnl, mk = 0.0, 0.0
        tot += upnl
        rows.append({
            "ID": p.get("id"), "Symbol": p.get("symbol"),
            "Chieu": p.get("side"), "Loai": p.get("tag"),
            "Entry": p.get("entry"),
            "Gia live": round(mk, 4) if mk else None,
            "Lãi/lỗ live (U)": round(upnl, 2),
            "SL": p.get("sl"), "TP": p.get("tp"),
            "Notional": p.get("notional"),
        })
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


def tab_live(where, params):
    d = day_filter()
    bn = q(f"SELECT {d} AS day, pnl AS net, tag, reason, live, dry "
           f"FROM binance_trades {where} ORDER BY closed_at", params)

    section("📈 Binance Futures — LIVE (tien that)")
    kpi_cards("Hieu suat", bn)
    n = len(bn)
    n_live = sum(1 for r in bn if r.get("live") and not r.get("dry"))
    n_dry = sum(1 for r in bn if r.get("dry"))
    if n:
        st.caption(f"Trong do: {n_live} lenh LIVE tien that, {n_dry} lenh dry-run.")
    pnl_charts(daily_df(bn), "Binance LIVE")

    binance_live_positions_block()

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

    st.divider()
    section("☀️ Solana meme — live")
    bal = sol_balance()
    if bal is None:
        bal_txt, status, cls = "?", "khong doc duoc so du", ""
    elif bal < 0.05:
        bal_txt, status, cls = f"{bal:.3f} SOL", "🟡 Chua nap SOL — cho funding", "neg"
    else:
        bal_txt, status, cls = f"{bal:.3f} SOL", "🟢 Da san sang test", "pos"
    c1, c2, c3 = st.columns(3)
    c1.markdown(f'<div class="kpi-card"><div class="kpi-label">Vi bot</div>'
                f'<div class="addr">{SOL_WALLET}</div></div>',
                unsafe_allow_html=True)
    c2.markdown(f'<div class="kpi-card"><div class="kpi-label">So du</div>'
                f'<div class="kpi-value">{bal_txt}</div></div>',
                unsafe_allow_html=True)
    c3.markdown(f'<div class="kpi-card"><div class="kpi-label">Trang thai</div>'
                f'<div class="kpi-value {cls}" style="font-size:17px">'
                f'{status}</div></div>', unsafe_allow_html=True)


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
    tong pnl lenh dong + realized vi the mo + unrealized theo gia Jupiter live."""
    st_ = load_state(RADAR_STATE)
    if not st_:
        return None, "khong doc duoc radar_state"
    pos = st_.get("paper", []) + st_.get("paper_holder", [])
    # pnl lenh dong tu DB (da sync)
    r = q("SELECT COALESCE(SUM(pnl_usd),0) AS s FROM radar_trades")
    closed = float(r[0]["s"]) if r else 0.0
    # gia batch Jupiter
    mints = [p.get("token") for p in pos if p.get("token")]
    marks = {}
    try:
        rr = requests.get("https://lite-api.jup.ag/price/v3",
                           params={"ids": ",".join(dict.fromkeys(mints))},
                           timeout=10)
        d = rr.json()
        for m in mints:
            px = (d.get(m) or {}).get("usdPrice")
            if px:
                marks[m] = float(px)
    except Exception:
        pass
    real_o, unreal = 0.0, 0.0
    for p in pos:
        try:
            real_o += float(p.get("realized", 0) or 0)
            e = float(p.get("entry", 0) or 0)
            s = float(p.get("size_usd", 0) or 0)
            rem = float(p.get("remaining", 1) or 0)
            mk = marks.get(p.get("token", ""))
            if e > 0 and s > 0 and rem > 0 and mk:
                unreal += (mk - e) / e * s * rem
        except Exception:
            pass
    return closed + real_o + unreal, f"{len(pos)} vi the mo"


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
    st.caption("Tu refresh 15s · P&L tich luy tu dau (paper, chua tru phi giao dich that).")


def tab_paper(where, params):
    d = day_filter()
    okx = q(f"SELECT {d} AS day, (pnl - fee) AS net, tag, reason "
            f"FROM okx_trades {where} ORDER BY closed_at", params)
    scalp = q(f"SELECT {d} AS day, pnl_usd AS net, wallet, reason "
              f"FROM radar_trades {where} AND plan='scalp' ORDER BY closed_at",
              params)
    holder = q(f"SELECT {d} AS day, pnl_usd AS net, wallet, reason "
               f"FROM radar_trades {where} AND plan='holder' ORDER BY closed_at",
               params)

    ptab1, ptab2 = st.tabs(["🤖 Bot OKX", "🦅 Radar meme"])

    with ptab1:
        section("Bot OKX — paper trade")
        kpi_cards("Hieu suat", okx)
        pnl_charts(daily_df(okx), "OKX paper")
        okx_positions_block()
        rokx = q(f"""SELECT closed_at, inst AS symbol, side, tag,
                            (pnl - fee) AS net, reason
                     FROM okx_trades {where}
                     ORDER BY closed_at DESC LIMIT 50""", params)
        trades_table(rokx,
                     {"closed_at": "Dong luc", "symbol": "Cap",
                      "side": "Chieu", "tag": "Loai", "net": "P&L rong (U)",
                      "reason": "Ly do"},
                     "50 lenh OKX gan nhat")

    with ptab2:
        section("Radar meme Solana — paper")
        radar_equity_realtime()
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

        radar_positions_block()

        rrc = q(f"""SELECT closed_at, symbol, plan, pnl_usd AS net, reason
                    FROM radar_trades {where}
                    ORDER BY closed_at DESC LIMIT 50""", params)
        trades_table(rrc,
                     {"closed_at": "Dong luc", "symbol": "Symbol",
                      "plan": "Plan", "net": "P&L rong (U)",
                      "reason": "Ly do"},
                     "50 lenh radar gan nhat")


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

    tab_live_, tab_paper_ = st.tabs(["🔴 LIVE — Tien that", "📄 PAPER — Chay thu"])
    with tab_live_:
        tab_live(where, params)
    with tab_paper_:
        tab_paper(where, params)

    st.divider()
    st.markdown('<div class="small-note">Vi the & equity live tu refresh '
                'rieng (5s/15s) · Cac chi so khac refresh khi tai trang · '
                'So lieu paper chi de doi chung, khong phai ket qua tien that.'
                '</div>', unsafe_allow_html=True)


main()
