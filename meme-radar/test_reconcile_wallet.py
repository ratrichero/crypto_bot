#!/usr/bin/env python3
"""Test reconcile_wallet (thuan, khong goi mang)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reconcile_wallet as rw  # noqa: E402

PASS = FAIL = 0


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok   " + name)
    else:
        FAIL += 1
        print("FAIL " + name + ("  -> %s" % (info,) if info else ""))


W = "Wallet1111111111111111111111111111111111111"
M = "MintART11111111111111111111111111111111111"
L = rw.LAMPORTS


def tb(idx, mint, owner, amount):
    return {"accountIndex": idx, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount)}}


def mk_tx(sol_pre, sol_post, fee, tok_pre=None, tok_post=None,
          ata_pre=0, ata_post=2039280, err=None, fee_payer=True,
          parsed=True):
    """Vi o index 0 (fee payer), ATA cua vi o index 1, wSOL tam o index 2."""
    keys = [W, "ATA1", "WSOLTMP", "Program"]
    if not fee_payer:
        keys = ["Other", "ATA1", "WSOLTMP", W]
    if parsed:
        keys = [{"pubkey": k, "signer": i == 0} for i, k in enumerate(keys)]
    wi = 0 if fee_payer else 3
    pre = [0, ata_pre, 0, 1]
    post = [0, ata_post, 0, 1]
    pre[wi], post[wi] = sol_pre, sol_post
    if not fee_payer:
        pre[0], post[0] = 10 * L, 10 * L - fee
    pre_tb, post_tb = [], []
    if tok_pre is not None:
        pre_tb.append(tb(1, M, W, tok_pre))
    if tok_post is not None:
        post_tb.append(tb(1, M, W, tok_post))
    # wSOL tam cua Jupiter: mo va dong trong cung tx -> bo qua
    post_tb.append(tb(2, rw.SOL_MINT, W, 0))
    return {"blockTime": 1_760_000_000,
            "transaction": {"message": {"accountKeys": keys}},
            "meta": {"err": err, "fee": fee, "preBalances": pre,
                     "postBalances": post, "preTokenBalances": pre_tb,
                     "postTokenBalances": post_tb}}


# ---- parse_tx: mua lan dau (tao ATA -> rent)
buy = mk_tx(1_000_000_000, 1_000_000_000 - 50_000_000 - 2_039_280 - 105_000,
            105_000, tok_pre=None, tok_post=4_000_000, ata_pre=0,
            ata_post=2_039_280)
p = rw.parse_tx(buy, W, M)
check("buy: sol_delta gom swap + rent + phi",
      p["sol_delta"] == -(50_000_000 + 2_039_280 + 105_000), p)
check("buy: rent_open = lamports ATA moi", p["rent_open"] == 2_039_280, p)
check("buy: token_delta", p["token_delta"] == 4_000_000, p)
check("buy: phi + fee_payer", p["fee"] == 105_000 and p["fee_payer"])
check("buy: bo qua wSOL tam", rw.SOL_MINT not in p["mints"])

# ---- sell toan bo (ATA khong dong)
sell = mk_tx(900_000_000, 900_000_000 + 60_000_000 - 95_000, 95_000,
             tok_pre=4_000_000, tok_post=0, ata_pre=2_039_280,
             ata_post=2_039_280)
s = rw.parse_tx(sell, W, M)
check("sell: sol_delta = nhan - phi", s["sol_delta"] == 60_000_000 - 95_000)
check("sell: token_delta am", s["token_delta"] == -4_000_000)
check("sell: khong rent", s["rent_open"] == 0 and s["rent_close"] == 0)

# ---- encoding json (khong parsed) + v0 loadedAddresses
raw = mk_tx(5, 3, 2, parsed=False)
raw["transaction"]["message"]["accountKeys"] = [W, "ATA1"]
raw["meta"]["loadedAddresses"] = {"writable": ["WSOLTMP"],
                                  "readonly": ["Program"]}
check("json encoding + loadedAddresses", rw.parse_tx(raw, W)["sol_delta"]
      == -2)
check("tx None -> None", rw.parse_tx(None, W) is None)
check("vi khong tra phi -> fee_payer False",
      not rw.parse_tx(mk_tx(5, 6, 5000, fee_payer=False), W)["fee_payer"])

# ---- reconcile_trade: bot ghi DUNG -> chain == bot, truoc phi = + phi
SOL = 200.0
cost_lam = 50_000_000 + 105_000          # bot: (spent - rent)
cost_usd = cost_lam / L * SOL            # 10.021
proc_lam = 60_000_000 - 95_000
proc_usd = proc_lam / L * SOL
trade = {"token": M, "symbol": "ART", "closed_at": 1_760_000_100,
         "reason": "copy_exit", "mode": "live", "entry_tx": "B1",
         "size_usd": round(cost_usd, 4),
         "realized_usd": round(proc_usd - cost_usd, 4),
         "legs": [{"frac": 1.0, "tx": "S1", "proceeds_usd": round(proc_usd, 4),
                   "pnl_usd": round(proc_usd - cost_usd, 4)}]}
txs = {"B1": buy, "S1": sell}
r = rw.reconcile_trade(trade, txs, W)
check("trade dung: chain ~ bot", abs(r["chain_usd"] - r["bot_usd"]) < 0.01,
      r)
check("trade dung: khong co co canh bao", r["flags"] == [], r["flags"])
check("phi = (buy fee + sell fee) * gia SOL",
      abs(r["fee_usd"] - (200_000 / L * SOL)) < 1e-6, r["fee_usd"])
check("truoc phi = chain + phi (~ app vi)",
      abs(r["gross_usd"] - (r["chain_usd"] + r["fee_usd"])) < 1e-9)
check("suy ra gia SOL tu ban ghi bot", abs(r["sol_usd"] - SOL) < 0.01,
      r["sol_usd"])
check("rent tach rieng (khong vao P&L)", abs(r["rent_sol"] - 0.00203928)
      < 1e-9)

# ---- bot ghi sai (vd. leg uoc tinh cao hon thuc te) -> BOT_LECH_CHAIN
bad = json.loads(json.dumps(trade))
bad["realized_usd"] = trade["realized_usd"] + 1.0
r2 = rw.reconcile_trade(bad, txs, W)
check("bot lech > $0.05 -> BOT_LECH_CHAIN", "BOT_LECH_CHAIN" in r2["flags"]
      and abs(r2["diff_usd"] - 1.0) < 0.01, r2)

# ---- leg khong tx / uoc tinh / wallet_empty
t3 = json.loads(json.dumps(trade))
t3["legs"] = [{"frac": 0.5, "tx": "S1", "proceeds_usd": proc_usd},
              {"frac": 0.5, "tx": None, "estimated": True,
               "proceeds_usd": 5.0},
              {"frac": 0.0, "tx": None, "note": "wallet_empty"}]
r3 = rw.reconcile_trade(t3, txs, W)
check("leg uoc tinh -> co", "leg_uoc_tinh_khong_tx" in r3["flags"])
check("leg wallet_empty -> co", "leg_wallet_empty" in r3["flags"])

# ---- entry tx loi / thieu
t4 = dict(trade, entry_tx="B404")
r4 = rw.reconcile_trade(t4, txs, W)
check("khong tim thay entry tx", "khong_tim_thay_entry_tx" in r4["flags"]
      and r4["chain_usd"] is None)
failed = mk_tx(1, 1, 5000, err={"InstructionError": [2, "x"]})
r5 = rw.reconcile_trade(dict(trade, entry_tx="BF"), {"BF": failed,
                                                    "S1": sell}, W)
check("entry tx loi on-chain", "entry_tx_LOI_onchain" in r5["flags"])

# ---- con token chua ban
partial = mk_tx(900_000_000, 930_000_000, 5000, tok_pre=4_000_000,
                tok_post=2_000_000, ata_pre=2_039_280, ata_post=2_039_280)
r6 = rw.reconcile_trade(trade, {"B1": buy, "S1": partial}, W)
check("con 50% token -> co", any(f.startswith("con_token_chua_ban")
                                 for f in r6["flags"]), r6["flags"])

# ---- khong suy ra duoc gia SOL -> dung --sol-usd
t7 = dict(trade, size_usd=0, legs=[{"frac": 1, "tx": "S1",
                                    "proceeds_usd": 0}])
r7 = rw.reconcile_trade(t7, txs, W, sol_usd_fallback=150.0)
check("fallback gia SOL", r7["sol_usd"] == 150.0 and r7["chain_usd"]
      is not None)
r8 = rw.reconcile_trade(t7, txs, W)
check("khong co gia -> co canh bao", "khong_co_gia_SOL(--sol-usd)"
      in r8["flags"])

# ---- aggregate
ag = rw.aggregate([r, r2, r4])
check("aggregate theo symbol", ag["ART"]["n"] == 3
      and not ag["ART"]["complete"] and "BOT_LECH_CHAIN" in ag["ART"]["flags"])

# ---- classify_untracked
check("untracked: tx loi", rw.classify_untracked(rw.parse_tx(failed, W))
      == "tx_loi(mat_phi)")
rent_back = mk_tx(1_000, 2_040_280, 5000, tok_pre=0, tok_post=None,
                  ata_pre=2_039_280, ata_post=0)
check("untracked: thu hoi rent", rw.classify_untracked(
    rw.parse_tx(rent_back, W)) == "thu_hoi_rent/nhan_SOL")
check("untracked: mua ngoai bot", rw.classify_untracked(
    rw.parse_tx(buy, W)) == "MUA_ngoai_bot")
check("untracked: ban ngoai bot", rw.classify_untracked(
    rw.parse_tx(sell, W)) == "BAN_ngoai_bot")
check("rent_close khi dong ATA", rw.parse_tx(rent_back, W, M)["rent_close"]
      == 2_039_280)

# ---- config: doc .env goc, khong lay khoa khac
with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, ".env"), "w") as f:
        f.write("SOLANA_PRIVATE_KEY=secret\nexport HELIUS_API_KEY='abc'\n"
                "SOL_WALLET=W1\nSOL_WALLET=W2\n")
    e = rw.read_env_file(os.path.join(d, ".env"),
                         {"HELIUS_API_KEY", "SOL_WALLET"})
    check(".env: chi lay khoa can, dong cuoi thang",
          e == {"HELIUS_API_KEY": "abc", "SOL_WALLET": "W2"}, e)
    key, wallet, src = rw.resolve_settings(env={}, repo=d)
    check("resolve tu .env", key == "abc" and wallet == "W2"
          and src == "HELIUS_API_KEY")
    key, wallet, _ = rw.resolve_settings(env={"SOL_WALLET": "WE"}, repo=d)
    check("env tien trinh thang .env", wallet == "WE")
    os.makedirs(os.path.join(d, "meme-radar"))
    with open(os.path.join(d, "meme-radar", ".helius_key"), "w") as f:
        f.write("filekey\n")
    with open(os.path.join(d, "meme-radar", "config.live.json"), "w") as f:
        json.dump({"wallet_address": "WC"}, f)
    os.remove(os.path.join(d, ".env"))
    key, wallet, src = rw.resolve_settings(env={}, repo=d)
    check("fallback .helius_key + config.live.json",
          key == "filekey" and wallet == "WC" and src.endswith(".helius_key"))
    # load_trades: chi live + trong cua so
    tp = os.path.join(d, "t.jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"mode": "live", "closed_at": 100}) + "\n")
        f.write(json.dumps({"mode": "dry", "closed_at": 200}) + "\n")
        f.write("rac\n")
        f.write(json.dumps({"mode": "live", "closed_at": 50}) + "\n")
    check("load_trades loc live + since", len(rw.load_trades(tp, 60)) == 1)

# ---- main() end-to-end voi RPC gia (khong mang)
import contextlib  # noqa: E402
import io  # noqa: E402
import time as _time  # noqa: E402


class FakeRpc:
    def __init__(self, url, **kw):
        self.url = url

    def get_tx(self, sig):
        return {"B1": buy, "S1": sell, "F1": failed,
                "R1": rent_back}.get(sig)

    def signatures(self, wallet, since, limit=1000):
        return [{"signature": x, "blockTime": int(_time.time())}
                for x in ("B1", "S1", "F1", "R1")]


_orig = rw.Rpc, rw.resolve_settings
try:
    rw.Rpc = FakeRpc
    rw.resolve_settings = lambda: ("k", W, "test")
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "t.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps(dict(trade, closed_at=int(_time.time())))
                    + "\n")
        pp = os.path.join(d, "p.json")
        with open(pp, "w") as f:
            json.dump([{"symbol": "OPEN", "token": M, "entry_tx": "B1",
                        "size_usd": 10}], f)
        jo = os.path.join(d, "o.json")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = rw.main(["--trades", tp, "--positions", pp, "--scan",
                          "--detail", "--json-out", jo])
        out = buf.getvalue()
        check("main: chay xong", rc == 0, out[-300:])
        check("main: bang theo token + TONG", "ART" in out and "TONG" in out)
        check("main: khong in api key", "api-key" not in out and "=k" not in
              out)
        check("main: scan phan loai tx ngoai bot",
              "tx_loi(mat_phi)" in out and "thu_hoi_rent/nhan_SOL" in out
              and "ngoai bot: 2" in out, out[-600:])
        check("main: vi the mo", "OPEN" in out)
        with open(jo) as f:
            check("main: json-out", len(json.load(f)["rows"]) == 1)
finally:
    rw.Rpc, rw.resolve_settings = _orig

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
