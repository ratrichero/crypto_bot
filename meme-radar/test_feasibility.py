#!/usr/bin/env python3
"""Test kha thi paper (feasibility.py) + paper engine radar (open_paper /
paper_decide / manage_positions / handle_sells) + parse sell/wallet price.

Chay: python test_feasibility.py   (khong can mang)
radar.py duoc chep sang thu muc tam (BASE rieng) de jsonl/log khong ghi
vao repo."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import feasibility as feas  # noqa: E402
from dexscreener import pick_pair  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def approx(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


F = feas.params({})


def test_feasibility_math():
    print("== feasibility: loc thanh khoan / slippage / exit ==")
    check("mac dinh Musev", F["min_liquidity_mult"] == 20
          and F["max_slippage_pct"] == 15 and F["min_exit_volume_mult"] == 5
          and F["slippage_coef"] == 50 and F["max_exit_waits"] == 3, F)
    check("cfg ghi de", feas.params({"min_liquidity_mult": 10})
          ["min_liquidity_mult"] == 10)
    check("liq $2000 size $100 -> loc (can $2000? = dung bien -> ok)",
          feas.liquidity_ok(100, 2000, F) == (True, None))
    check("liq $1999 size $100 -> skipped_low_liquidity",
          feas.liquidity_ok(100, 1999, F) == (False, "skipped_low_liquidity"))
    check("khong co liq -> skipped_no_liquidity_data (fail closed)",
          feas.liquidity_ok(100, None, F)[1] == "skipped_no_liquidity_data")
    check("liq 0 / NaN -> khong co so lieu",
          feas.liquidity_ok(100, 0, F)[0] is False
          and feas.liquidity_ok(100, float("nan"), F)[0] is False)
    check("vi du Musev: $100 / $2000 -> 2.5%",
          approx(feas.slippage_pct(100, 2000, F), 2.5))
    check("tran 15%", approx(feas.slippage_pct(100, 200, F), 15.0))
    check("thieu liq -> slippage toi da",
          approx(feas.slippage_pct(100, None, F), 15.0))
    check("apply_buy/apply_sell", approx(feas.apply_buy(1.0, 2.5), 1.025)
          and approx(feas.apply_sell(1.0, 2.5), 0.975))
    check("exit: vol5m $400 < leg $100*5 -> constrained",
          feas.exit_constrained(100, 400, F) is True)
    check("exit: vol5m $500 -> ok", feas.exit_constrained(100, 500, F) is False)
    check("exit: khong co volume -> khong che",
          feas.exit_constrained(100, None, F) is False)
    r = feas.replay_trade({"size_usd": 100, "legs": [{"frac": 1.0,
                                                      "ret": 0.5}]},
                          4000, F)
    # slip vao 1.25%, ra $150/$4000*50 = 1.875%
    exp = 100 * (1.5 * (1 - 0.01875) / 1.0125 - 1)
    check("replay: pnl_raw 50, pnl_adj theo slippage", approx(r["pnl_raw"], 50)
          and approx(r["pnl_adj"], exp), r)
    r = feas.replay_trade({"size_usd": 100, "legs": [{"frac": 1, "ret": 1}]},
                          1000, F)
    check("replay: liq thap -> skipped, pnl_adj 0", r["skipped"]
          and r["pnl_adj"] == 0)


def test_pick_pair_volume():
    print("== dexscreener.pick_pair: volume m5/h24 ==")
    mint = "MINT111"
    rows = [{"chainId": "solana", "pairAddress": "P1",
             "baseToken": {"address": mint, "symbol": "m"},
             "quoteToken": {"address": "So11111111111111111111111111111111111111112",
                            "symbol": "SOL"},
             "priceUsd": "0.01", "liquidity": {"usd": 50000},
             "volume": {"m5": 1234.5, "h24": "99999"}, "marketCap": 1e5}]
    p = pick_pair(rows, mint)
    check("volume_m5/h24", p and p["volume_m5"] == 1234.5
          and p["volume_h24"] == 99999.0, p)
    rows[0].pop("volume")
    p = pick_pair(rows, mint)
    check("thieu volume -> None", p["volume_m5"] is None, p)


def test_parse_sell_and_wallet_price():
    print("== helius: sold_frac + gia vi nguon ==")
    from sources import helius
    W, M = "WALLET", "MINTX"
    tx = {"meta": {"err": None, "fee": 5000,
                   "preBalances": [1_000_000_000], "postBalances": [1_500_000_000],
                   "preTokenBalances": [{"owner": W, "mint": M,
                                         "uiTokenAmount": {"uiAmount": 1000}}],
                   "postTokenBalances": [{"owner": W, "mint": M,
                                          "uiTokenAmount": {"uiAmount": 250}}]},
          "transaction": {"message": {"accountKeys": [{"pubkey": W}]}}}
    sl = helius.parse_sell(tx, W, 0.05)
    check("sold_frac = 750/1000", sl and approx(sl.get("sold_frac"), 0.75), sl)
    check("min_sell_sol: 0.5 SOL < 1.0 -> None",
          helius.parse_sell(tx, W, 1.0) is None)
    closed = json.loads(json.dumps(tx))
    closed["meta"]["postTokenBalances"] = []
    sl = helius.parse_sell(closed, W, 0.05)
    check("ban sach + dong ATA -> sold_frac 1.0", sl and sl["sold_frac"] == 1.0
          and sl["tokens"] == 1000 and sl["mint"] == M, sl)
    check("wallet_price_usd = sol*solusd/tokens",
          approx(helius.wallet_price_usd({"tokens": 1000, "sol_spent": 2},
                                         150), 0.3))
    check("tokens 0 -> None",
          helius.wallet_price_usd({"tokens": 0, "sol_spent": 2}, 150) is None)


def load_radar(tmp, cfg_extra=None):
    cfg = json.load(open(os.path.join(HERE, "config.example.json")))
    cfg.update({"size_by_mcap": [[0, 100]], "max_paper_positions": 5,
                "horizons_min": [5], "min_sol_spent": 0.3,
                "helius_api_key": "x"})
    cfg.update(cfg_extra or {})
    json.dump(cfg, open(os.path.join(tmp, "config.json"), "w"))
    shutil.copy(os.path.join(HERE, "radar.py"), os.path.join(tmp, "radar.py"))
    spec = importlib.util.spec_from_file_location(
        "radar_t" + str(abs(hash(tmp))), os.path.join(tmp, "radar.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log = lambda m: None
    return mod


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    return [json.loads(x) for x in open(path) if x.strip()]


def new_state():
    return {"paper": [], "paper_holder": [], "alerted_clusters": [],
            "closed_keys": [], "sell_events": []}


def sig(token="TOK", wallet="W1", px=1.0, liq=10000, tid="t1"):
    return {"token": token, "symbol": token, "wallet": wallet, "tid": tid,
            "price_usd": px, "price_now": px, "mcap_usd": 100000,
            "liquidity_usd": liq, "amount_usd": 500}


def test_open_paper():
    print("== radar.open_paper: loc + slippage vao ==")
    tmp = tempfile.mkdtemp()
    try:
        r = load_radar(tmp)
        st = new_state()
        r.open_paper(st, sig(liq=1500), 1000)
        ents = read_jsonl(r.ENTRIES_P)
        check("liq $1500 < $2000 -> khong mo", st["paper"] == [])
        check("ghi skipped_low_liquidity", ents and ents[-1]["decision"]
              == "skipped_low_liquidity" and ents[-1]["need_usd"] == 2000,
              ents)
        r.open_paper(st, sig(liq=None), 1000, info_fn=lambda t: {})
        check("khong biet liq -> skipped_no_liquidity_data",
              read_jsonl(r.ENTRIES_P)[-1]["decision"]
              == "skipped_no_liquidity_data" and st["paper"] == [])
        r.open_paper(st, sig(liq=None), 1000,
                     info_fn=lambda t: {"liquidity_usd": 4000})
        p = st["paper"][0]
        check("lay liq tu DexScreener khi signal thieu", p["liquidity_usd"]
              == 4000)
        check("slippage vao 1.25%, entry_eff", approx(p["entry_slip_pct"],
                                                      1.25)
              and approx(p["entry_eff"], 1.0125) and p["entry"] == 1.0, p)
        check("ghi opened", read_jsonl(r.ENTRIES_P)[-1]["decision"]
              == "opened")
        r.open_paper(st, sig(liq=9e9, tid="t2"), 1001)
        check("dedup token dang mo", len(st["paper"]) == 1)
    finally:
        shutil.rmtree(tmp)
    tmp = tempfile.mkdtemp()
    try:
        r = load_radar(tmp, {"feasibility_enabled": False})
        st = new_state()
        r.open_paper(st, sig(liq=10), 1000)
        check("feasibility_enabled=false -> mo nhu cu, slip 0",
              len(st["paper"]) == 1 and st["paper"][0]["entry_slip_pct"] == 0)
    finally:
        shutil.rmtree(tmp)


def test_manage_positions():
    print("== radar.manage_positions: exit kha thi ==")
    tmp = tempfile.mkdtemp()
    try:
        r = load_radar(tmp)
        P = r.build_params(main=True)
        st = new_state()
        r.open_paper(st, sig(liq=4000), 1000)
        # SL: gia 0.7, volume du -> ban het, slip theo leg $70 / liq 4000
        info = {"price": 0.7, "liquidity_usd": 4000, "volume_m5": 10000}
        r.manage_positions(st, P, 1060, price_fn=lambda t: info)
        tr = read_jsonl(r.TRADES_P)
        check("SL dong", st["paper"] == [] and tr and tr[-1]["reason"]
              == "stop_loss", tr)
        t = tr[-1]
        slip_out = 70 / 4000 * 50
        exp = 0.7 * (1 - slip_out / 100) / 1.0125 - 1
        check("final_ret goc -30%", approx(t["final_ret"], -0.3))
        check("final_ret_adj tinh slippage vao+ra",
              approx(t["final_ret_adj"], round(exp, 4)), t)
        check("leg co slip_pct/ret_adj", approx(t["legs"][0]["slip_pct"],
                                                round(slip_out, 3)))
        check("khong bi rang buoc", t["exit_constrained"] is False
              and t["exit_failed_liquidity"] is False)

        # Exit bi rang buoc: volume 5m qua nho -> cho 3 vong, vong 4 thoat
        st = new_state()
        r.open_paper(st, sig(token="T2", liq=4000), 2000)
        thin = {"price": 0.7, "liquidity_usd": 4000, "volume_m5": 100}
        for i in range(3):
            r.manage_positions(st, P, 2060 + i, price_fn=lambda t: thin)
        p = st["paper"][0] if st["paper"] else {}
        check("3 vong exit_constrained -> van giu", p.get("exit_waits") == 3
              and p.get("remaining") == 1.0 and p.get("legs") == [], p)
        r.manage_positions(st, P, 2070, price_fn=lambda t: thin)
        t = read_jsonl(r.TRADES_P)[-1]
        check("vong 4 -> thoat exit_failed_liquidity",
              st["paper"] == [] and t["exit_failed_liquidity"]
              and t["exit_waits_total"] == 4, t)
        check("slippage toi da 15%", t["legs"][0]["slip_pct"] == 15.0)
        exp = 0.7 * 0.85 / 1.0125 - 1
        check("ret_adj voi slip 15%", approx(t["final_ret_adj"],
                                             round(exp, 4)), t)

        # Rang buoc roi volume hoi phuc -> thoat binh thuong, reset waits
        st = new_state()
        r.open_paper(st, sig(token="T3", liq=4000), 3000)
        r.manage_positions(st, P, 3060, price_fn=lambda t: thin)
        ok = {"price": 0.7, "liquidity_usd": 4000, "volume_m5": 1e6}
        r.manage_positions(st, P, 3061, price_fn=lambda t: ok)
        t = read_jsonl(r.TRADES_P)[-1]
        check("volume hoi phuc -> thoat thuong, co danh dau constrained",
              t["token"] == "T3" and t["exit_constrained"]
              and not t["exit_failed_liquidity"]
              and t["legs"][0]["slip_pct"] < 15, t)

        # Khong co action -> khong dung toi volume
        st = new_state()
        r.open_paper(st, sig(token="T4", liq=4000), 4000)
        r.manage_positions(st, P, 4060,
                           price_fn=lambda t: {"price": 1.05,
                                               "volume_m5": 0})
        check("khong co lenh ban -> khong exit_constrained",
              not st["paper"][0].get("exit_constrained"))
        # TP1 ban 34%: leg nho hon -> chi can volume theo leg
        r.manage_positions(st, P, 4070,
                           price_fn=lambda t: {"price": 1.6,
                                               "liquidity_usd": 4000,
                                               "volume_m5": 300})
        p = st["paper"][0]
        check("TP1 leg $54 * 5 = $272 < vol $300 -> ban",
              p["tp1"] and approx(p["remaining"], 0.66), p)
    finally:
        shutil.rmtree(tmp)


def test_paper_decide_parity():
    print("== radar.paper_decide dong bo strategy.decide_exits ==")
    import strategy
    tmp = tempfile.mkdtemp()
    try:
        r = load_radar(tmp)
        P = r.build_params(main=True)
        cases = [({}, 0.7, 60), ({}, 1.6, 60), ({"tp1": True}, 2.1, 60),
                 ({"smart_exit": True}, 1.0, 60),
                 ({"copy_exit": True}, 1.0, 60),
                 ({"copy_exit": True}, 0.6, 60),
                 ({"tp1": True, "tp2": True, "peak": 3.0}, 2.0, 60),
                 ({}, 1.3, 481 * 60), ({}, 1.05, 481 * 60)]
        for flags, px, now in cases:
            p = {"entry": 1.0, "peak": 1.0, "opened_at": 0, "remaining": 1.0,
                 **flags}
            acts, reason = r.paper_decide(dict(p), P, px, now)
            legs, sreason = strategy.decide_exits(dict(p), px, now, P)
            whys = [w for _, w in acts]
            swhys = [w for _, w in legs]
            fr = [round(f, 4) for f, _ in acts]
            sfr = [round(f, 4) for f, _ in legs]
            check(f"{flags} px={px}: {whys} == {swhys}", whys == swhys
                  and fr == sfr and reason == sreason,
                  (fr, sfr, reason, sreason))
    finally:
        shutil.rmtree(tmp)


def test_handle_sells():
    print("== radar.handle_sells: wallet_sell + copy exit + dedup ==")
    tmp = tempfile.mkdtemp()
    try:
        r = load_radar(tmp)
        st = new_state()
        r.open_paper(st, sig(token="TOK", wallet="W1", liq=4000), 1000)
        small = {"tid": "s1", "wallet": "W1", "token": "TOK", "ts": 1100,
                 "sol_amount": 0.06, "sold_frac": 0.3, "side": "sell"}
        r.handle_sells(st, [small, dict(small)], 1100)
        al = [a for a in read_jsonl(r.ALERT_P) if a["type"] == "wallet_sell"]
        check("1 alert wallet_sell (dedup tid)", len(al) == 1
              and al[0]["sold_frac"] == 0.3 and al[0]["tid"] == "s1", al)
        check("lenh nho khong vao sell_events", st["sell_events"] == [])
        p = st["paper"][0]
        check("30% < 50% -> chua copy_exit", not p.get("copy_exit")
              and approx(p["src_remaining"], 0.7))
        r.handle_sells(st, [dict(small, tid="s2", sold_frac=0.4,
                                 sol_amount=0.5)], 1110)
        check("cong don 1-0.7*0.6 = 58% -> copy_exit", p.get("copy_exit"), p)
        check("lenh >= min_sol -> sell_events", len(st["sell_events"]) == 1)
        P = r.build_params(main=True)
        r.manage_positions(st, P, 1120,
                           price_fn=lambda t: {"price": 1.1,
                                               "liquidity_usd": 4000,
                                               "volume_m5": 1e6})
        t = read_jsonl(r.TRADES_P)[-1]
        check("paper thoat COPY_EXIT", t["reason"] == "copy_exit", t)
        # vi khac / lenh ban truoc khi mo -> khong tinh
        st = new_state()
        r.open_paper(st, sig(token="TOK", wallet="W1", liq=4000), 5000)
        r.handle_sells(st, [{"tid": "x1", "wallet": "W2", "token": "TOK",
                             "ts": 5010, "sol_amount": 1, "sold_frac": 1.0},
                            {"tid": "x2", "wallet": "W1", "token": "TOK",
                             "ts": 4000, "sol_amount": 1, "sold_frac": 1.0}],
                       5010)
        check("vi khac / lenh cu -> khong copy_exit",
              not st["paper"][0].get("copy_exit"))
        r.handle_sells(st, [{"tid": "x3", "wallet": "W1", "token": "TOK",
                             "ts": 5020, "sol_amount": 1}], 5020)
        check("khong co sold_frac (nguon cu) -> khong alert wallet_sell",
              not any(a.get("tid") == "x3" for a in read_jsonl(r.ALERT_P)))
    finally:
        shutil.rmtree(tmp)


def test_enrich_ws():
    print("== radar.enrich_ws: gia vi nguon + liquidity ==")
    tmp = tempfile.mkdtemp()
    try:
        r = load_radar(tmp)
        r.ds_info = lambda a: {"price": 0.01, "symbol": "abc", "mcap": 5e4,
                               "liquidity_usd": 8000}
        s = r.enrich_ws({"tid": "t", "wallet": "W", "token": "M",
                         "sol_spent": 1.0, "tokens": 15000, "ts": 1}, 150.0)
        check("wallet_price_usd = 150/15000", approx(s["wallet_price_usd"],
                                                    0.01), s)
        check("liquidity + symbol upper", s["liquidity_usd"] == 8000
              and s["symbol"] == "ABC")
        s = r.enrich_ws({"tid": "t", "wallet": "W", "token": "M",
                         "sol_spent": 1.0, "ts": 1}, 150.0)
        check("thieu tokens -> wallet_price None", s["wallet_price_usd"]
              is None)
    finally:
        shutil.rmtree(tmp)


def test_report():
    print("== report: muc kha thi + do nhay ==")
    import report
    trades = [
        {"feasibility": True, "size_usd": 100, "final_ret": 0.5,
         "final_ret_adj": 0.45, "realized_adj_usd": 45.0,
         "entry_slip_pct": 2.0, "exit_constrained": True,
         "exit_failed_liquidity": False,
         "legs": [{"frac": 1.0, "ret": 0.5, "slip_pct": 3.0}],
         "liquidity_usd": 5000},
        {"feasibility": True, "size_usd": 100, "final_ret": -0.3,
         "final_ret_adj": -0.42, "realized_adj_usd": -42.0,
         "entry_slip_pct": 1.0, "exit_constrained": True,
         "exit_failed_liquidity": True,
         "legs": [{"frac": 1.0, "ret": -0.3, "slip_pct": 15.0}]},
        {"size_usd": 100, "final_ret": 1.0,
         "legs": [{"frac": 1.0, "ret": 1.0}]},
    ]
    entries = [{"decision": "opened"}, {"decision": "opened"},
               {"decision": "skipped_low_liquidity"},
               {"decision": "skipped_no_liquidity_data"}]
    txt = "\n".join(report.feasibility_lines(trades, entries, F))
    check("ty le bi loc 50%", "bi loc 2 (50%)" in txt, txt)
    check("P&L goc 20 -> sau slippage 3", "P&L goc +20.0U -> sau slippage "
          "+3.0U" in txt, txt)
    check("slippage TB vao 1.50% ra 9.00%", "vao 1.50%, ra 9.00%" in txt, txt)
    check("dem constrained / failed", ": 2 lenh | exit_failed_liquidity"
          in txt and "3 vong): 1 lenh" in txt, txt)
    sens = "\n".join(report.sensitivity_lines(trades, F))
    check("bang do nhay co cac muc liq", "$       2,000" in sens
          and "$     100,000" in sens, sens)
    check("liq $2000 < $100*20? = bien -> giu 3/3", "3/3" in sens, sens)
    check("min_liquidity_mult rows", "x10" in sens and "x40" in sens, sens)
    check("khong co lenh moi -> goi y --sensitivity", "--sensitivity" in
          "\n".join(report.feasibility_lines(trades[2:], [], F)))


if __name__ == "__main__":
    test_feasibility_math()
    test_pick_pair_volume()
    test_parse_sell_and_wallet_price()
    test_open_paper()
    test_manage_positions()
    test_paper_decide_parity()
    test_handle_sells()
    test_enrich_ws()
    test_report()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
