"""Dashboard theo doi bot Binance LIVE + OKX paper + radar meme Solana. Tieng Viet.

Chay:  DATABASE_URL=postgres://... streamlit run app.py
Tu refresh 60s. Vi the dang mo doc truc tiep tu state file cua bot.
"""
import os
import time

import pandas as pd
import plotly.express as px
import psycopg
import psycopg.rows
import streamlit as st

st.set_page_config(page_title="Crypto Bots Dashboard", layout="wide")

DB = os.environ.get("DATABASE_URL")
BINANCE_STATE = os.environ.get("BINANCE_STATE",
                               "/home/ubuntu/muse_bot/binance-bot/state.json")
OKX_STATE = os.environ.get("OKX_STATE",
                           os.path.expanduser("~/workspace/trading-bot/state.json"))
RADAR_STATE = os.environ.get("RADAR_STATE",
                             os.path.expanduser("~/workspace/meme-radar/radar_state.json"))
WALLETS_JSON = os.environ.get("WALLETS_JSON",
                              os.path.expanduser("~/workspace/meme-radar/wallets.json"))

TZ = "Asia/Ho_Chi_Minh"


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


def kpi_row(title, rows):
    pnl, wr, pf, exp, n = metrics(rows)
    st.subheader(title)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("P&L ròng", f"{pnl:+.1f} U")
    c2.metric("Winrate", f"{wr:.0%}")
    c3.metric("Profit factor", f"{pf:.2f}")
    c4.metric("Kỳ vọng/lệnh", f"{exp:+.2f} U")
    c5.metric("Số lệnh", f"{n}")


def daily_df(rows):
    if not rows:
        return pd.DataFrame(columns=["day", "net"])
    df = pd.DataFrame(rows)
    df["day"] = pd.to_datetime(df["day"]).dt.date
    g = df.groupby("day", as_index=False)["net"].sum().sort_values("day")
    g["cum"] = g["net"].cumsum()
    return g


def main():
    st.title("📊 Crypto Bots Dashboard")
    if not DB:
        st.error("Chua dat DATABASE_URL. Vi du: "
                 "DATABASE_URL=postgres://user:pass@localhost:5432/cryptobots "
                 "streamlit run app.py")
        st.stop()

    filt = st.radio("Khoảng thời gian", ["7 ngày", "30 ngày", "Tất cả"],
                    horizontal=True, key="rng")
    days = {"7 ngày": 7, "30 ngày": 30, "Tất cả": None}[filt]

    def _where(n=1):
        if days is None:
            return "", ()
        return ("WHERE closed_at >= now() - make_interval(days => %s)", (days,) * n)

    where, params = _where(1)

    d = day_filter()

    # ================= BINANCE LIVE (tien that) =================
    st.header("🔴 Binance LIVE — tiền thật")
    bn = q(f"SELECT {d} AS day, pnl AS net, tag, reason, live, dry "
           f"FROM binance_trades {where} ORDER BY closed_at", params)
    pnl, wr, pf, exp, n = metrics(bn)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("P&L ròng", f"{pnl:+.1f} U")
    c2.metric("Winrate", f"{wr:.0%}")
    c3.metric("Profit factor", f"{pf:.2f}")
    c4.metric("Kỳ vọng/lệnh", f"{exp:+.2f} U")
    c5.metric("Số lệnh", f"{n}")
    n_live = sum(1 for r in bn if r.get("live") and not r.get("dry"))
    n_dry = sum(1 for r in bn if r.get("dry"))
    if n:
        st.caption(f"Trong đó: {n_live} lệnh LIVE tiền thật, {n_dry} lệnh dry-run.")

    bnd = daily_df(bn)
    if not bnd.empty:
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(px.line(bnd, x="day", y="cum",
                                    title="Binance LIVE — P&L cộng dồn (U)"),
                            width="stretch")
        with c2:
            st.plotly_chart(px.bar(bnd, x="day", y="net",
                                   title="Binance LIVE — P&L từng ngày (U)"),
                            width="stretch")
    else:
        st.info("Binance LIVE chưa có lệnh đóng trong khoảng đã chọn.")

    try:
        import json
        b_st = json.load(open(BINANCE_STATE))
        bpos = b_st.get("positions", [])
        st.subheader(f"Vị thế đang mở: {len(bpos)} "
                     f"(equity {b_st.get('equity', '?')})")
        if bpos:
            st.dataframe(pd.DataFrame([{
                "ID": p.get("id"), "Symbol": p.get("symbol"),
                "Chiều": p.get("side"), "Loại": p.get("tag"),
                "Entry": p.get("entry"), "SL": p.get("sl"), "TP": p.get("tp"),
                "Notional": p.get("notional"),
            } for p in bpos]), width="stretch")
    except Exception as e:
        st.warning(f"Không đọc được {BINANCE_STATE}: {e}")

    st.subheader("50 lệnh Binance gần nhất")
    rbn = q("""SELECT closed_at, symbol, side, tag, pnl AS net, reason,
                      live, dry
               FROM binance_trades ORDER BY closed_at DESC LIMIT 50""")
    if rbn:
        dfb = pd.DataFrame(rbn)
        dfb["closed_at"] = pd.to_datetime(dfb["closed_at"]).dt.tz_convert(TZ)\
            .dt.strftime("%m-%d %H:%M")
        dfb["chế độ"] = dfb.apply(
            lambda r: "LIVE" if (r["live"] and not r["dry"]) else "dry-run",
            axis=1)
        st.dataframe(dfb[["closed_at", "symbol", "side", "tag", "net",
                           "reason", "chế độ"]].rename(columns={
            "closed_at": "Đóng lúc", "symbol": "Symbol", "side": "Chiều",
            "tag": "Loại", "net": "P&L ròng (U)", "reason": "Lý do"}),
            width="stretch")

    st.divider()

    okx = q(f"SELECT {d} AS day, (pnl - fee) AS net, tag, reason "
            f"FROM okx_trades {where} ORDER BY closed_at", params)
    scalp = q(f"SELECT {d} AS day, pnl_usd AS net, wallet "
              f"FROM radar_trades {where} AND plan='scalp' ORDER BY closed_at",
              params)
    holder = q(f"SELECT {d} AS day, pnl_usd AS net, wallet "
               f"FROM radar_trades {where} AND plan='holder' ORDER BY closed_at",
               params)

    st.header("KPI tổng")
    kpi_row("🤖 Bot OKX (paper)", okx)
    kpi_row("🦅 Radar meme — plan scalp", scalp)
    kpi_row("🦉 Radar meme — plan holder", holder)

    st.header("P&L theo ngày")
    do, ds, dh = daily_df(okx), daily_df(scalp), daily_df(holder)
    for df_, nm in ((do, "OKX"), (ds, "Scalp"), (dh, "Holder")):
        df_["system"] = nm
    alld = pd.concat([do, ds, dh], ignore_index=True)
    if not alld.empty:
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(px.line(alld, x="day", y="cum", color="system",
                                    title="P&L cộng dồn (U)"),
                            width="stretch")
        with c2:
            st.plotly_chart(px.bar(alld, x="day", y="net", color="system",
                                   barmode="group", title="P&L từng ngày (U)"),
                            width="stretch")
    else:
        st.info("Chưa có dữ liệu trong khoảng đã chọn.")

    st.header("Vị thế đang mở")
    try:
        import json
        okx_st = json.load(open(OKX_STATE))
        pos = okx_st.get("positions", [])
        st.subheader(f"OKX — {len(pos)} vị thế (equity {okx_st.get('equity', '?')})")
        if pos:
            st.dataframe(pd.DataFrame([{
                "Cặp": p.get("inst"), "Chiều": p.get("side"),
                "Loại": p.get("tag"), "Entry": p.get("entry"),
                "Notional": p.get("notional"),
            } for p in pos]), width="stretch")
    except Exception as e:
        st.warning(f"Không đọc được {OKX_STATE}: {e}")
    try:
        import json
        r_st = json.load(open(RADAR_STATE))
        for key, name in (("paper", "Radar scalp"), ("paper_holder", "Radar holder")):
            pp = r_st.get(key, [])
            st.subheader(f"{name} — {len(pp)} vị thế")
            if pp:
                st.dataframe(pd.DataFrame([{
                    "Symbol": p.get("symbol"), "Ví": (p.get("wallet") or "")[:10],
                    "Entry": p.get("entry"), "Size $": p.get("size_usd"),
                    "Còn lại": f"{(p.get('remaining', 1) or 0):.0%}",
                } for p in pp]), width="stretch")
    except Exception as e:
        st.warning(f"Không đọc được {RADAR_STATE}: {e}")

    st.header("Xếp hạng ví radar (theo P&L paper)")
    wl = q(f"""SELECT t.wallet, w.label, COUNT(*) AS n,
                      SUM(t.pnl_usd) AS pnl,
                      AVG(CASE WHEN t.pnl_usd > 0 THEN 1.0 ELSE 0.0 END) AS wr
               FROM radar_trades t LEFT JOIN wallets w ON w.address = t.wallet
               {where} GROUP BY t.wallet, w.label
               ORDER BY pnl DESC""", params)
    if wl:
        st.dataframe(pd.DataFrame([{
            "Ví": (r["wallet"] or "")[:12],
            "Label": r["label"] or "",
            "Lệnh": r["n"], "P&L (U)": round(r["pnl"] or 0, 1),
            "Winrate": f"{(r['wr'] or 0):.0%}",
        } for r in wl]), width="stretch")

    st.header("50 lệnh đóng gần nhất")
    w2, p2 = _where(2)
    recent = q(f"""(SELECT closed_at, 'OKX' AS system, inst AS symbol, tag AS kind,
                          side, (pnl - fee) AS net, reason
                   FROM okx_trades {w2})
                  UNION ALL
                  (SELECT closed_at, 'radar-' || plan AS system, symbol,
                          plan AS kind, '' AS side, pnl_usd AS net, reason
                   FROM radar_trades {w2})
                  ORDER BY closed_at DESC LIMIT 50""", p2)
    if recent:
        df = pd.DataFrame(recent)
        df["closed_at"] = pd.to_datetime(df["closed_at"]).dt.tz_convert(TZ)\
            .dt.strftime("%m-%d %H:%M")
        st.dataframe(df.rename(columns={
            "closed_at": "Đóng lúc", "system": "Hệ thống", "symbol": "Symbol",
            "kind": "Loại", "side": "Chiều", "net": "P&L ròng (U)",
            "reason": "Lý do"}), width="stretch")

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now7 = _dt.now(_tz(_td(hours=7))).strftime("%H:%M:%S")
    st.caption(f"Cập nhật: {now7} (+07) — tự refresh 60s")


try:
    frag = st.fragment(run_every=60)
except Exception:
    frag = lambda f: f  # noqa: E731

frag(main)()
