#!/usr/bin/env python3
"""Doi chieu P&L live_trader (dashboard) voi giao dich THAT tren chain.

CHI DOC: khong gui tx, khong can private key. Doc chu ky tx bot da luu trong
live_trades.jsonl (entry_tx + legs[].tx) va live_positions.json, goi RPC
getTransaction de lay so du SOL/token truoc-sau, phi mang, rent.

Chay (tu goc repo, tren VPS):
    .venv/bin/python meme-radar/reconcile_wallet.py            # 24h gan nhat
    .venv/bin/python meme-radar/reconcile_wallet.py --hours 48 --scan

Cot quan trong:
  Bot      = realized_usd bot ghi (so dashboard hien).
  Chain    = cung cach tinh (SOL nhan - SOL chi, DA tru phi mang) nhung lay
             tu tx that -> lech voi Bot = loi ghi so cua bot.
  Truoc phi= Chain + phi mang (base + priority) -> gan voi cach cac app vi
             (Phantom/GMGN/...) tinh "PnL" (thuong khong tru gas).
  --scan   : liet ke tx cua vi trong cua so MA bot khong ghi (tx loi van mat
             phi, thu hoi rent, mua/ban tay, ...).

USD: dung gia SOL ma bot da dung cho chinh lenh do (suy ra tu cost/proceeds
bot ghi) de chi so sanh phan "ke toan"; leg khong suy ra duoc -> --sol-usd.
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000


# ---------------------------------------------------------------- config

def read_env_file(path, keys):
    """Chi lay cac khoa can (khong in gia tri). Dong cuoi thang."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                k = k.strip()
                if k in keys:
                    out[k] = v.strip().strip('"').strip("'")
    except (IOError, OSError):
        pass
    return out


def resolve_settings(env=None, repo=REPO):
    """-> (helius_key, wallet, nguon_key). Env tien trinh > .env goc >
    file .helius_key / config.live.json. KHONG in key."""
    env = dict(os.environ if env is None else env)
    file_env = read_env_file(os.path.join(repo, ".env"),
                             {"HELIUS_API_KEY", "HELIUS_KEY_FILE",
                              "SOL_WALLET"})
    for k, v in file_env.items():
        env.setdefault(k, v)
    key, src = (env.get("HELIUS_API_KEY") or "").strip(), "HELIUS_API_KEY"
    if not key:
        src = ""
        for path in (env.get("HELIUS_KEY_FILE"),
                     os.path.join(repo, "meme-radar", ".helius_key"),
                     os.path.join(repo, ".helius_key")):
            if not path:
                continue
            try:
                with open(path) as f:
                    key = f.read().strip()
            except (IOError, OSError):
                continue
            if key:
                src = path
                break
    wallet = (env.get("SOL_WALLET") or "").strip()
    if not wallet:
        try:
            with open(os.path.join(repo, "meme-radar",
                                   "config.live.json")) as f:
                wallet = (json.load(f).get("wallet_address") or "").strip()
        except Exception:
            wallet = ""
    return key, wallet, src


class Rpc:
    def __init__(self, url, timeout=20, pause=0.12):
        self.url, self.timeout, self.pause, self._id = url, timeout, pause, 0

    def call(self, method, params):
        import requests
        self._id += 1
        for attempt in range(4):
            try:
                r = requests.post(self.url, json={
                    "jsonrpc": "2.0", "id": self._id, "method": method,
                    "params": params}, timeout=self.timeout)
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                d = r.json()
            except Exception as e:
                # Khong in URL (chua api-key)
                if attempt == 3:
                    raise RuntimeError("%s: %s" % (method,
                                                   type(e).__name__)) from None
                time.sleep(1.0 * (attempt + 1))
                continue
            if d.get("error"):
                raise RuntimeError("%s: %s" % (method,
                                               str(d["error"])[:200]))
            time.sleep(self.pause)
            return d.get("result")
        raise RuntimeError("%s: 429 lien tuc" % method)

    def get_tx(self, sig):
        return self.call("getTransaction", [sig, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0}])

    def signatures(self, wallet, since_ts, limit=1000):
        out, before = [], None
        while len(out) < limit:
            opts = {"limit": min(1000, limit - len(out)),
                    "commitment": "confirmed"}
            if before:
                opts["before"] = before
            page = self.call("getSignaturesForAddress", [wallet, opts]) or []
            if not page:
                break
            for s in page:
                if (s.get("blockTime") or 0) < since_ts:
                    return out
                out.append(s)
            before = page[-1]["signature"]
        return out


# ---------------------------------------------------------------- pure

def _key(k):
    return k.get("pubkey") if isinstance(k, dict) else k


def parse_tx(tx, wallet, mint=None):
    """Tach tx -> so lieu cua vi. Thuan, test duoc.

    sol_delta: lamports vi thay doi (DA gom phi neu vi tra phi).
    fee: meta.fee (vi la fee payer -> phi do vi tra).
    token_delta: base units cua `mint` (moi account token chu so huu = vi).
    rent_open: lamports nap vao token account MOI cua vi (mint do).
    rent_close: lamports thu ve tu token account cua vi bi dong.
    """
    if not tx:
        return None
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [_key(k) for k in msg.get("accountKeys") or []]
    # encoding json (khong parsed) cho v0: loadedAddresses tach rieng
    la = meta.get("loadedAddresses") or {}
    if len(keys) < len(meta.get("preBalances") or []):
        keys = keys + list(la.get("writable") or []) + list(
            la.get("readonly") or [])
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    out = {"ok": meta.get("err") is None, "fee": int(meta.get("fee") or 0),
           "block_time": tx.get("blockTime"), "sol_delta": 0,
           "token_delta": 0, "rent_open": 0, "rent_close": 0,
           "fee_payer": keys[0] == wallet if keys else False,
           "mints": {}}
    if wallet in keys:
        i = keys.index(wallet)
        if i < len(pre) and i < len(post):
            out["sol_delta"] = int(post[i]) - int(pre[i])

    def _tb(lst):
        d = {}
        for b in lst or []:
            if b.get("owner") != wallet:
                continue
            amt = int(((b.get("uiTokenAmount") or {}).get("amount")) or 0)
            d[(b.get("accountIndex"), b.get("mint"))] = amt
        return d
    tpre, tpost = _tb(meta.get("preTokenBalances")), _tb(
        meta.get("postTokenBalances"))
    for k in set(tpre) | set(tpost):
        idx, m = k
        delta = tpost.get(k, 0) - tpre.get(k, 0)
        if m == SOL_MINT:
            continue  # wSOL tam cua Jupiter: da phan anh trong sol_delta
        out["mints"][m] = out["mints"].get(m, 0) + delta
        if mint and m != mint:
            continue
        if idx is None or idx >= len(pre) or idx >= len(post):
            continue
        if k not in tpre and int(pre[idx]) == 0 and int(post[idx]) > 0:
            out["rent_open"] += int(post[idx])
        if k not in tpost and int(pre[idx]) > 0 and int(post[idx]) == 0:
            out["rent_close"] += int(pre[idx])
    if mint:
        out["token_delta"] = out["mints"].get(mint, 0)
    return out


def reconcile_trade(trade, txs, wallet, sol_usd_fallback=None):
    """So realized_usd bot ghi voi tx that. txs: {sig: getTransaction}.

    Tra ve dict: bot_usd, chain_usd (cung cach bot tinh: da tru phi),
    gross_usd (truoc phi mang ~ app vi), fee_usd, cac co canh bao.
    """
    mint = trade.get("token")
    flags = []
    size_usd = float(trade.get("size_usd") or 0)
    bot_usd = float(trade.get("realized_usd") or 0)
    r = {"symbol": trade.get("symbol"), "token": mint,
         "closed_at": trade.get("closed_at"), "reason": trade.get("reason"),
         "bot_usd": bot_usd, "chain_usd": None, "gross_usd": None,
         "fee_usd": 0.0, "fee_sol": 0.0, "rent_sol": 0.0, "flags": flags,
         "buy_sol": None, "sell_sol": 0.0, "sol_usd": None,
         "tokens_bought": 0, "tokens_sold": 0}
    sig = trade.get("entry_tx")
    b = parse_tx(txs.get(sig), wallet, mint) if sig else None
    if not sig:
        flags.append("khong_co_entry_tx")
    elif b is None:
        flags.append("khong_tim_thay_entry_tx")
    elif not b["ok"]:
        flags.append("entry_tx_LOI_onchain")
    sol_px = []
    cost_sol = None
    if b and b["ok"]:
        # bot: cost_usd = (SOL chi - rent) * sol_usd; SOL chi gom phi
        cost_lam = -b["sol_delta"] - b["rent_open"]
        cost_sol = cost_lam / LAMPORTS
        r["buy_sol"] = cost_sol
        r["rent_sol"] = b["rent_open"] / LAMPORTS
        r["tokens_bought"] = b["token_delta"]
        if b["fee_payer"]:
            r["fee_sol"] += b["fee"] / LAMPORTS
        if cost_sol > 0 and size_usd > 0:
            sol_px.append(size_usd / cost_sol)
    proceeds_lam = 0
    sell_fee_lam = 0
    for leg in trade.get("legs") or []:
        lsig = leg.get("tx")
        if leg.get("note"):
            flags.append("leg_%s" % leg["note"])
            continue
        if not lsig:
            flags.append("leg_uoc_tinh_khong_tx" if leg.get("estimated")
                         else "leg_khong_tx")
            continue
        s = parse_tx(txs.get(lsig), wallet, mint)
        if s is None:
            flags.append("khong_tim_thay_sell_tx")
            continue
        if not s["ok"]:
            flags.append("sell_tx_LOI_onchain")
            continue
        # bot: proceeds = SOL sau - SOL truoc (da tru phi; neu tx dong luon
        # token account thi rent thu ve cung nam trong day)
        proceeds_lam += s["sol_delta"]
        r["tokens_sold"] += -s["token_delta"]
        if s["fee_payer"]:
            sell_fee_lam += s["fee"]
        p = float(leg.get("proceeds_usd") or 0)
        if s["sol_delta"] > 0 and p > 0:
            sol_px.append(p / (s["sol_delta"] / LAMPORTS))
    r["sell_sol"] = proceeds_lam / LAMPORTS
    r["fee_sol"] += sell_fee_lam / LAMPORTS
    if r["tokens_bought"] and r["tokens_sold"] < r["tokens_bought"] * 0.98:
        flags.append("con_token_chua_ban(%.0f%%)" % (
            100 - 100.0 * r["tokens_sold"] / r["tokens_bought"]))
    px = (sum(sol_px) / len(sol_px)) if sol_px else sol_usd_fallback
    r["sol_usd"] = px
    if cost_sol is not None and px:
        chain_sol = r["sell_sol"] - cost_sol
        r["chain_sol"] = chain_sol
        r["chain_usd"] = chain_sol * px
        r["fee_usd"] = r["fee_sol"] * px
        r["gross_usd"] = r["chain_usd"] + r["fee_usd"]
    elif not px:
        flags.append("khong_co_gia_SOL(--sol-usd)")
    r["diff_usd"] = (bot_usd - r["chain_usd"]
                     if r["chain_usd"] is not None else None)
    if r["diff_usd"] is not None and abs(r["diff_usd"]) >= 0.05:
        flags.append("BOT_LECH_CHAIN")
    return r


def aggregate(rows):
    agg = {}
    for x in rows:
        a = agg.setdefault(x["symbol"] or "?", {
            "n": 0, "bot": 0.0, "chain": 0.0, "gross": 0.0, "fee": 0.0,
            "rent": 0.0, "flags": set(), "complete": True})
        a["n"] += 1
        a["bot"] += x["bot_usd"]
        if x["chain_usd"] is None:
            a["complete"] = False
        else:
            a["chain"] += x["chain_usd"]
            a["gross"] += x["gross_usd"]
            a["fee"] += x["fee_usd"]
        a["rent"] += x["rent_sol"]
        a["flags"].update(f for f in x["flags"] if f != "BOT_LECH_CHAIN")
        if "BOT_LECH_CHAIN" in x["flags"]:
            a["flags"].add("BOT_LECH_CHAIN")
    return agg


def classify_untracked(p):
    """Phan loai tx cua vi ma bot khong ghi."""
    if p is None:
        return "khong_doc_duoc"
    if not p["ok"]:
        return "tx_loi(mat_phi)"
    toks = {m: d for m, d in p["mints"].items() if d}
    if not toks:
        if p["sol_delta"] > 0:
            return "thu_hoi_rent/nhan_SOL"
        return "khac(khong_doi_token)"
    if any(d > 0 for d in toks.values()) and p["sol_delta"] < 0:
        return "MUA_ngoai_bot"
    if any(d < 0 for d in toks.values()) and p["sol_delta"] > 0:
        return "BAN_ngoai_bot"
    return "chuyen_token/khac"


# ---------------------------------------------------------------- io

def load_trades(path, since_ts):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except ValueError:
                    continue
                if t.get("mode") != "live":
                    continue
                if (t.get("closed_at") or 0) >= since_ts:
                    out.append(t)
    except (IOError, OSError):
        pass
    return out


def _fmt(v, w=8):
    return ("%+.2f" % v).rjust(w) if v is not None else "?".rjust(w)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--trades", default=os.path.join(BASE,
                                                     "live_trades.jsonl"))
    ap.add_argument("--positions", default=os.path.join(
        BASE, "live_positions.json"))
    ap.add_argument("--wallet", default=None)
    ap.add_argument("--sol-usd", type=float, default=None,
                    help="gia SOL du phong khi khong suy ra duoc")
    ap.add_argument("--scan", action="store_true",
                    help="liet ke tx cua vi ma bot khong ghi")
    ap.add_argument("--max-scan", type=int, default=400)
    ap.add_argument("--detail", action="store_true",
                    help="in tung lenh (mac dinh chi lenh co canh bao)")
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args(argv)

    key, wallet, src = resolve_settings()
    wallet = a.wallet or wallet
    if not wallet:
        sys.exit("Khong biet vi: dat SOL_WALLET hoac --wallet")
    url = ("https://mainnet.helius-rpc.com/?api-key=%s" % key if key
           else "https://api.mainnet-beta.solana.com")
    print("Vi %s...%s | RPC: %s" % (wallet[:6], wallet[-4:],
                                   "Helius (%s)" % src if key
                                   else "public (cham, de bi 429)"))
    rpc = Rpc(url, pause=0.12 if key else 0.6)
    since = time.time() - a.hours * 3600
    trades = load_trades(a.trades, since)
    try:
        with open(a.positions) as f:
            open_pos = json.load(f) or []
    except Exception:
        open_pos = []
    sigs = set()
    for t in trades + open_pos:
        if t.get("entry_tx"):
            sigs.add(t["entry_tx"])
        for leg in t.get("legs") or []:
            if leg.get("tx"):
                sigs.add(leg["tx"])
    print("%d lenh dong trong %.0fh, %d vi the mo, %d tx can doc..."
          % (len(trades), a.hours, len(open_pos), len(sigs)))
    txs = {}
    for s in sigs:
        try:
            txs[s] = rpc.get_tx(s)
        except Exception as e:
            print("  ! %s...: %s" % (s[:10], e))
    rows = [reconcile_trade(t, txs, wallet, a.sol_usd) for t in trades]

    print("\n== Theo token (so voi bang PnL cua app vi) ==")
    print("%-12s %3s %8s %8s %8s %7s  %s" % (
        "Token", "n", "Bot", "Chain", "TruocPhi", "Phi", "Ghi chu"))
    agg = aggregate(rows)
    tot = {"bot": 0.0, "chain": 0.0, "gross": 0.0, "fee": 0.0}
    for sym, x in sorted(agg.items(), key=lambda kv: -abs(kv[1]["bot"])):
        for k in tot:
            tot[k] += x[k]
        print("%-12s %3d %s %s %s %s  %s" % (
            sym[:12], x["n"], _fmt(x["bot"]),
            _fmt(x["chain"] if x["complete"] else None),
            _fmt(x["gross"] if x["complete"] else None), _fmt(x["fee"], 7),
            ",".join(sorted(x["flags"]))))
    print("%-12s %3d %s %s %s %s" % ("TONG", len(rows), _fmt(tot["bot"]),
                                     _fmt(tot["chain"]), _fmt(tot["gross"]),
                                     _fmt(tot["fee"], 7)))
    print("  Bot = dashboard; Chain = tx that (da tru phi mang); "
          "TruocPhi = Chain + phi mang (~ cach app vi tinh).")

    bad = [x for x in rows if x["flags"]]
    show = rows if a.detail else bad
    if show:
        print("\n== Tung lenh%s ==" % ("" if a.detail else " co canh bao"))
        for x in sorted(show, key=lambda r: r["closed_at"] or 0):
            ts = time.strftime("%m-%d %H:%M",
                               time.localtime(x["closed_at"] or 0))
            print("%s %-10s bot %s chain %s phi %s rent %.5f SOL  %s" % (
                ts, (x["symbol"] or "?")[:10], _fmt(x["bot_usd"], 7),
                _fmt(x["chain_usd"], 7), _fmt(x["fee_usd"], 6),
                x["rent_sol"], ",".join(x["flags"]) or "ok"))

    if open_pos:
        print("\n== Vi the dang mo ==")
        for p in open_pos:
            b = parse_tx(txs.get(p.get("entry_tx")), wallet, p.get("token"))
            if not b:
                print("  %s: khong doc duoc entry tx" % p.get("symbol"))
                continue
            cost = (-b["sol_delta"] - b["rent_open"]) / LAMPORTS
            print("  %-10s bot cost $%.2f | chain %.6f SOL (phi %.6f, rent "
                  "%.5f)" % (p.get("symbol"), float(p.get("size_usd") or 0),
                             cost, b["fee"] / LAMPORTS,
                             b["rent_open"] / LAMPORTS))

    untracked = []
    if a.scan:
        print("\n== Tx cua vi trong %.0fh MA bot khong ghi ==" % a.hours)
        all_sigs = rpc.signatures(wallet, since, limit=a.max_scan)
        cats = {}
        for s in all_sigs:
            sig = s["signature"]
            if sig in sigs:
                continue
            try:
                p = parse_tx(rpc.get_tx(sig), wallet)
            except Exception:
                p = None
            c = classify_untracked(p)
            d = cats.setdefault(c, {"n": 0, "sol": 0.0, "fee": 0.0})
            d["n"] += 1
            if p:
                d["sol"] += p["sol_delta"] / LAMPORTS
                d["fee"] += (p["fee"] / LAMPORTS) if p["fee_payer"] else 0
            untracked.append({"sig": sig, "cat": c, "time": s.get("blockTime"),
                              "sol_delta": p and p["sol_delta"],
                              "mints": p and p["mints"]})
        print("Tong tx vi: %d, bot ghi: %d, ngoai bot: %d" % (
            len(all_sigs), len(all_sigs) - len(untracked), len(untracked)))
        for c, d in sorted(cats.items(), key=lambda kv: -kv[1]["n"]):
            print("  %-24s %4d tx  SOL %+.6f  (phi %.6f)" % (
                c, d["n"], d["sol"], d["fee"]))
        for u in untracked:
            if u["cat"] in ("MUA_ngoai_bot", "BAN_ngoai_bot",
                            "chuyen_token/khac"):
                print("    %s %s %s... SOL %+.6f" % (
                    time.strftime("%m-%d %H:%M",
                                  time.localtime(u["time"] or 0)),
                    u["cat"], u["sig"][:12],
                    (u["sol_delta"] or 0) / LAMPORTS))
    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump({"rows": rows, "untracked": untracked}, f, indent=1,
                      default=list)
        print("\nDa ghi %s" % a.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
