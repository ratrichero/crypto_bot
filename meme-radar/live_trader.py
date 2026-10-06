#!/usr/bin/env python3
"""Live Solana meme execution module — "dan qua" live leg.

Doc lap voi radar paper: doc tin hieu tu signals.jsonl (radar.py ghi san),
thuc hien swap THAT qua Jupiter khi mode=live, mo phong khi mode=dry_run.

Exit ladder mirror y het paper (radar.py manage_positions, plan scalp):
  TP1 +50% -> ban 1/3 | TP2 +100% -> ban them 1/3 | con lai trailing -30%
  SL -25% cat het | smart exit (>=2 vi xa cung token/30p) | time stop 480p
  (het gio: lai >=20% -> chot 1/2, giu 1/2 trailing; khong thi cat het)

An toan:
  - mode mac dinh dry_run (chi log, khong gui tx, khong can key)
  - mode live: bat buoc private key (env SOLANA_PRIVATE_KEY, nap tu file .env
    chmod 600 — uu tien) hoac file .solana_key ton tai + pubkey khop
    wallet_address, thieu -> fail closed (dung chuong trinh)
  - kill switch: file STOP trong thu muc nay -> dung nhe nhang.
    CHU Y: STOP KHONG tu dong dong vi the dang mo — phai xu ly tay.
  - daily stop: dung mo moi khi lo thuc te trong ngay < -daily_stop_pct
  - khong bao gio log private key

Chay:  .venv/bin/python live_trader.py   (doc config.live.json)
"""
import base64
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

BASE = os.path.dirname(os.path.abspath(__file__))

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnAA9JxkRrP8"

SIG_P = os.path.join(BASE, "signals.jsonl")
ALERT_P = os.path.join(BASE, "alerts.jsonl")
POS_P = os.path.join(BASE, "live_positions.json")
STATE_P = os.path.join(BASE, "live_state.json")
TRADES_P = os.path.join(BASE, "live_trades.jsonl")
LOG_P = os.path.join(BASE, "live_trader.log")
STOP_P = os.path.join(BASE, "STOP")
CFG_P = os.path.join(BASE, "config.live.json")
ENV_P = os.path.join(BASE, ".env")


def _load_env_file(path=ENV_P):
    """Nap KEY=VALUE tu file .env (chmod 600) vao os.environ neu chua co. Khong log gia tri."""
    try:
        if not os.path.exists(path):
            return
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_env_file()

# ---------------------------------------------------------------- config

DEFAULTS = {
    "mode": "dry_run",  # dry_run | live
    "wallet_address": "DxYkrsJA6YdS1cqJ9ocPCYRBacd7Xan3DeYWZva89dLd",
    "key_file": ".solana_key",
    "helius_key_file": ".helius_key",
    "jupiter_base": "https://lite-api.jup.ag",
    "trade_size_usd": 10.0,
    "max_positions": 10,
    "slippage_bps": 100,
    "priority_fee_lamports": 20000,
    "max_price_impact_pct": 5.0,
    "price_poll_seconds": 20,
    "loop_seconds": 10,
    "confirm_timeout_seconds": 30,
    "max_swap_retries": 3,
    "fee_buffer_sol": 0.02,
    "daily_stop_pct": 0.20,
    "min_signal_usd": 300,
    "skip_preflight": False,
    # exit ladder (mirror paper)
    "tp1_pct": 0.50, "tp1_frac": 0.3334,
    "tp2_pct": 1.00, "tp2_frac": 0.3333,
    "trailing_pct": 0.30,
    "sl_pct": 0.25,
    "time_stop_min": 480,
    "ts_keep_pct": 0.20, "ts_keep_frac": 0.50,
}


def load_config(path=CFG_P):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        cfg.update(json.load(open(path)))
    return cfg


# ---------------------------------------------------------------- log

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


# ---------------------------------------------------------------- pure helpers (testable)


def usd_to_lamports(usd, sol_usd):
    return int(usd / sol_usd * 1_000_000_000)


def decide_exits(pos, price, now, P):
    """Pure exit decision — mirror radar.py manage_positions (plan scalp).

    Mutates pos flags (tp1/tp2/ts_keep/ts_done/peak) giong paper.
    Returns (actions, reason): actions = [(frac, why)], reason set khi
    vi the dong han (trailing/stop_loss/smart_exit/time_stop).
    """
    actions = []
    entry = pos.get("entry") or 0
    if entry <= 0 or not price or price <= 0:
        return actions, None
    ret = price / entry - 1
    reason = None
    if price > pos.get("peak", entry):
        pos["peak"] = price
    if pos.get("remaining", 1.0) <= 0:
        return actions, None

    def take(frac, why):
        frac = min(frac, pos.get("remaining", 1.0))
        if frac <= 0:
            return
        actions.append((round(frac, 4), why))
        pos["remaining"] = pos.get("remaining", 1.0) - frac

    # TP ladder
    if not pos.get("tp1") and ret >= P["tp1_pct"]:
        pos["tp1"] = True
        take(P["tp1_frac"], "TP1")
    if pos.get("tp1") and not pos.get("tp2") and ret >= P["tp2_pct"]:
        pos["tp2"] = True
        take(P["tp2_frac"], "TP2")
    # trailing: armed sau TP2 hoac ts_keep (giong paper)
    rem = pos.get("remaining", 1.0)
    trail_arm = pos.get("tp2") or pos.get("ts_keep")
    if trail_arm and rem > 0 and price <= pos["peak"] * (1 - P["trailing_pct"]):
        take(rem, "TRAIL")
        reason = "trailing"
    # SL
    rem = pos.get("remaining", 1.0)
    if not reason and rem > 0 and ret <= -P["sl_pct"]:
        take(rem, "SL")
        reason = "stop_loss"
    # smart exit: dan qua bay
    rem = pos.get("remaining", 1.0)
    if not reason and rem > 0 and pos.get("smart_exit"):
        take(rem, "SMART_EXIT")
        reason = "smart_exit"
    # time stop
    rem = pos.get("remaining", 1.0)
    el_min = (now - pos["opened_at"]) / 60
    if (not reason and rem > 0 and not pos.get("ts_done")
            and el_min >= P["time_stop_min"]):
        pos["ts_done"] = True
        if ret >= P["ts_keep_pct"]:
            take(rem * P["ts_keep_frac"], "TIME_KEEP")
            pos["ts_keep"] = True
        else:
            take(rem, "TIME")
            reason = "time_stop"
    return actions, reason


# ---------------------------------------------------------------- key


def load_keypair(cfg):
    """Doc private key base58 tu env SOLANA_PRIVATE_KEY (uu tien, nap tu .env)
    hoac file .solana_key; verify pubkey khop wallet_address. Fail closed."""
    from solders.keypair import Keypair
    secret = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    src = "env SOLANA_PRIVATE_KEY"
    if not secret:
        kp_path = os.path.join(BASE, cfg["key_file"])
        if not os.path.exists(kp_path):
            raise SystemExit(
                f"FATAL: mode=live nhung khong thay key (env SOLANA_PRIVATE_KEY "
                f"trong va khong co key file {kp_path}) -> dung")
        secret = open(kp_path).read().strip()
        src = f"key file {kp_path}"
    if not secret:
        raise SystemExit(f"FATAL: key rong ({src}) -> dung")
    try:
        kp = Keypair.from_base58_string(secret)
    except Exception as e:
        raise SystemExit(f"FATAL: key khong hop le ({src}): {e}")
    pubkey = str(kp.pubkey())
    if pubkey != cfg["wallet_address"]:
        raise SystemExit(
            f"FATAL: pubkey tu key ({pubkey[:8]}...) khong khop "
            f"wallet_address trong config -> dung (chong nham vi)")
    return kp


# ---------------------------------------------------------------- RPC (Helius, raw JSON-RPC qua requests)


class RpcError(Exception):
    pass


class RpcClient:
    def __init__(self, url, timeout=20):
        self.url = url
        self.timeout = timeout
        self._id = 0
        self._decimals_cache = {}

    def call(self, method, params):
        self._id += 1
        r = requests.post(self.url, json={
            "jsonrpc": "2.0", "id": self._id,
            "method": method, "params": params,
        }, timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise RpcError(str(d["error"])[:200])
        return d["result"]

    def get_balance_lamports(self, pubkey):
        return int(self.call("getBalance", [pubkey])["value"])

    def get_mint_decimals(self, mint):
        if mint in self._decimals_cache:
            return self._decimals_cache[mint]
        res = self.call("getAccountInfo",
                        [mint, {"encoding": "jsonParsed"}])
        dec = res["value"]["data"]["parsed"]["info"]["decimals"]
        self._decimals_cache[mint] = dec
        return dec

    def get_token_balance_base(self, owner, mint):
        """Tra ve (base_units:int, decimals:int|None). 0 neu khong co account."""
        res = self.call("getTokenAccountsByOwner",
                        [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0
        dec = None
        for item in res.get("value", []):
            info = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += int(info["amount"])
            dec = info["decimals"]
        return total, dec

    def send_transaction(self, b64tx, skip_preflight=False):
        return self.call("sendTransaction", [b64tx, {
            "encoding": "base64",
            "skipPreflight": skip_preflight,
            "preflightCommitment": "confirmed",
        }])

    def get_sig_status(self, sig):
        """confirmed|finalized|processed|None(khong thay)"""
        res = self.call("getSignatureStatuses",
                        [[sig], {"searchTransactionHistory": True}])
        v = (res.get("value") or [None])[0]
        if not v:
            return None
        if v.get("err"):
            return "failed"
        return v.get("confirmationStatus")


# ---------------------------------------------------------------- Jupiter


class NoRoute(Exception):
    pass


class SwapError(Exception):
    pass


class JupiterClient:
    def __init__(self, base="https://lite-api.jup.ag", timeout=20,
                 http_get=None, http_post=None):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._get = http_get or requests.get
        self._post = http_post or requests.post
        self._sol_usd = (0, 0.0)

    def quote(self, in_mint, out_mint, amount_base, slippage_bps):
        r = self._get(f"{self.base}/swap/v1/quote", params={
            "inputMint": in_mint, "outputMint": out_mint,
            "amount": str(int(amount_base)),
            "slippageBps": str(int(slippage_bps)),
        }, timeout=self.timeout)
        r.raise_for_status()
        q = r.json()
        if not isinstance(q, dict) or "outAmount" not in q:
            raise NoRoute(f"no route: {str(q)[:150]}")
        return q

    def swap_tx(self, quote, user_pubkey, priority_fee):
        r = self._post(f"{self.base}/swap/v1/swap", json={
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": priority_fee,
        }, timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        tx = d.get("swapTransaction")
        if not tx:
            raise SwapError(f"swap build failed: {str(d)[:200]}")
        return tx

    def sol_price_usd(self):
        now = time.time()
        if now - self._sol_usd[0] < 60:
            return self._sol_usd[1]
        q = self.quote(SOL_MINT, USDC_MINT, 1_000_000_000, 50)
        px = int(q["outAmount"]) / 1_000_000
        self._sol_usd = (now, px)
        return px

    def token_price_usd(self, mint, decimals):
        q = self.quote(mint, USDC_MINT, 10 ** decimals, 200)
        return int(q["outAmount"]) / 1_000_000

    def ds_price_usd(self, mint):
        """Fallback DexScreener (radar van dung)."""
        r = self._get(
            f"https://api.dexscreener.com/tokens/v1/solana/{mint}",
            timeout=12)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        return float(rows[0]["priceUsd"])


# ---------------------------------------------------------------- swapper


class Swapper:
    def __init__(self, rpc, jup, keypair, cfg, dry_run):
        self.rpc = rpc
        self.jup = jup
        self.kp = keypair
        self.cfg = cfg
        self.dry = dry_run
        self.pubkey = str(keypair.pubkey()) if keypair else None

    def _priority_fee(self):
        pf = self.cfg["priority_fee_lamports"]
        if isinstance(pf, dict) and pf.get("auto"):
            return {"priorityLevelWithMaxLamports": {
                "maxLamports": pf.get("max_lamports", 1000000),
                "priorityLevel": pf.get("level", "veryHigh")}}
        return int(pf)

    def _sign_and_send(self, swap_b64):
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
        signed = VersionedTransaction(tx.message, [self.kp])
        raw_b64 = base64.b64encode(bytes(signed)).decode()
        return self.rpc.send_transaction(raw_b64, self.cfg["skip_preflight"])

    def _confirm(self, sig):
        deadline = time.time() + self.cfg["confirm_timeout_seconds"]
        while time.time() < deadline:
            st = self.rpc.get_sig_status(sig)
            if st in ("confirmed", "finalized"):
                return True
            if st == "failed":
                raise SwapError(f"tx failed on-chain: {sig[:12]}")
            time.sleep(2)
        return False

    def _quote_swap(self, in_mint, out_mint, amount_base):
        last = None
        for _ in range(self.cfg["max_swap_retries"]):
            try:
                q = self.jup.quote(in_mint, out_mint, amount_base,
                                   self.cfg["slippage_bps"])
            except NoRoute:
                raise
            except Exception as e:
                last = e
                time.sleep(1)
                continue
            try:
                pi = float(q.get("priceImpactPct") or 0)
            except (TypeError, ValueError):
                pi = 0
            if pi > self.cfg["max_price_impact_pct"]:
                raise SwapError(f"price impact {pi}% > max "
                                f"{self.cfg['max_price_impact_pct']}% -> skip")
            txb64 = self.jup.swap_tx(q, self.pubkey, self._priority_fee())
            return q, txb64
        raise SwapError(f"quote/swap failed sau {self.cfg['max_swap_retries']} "
                        f"lan thu: {last}")

    def execute_buy(self, mint, size_usd, symbol="?"):
        """Mua token bang SOL tri gia size_usd. Tra ve dict ket qua."""
        sol_usd = self.jup.sol_price_usd()
        lamports = usd_to_lamports(size_usd, sol_usd)
        if self.dry:
            q = self.jup.quote(SOL_MINT, mint, lamports,
                               self.cfg["slippage_bps"])
            dec = self.rpc.get_mint_decimals(mint)
            tokens = int(q["outAmount"]) / (10 ** dec)
            price = size_usd / tokens if tokens > 0 else 0
            log(f"DRY_RUN BUY {symbol} ${size_usd:.2f} -> {tokens:.4f} token "
                f"@~${price:.8f} (khong gui tx)")
            return {"tokens_base": int(q["outAmount"]), "decimals": dec,
                    "cost_usd": size_usd, "entry_usd": price, "tx": None,
                    "dry": True}
        bal = self.rpc.get_balance_lamports(self.pubkey)
        need = lamports + int(self.cfg["fee_buffer_sol"] * 1_000_000_000)
        if bal < need:
            raise SwapError(
                f"insufficient SOL: co {bal/1e9:.4f}, can "
                f"{need/1e9:.4f} (goc + fee buffer)")
        bal_before = bal
        q, txb64 = self._quote_swap(SOL_MINT, mint, lamports)
        sig = self._sign_and_send(txb64)
        log(f"LIVE BUY {symbol} tx={sig[:12]}... cho confirm")
        if not self._confirm(sig):
            raise SwapError(f"buy unconfirmed: {sig[:12]} (kiem tra tay)")
        bal_after = self.rpc.get_balance_lamports(self.pubkey)
        spent_usd = max(bal_before - bal_after, 0) / 1e9 * sol_usd
        tokens_base, dec = self.rpc.get_token_balance_base(self.pubkey, mint)
        if tokens_base <= 0:
            raise SwapError("buy confirmed nhung khong thay token ve vi")
        entry_usd = spent_usd / (tokens_base / (10 ** dec))
        log(f"LIVE BUY {symbol} OK nhan {tokens_base/(10**dec):.4f} token "
            f"@{entry_usd:.8f} tx={sig[:12]}...")
        return {"tokens_base": tokens_base, "decimals": dec,
                "cost_usd": round(spent_usd, 4), "entry_usd": entry_usd,
                "tx": sig, "dry": False}

    def execute_sell(self, mint, frac, symbol="?"):
        """Ban frac so token DANG CO tren vi. Tra ve dict ket qua."""
        if self.dry:
            # dry-run: khong co so du that -> mo phong hoan toan theo gia quote
            log(f"DRY_RUN SELL {symbol} {frac:.0%} (mo phong, khong gui tx)")
            return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                    "dry": True, "simulated": True}
        bal_base, dec = self.rpc.get_token_balance_base(self.pubkey, mint)
        if bal_base <= 0 or not dec:
            return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                    "dry": False, "note": "empty"}
        amount = bal_base if frac >= 0.999 else int(bal_base * frac)
        if amount <= 0:
            return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                    "dry": False, "note": "dust"}
        sol_usd = self.jup.sol_price_usd()
        sol_before = self.rpc.get_balance_lamports(self.pubkey)
        q, txb64 = self._quote_swap(mint, SOL_MINT, amount)
        sig = self._sign_and_send(txb64)
        log(f"LIVE SELL {symbol} {frac:.0%} tx={sig[:12]}... cho confirm")
        if not self._confirm(sig):
            # Khong xoa so sach voij vang: de poll sau tu kiem tra lai.
            # Tinh theo so du hien tai nen khong bi ban trung.
            nb, _ = self.rpc.get_token_balance_base(self.pubkey, mint)
            if nb < bal_base * 0.9:
                est = int(q.get("otherAmountThreshold", 0)) / 1e9 * sol_usd
                log(f"LIVE SELL {symbol} timeout nhung token da di "
                    f"-> tinh theo threshold ~${est:.2f} (CANH BAO)")
                return {"sold_base": bal_base - nb, "proceeds_usd": round(est, 4),
                        "tx": sig, "dry": False, "unconfirmed": True}
            raise SwapError(f"sell unconfirmed: {sig[:12]} (se thu lai)")
        sol_after = self.rpc.get_balance_lamports(self.pubkey)
        proceeds_usd = max(sol_after - sol_before, 0) / 1e9 * sol_usd
        log(f"LIVE SELL {symbol} OK +${proceeds_usd:.2f} tx={sig[:12]}...")
        return {"sold_base": amount, "proceeds_usd": round(proceeds_usd, 4),
                "tx": sig, "dry": False}


# ---------------------------------------------------------------- trader


def load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            pass
    return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def tail_new(path, offset):
    """Doc cac dong moi append tu offset. Tra ve (rows, new_offset)."""
    rows = []
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return rows, offset
    if offset > size:
        offset = 0
    with open(path) as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        offset = f.tell()
    return rows, offset


class LiveTrader:
    def __init__(self, cfg, jup=None, rpc=None, swapper=None):
        self.cfg = cfg
        self.dry = cfg["mode"] != "live"
        self.jup = jup or JupiterClient(cfg["jupiter_base"])
        helius_key = ""
        kp_path = os.path.join(BASE, cfg["helius_key_file"])
        if os.path.exists(kp_path):
            helius_key = open(kp_path).read().strip()
        rpc_url = (f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
                   if helius_key else "https://api.mainnet-beta.solana.com")
        self.rpc = rpc or RpcClient(rpc_url)
        if self.dry:
            self.kp = None
            log("DRY_RUN mode: chi log y dinh swap, KHONG gui transaction")
        else:
            self.kp = load_keypair(cfg)  # fail closed neu thieu/sai key
            log(f"LIVE mode: da nap key vi {cfg['wallet_address'][:8]}... "
                f"(da che). MOI LENH LA TIEN THAT.")
        self.swapper = swapper or Swapper(self.rpc, self.jup, self.kp, cfg,
                                         self.dry)
        self.positions = load_json(POS_P, [])
        st = load_json(STATE_P, {})
        self.state = {"sig_offset": 0, "alert_offset": 0, "processed": [],
                      "daily": {}, **st}
        # lan chay dau: bat dau tu CUOI file, khong danh tin cu
        if not st:
            try:
                self.state["sig_offset"] = os.path.getsize(SIG_P)
            except FileNotFoundError:
                pass
            try:
                self.state["alert_offset"] = os.path.getsize(ALERT_P)
            except FileNotFoundError:
                pass
        self._price_last = {}

    # -- helpers ------------------------------------------------------

    def save(self):
        save_json(POS_P, self.positions)
        st = self.state
        st["processed"] = st["processed"][-3000:]
        save_json(STATE_P, st)

    def _today(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _daily(self):
        d = self.state.setdefault("daily", {})
        if d.get("day") != self._today():
            # ngay moi: chup portfolio (SOL trong vi) lam moc daily stop
            try:
                sol_usd = self.jup.sol_price_usd()
                bal = (self.swapper.rpc.get_balance_lamports(
                    self.cfg["wallet_address"]) / 1e9 if not self.dry
                    else 1000.0)
                d.update({"day": self._today(), "realized_usd": 0.0,
                          "day_start_portfolio_usd": round(bal * sol_usd, 2)})
            except Exception as e:
                d.update({"day": self._today(), "realized_usd": 0.0,
                          "day_start_portfolio_usd": 1000.0})
                log(f"daily reset: khong do duoc portfolio ({e}) -> moc 1000")
        return d

    def _daily_halted(self):
        d = self._daily()
        base = d.get("day_start_portfolio_usd") or 1.0
        return d.get("realized_usd", 0.0) < -self.cfg["daily_stop_pct"] * base

    def _token_price(self, mint, dec):
        try:
            return self.jup.token_price_usd(mint, dec)
        except Exception:
            return self.jup.ds_price_usd(mint)

    # -- signal intake -------------------------------------------------

    def ingest_signals(self, now):
        sigs, off = tail_new(SIG_P, self.state["sig_offset"])
        self.state["sig_offset"] = off
        opened = 0
        for s in sigs:
            tid = s.get("tid")
            if not tid or tid in self.state["processed"]:
                continue
            self.state["processed"].append(tid)
            if (s.get("amount_usd") or 0) < self.cfg["min_signal_usd"]:
                continue
            if len(self.positions) >= self.cfg["max_positions"]:
                log(f"skip {s.get('symbol')}: du {self.cfg['max_positions']} "
                    f"vi the toi da")
                continue
            if self._daily_halted():
                log("DAILY STOP: dung mo vi the moi hom nay")
                continue
            try:
                self._open_from_signal(s, now)
                opened += 1
            except (NoRoute, SwapError) as e:
                log(f"skip mo {s.get('symbol')}: {e}")
            except Exception:
                log("ERROR mo vi the:\n" + traceback.format_exc())
        return opened

    def _open_from_signal(self, s, now):
        mint = s["token"]
        symbol = s.get("symbol", "?")
        size = float(self.cfg["trade_size_usd"])
        if self.dry:
            r = self.swapper.execute_buy(mint, size, symbol)
            entry = r["entry_usd"] or s.get("price_now") or s.get("price_usd")
            dec = r["decimals"]
            cost = size
            tx = None
        else:
            r = self.swapper.execute_buy(mint, size, symbol)
            entry = r["entry_usd"]
            dec = r["decimals"]
            cost = r["cost_usd"]
            tx = r["tx"]
            if not entry or r["tokens_base"] <= 0:
                raise SwapError("buy khong lay duoc gia/so luong that")
        pos = {
            "token": mint, "symbol": symbol, "wallet": s.get("wallet", ""),
            "signal_tid": s.get("tid"), "opened_at": int(now),
            "entry": entry, "size_usd": round(cost, 4),
            "tokens_base": r["tokens_base"], "decimals": dec,
            "peak": entry, "remaining": 1.0, "realized_usd": 0.0,
            "tp1": False, "tp2": False, "ts_keep": False, "ts_done": False,
            "smart_exit": False, "legs": [], "entry_tx": tx,
            "price_poll_at": 0,
        }
        self.positions.append(pos)
        log(f"{'DRY' if self.dry else 'LIVE'} OPEN {symbol} @{entry:.8f} "
            f"size=${cost:.2f} (copy {pos['wallet'][:8]}...)")

    # -- sell-cluster intake --------------------------------------------

    def ingest_alerts(self):
        alerts, off = tail_new(ALERT_P, self.state["alert_offset"])
        self.state["alert_offset"] = off
        for a in alerts:
            if a.get("type") != "sell_cluster":
                continue
            tok = a.get("token")
            n = 0
            for p in self.positions:
                if p["token"] == tok and p.get("remaining", 1.0) > 0 \
                        and not p.get("smart_exit"):
                    p["smart_exit"] = True
                    n += 1
            if n:
                log(f"SMART EXIT: {n} vi the {tok[:8]}... bi dan qua bay "
                    f"({a.get('n_wallets')} vi xa)")

    # -- exit engine -----------------------------------------------------

    def manage_one(self, pos, now):
        """Poll gia 1 vi the, chay exit ladder, thuc hien ban. Tra ve True
        neu vi the da dong han."""
        if now - pos.get("price_poll_at", 0) < self.cfg["price_poll_seconds"]:
            return False
        pos["price_poll_at"] = now
        try:
            price = self._token_price(pos["token"], pos["decimals"])
        except Exception as e:
            log(f"khong lay duoc gia {pos['symbol']}: {e}")
            return False
        if not price:
            return False
        actions, reason = decide_exits(pos, price, now, self.cfg)
        for frac, why in actions:
            self._sell_leg(pos, frac, why, price, now)
        # Chi dong vi the khi thuc su het token (cac leg thanh cong).
        # Neu leg that bai, remaining duoc hoan tac -> poll sau thu lai.
        if pos.get("remaining", 1.0) <= 0.005:
            if pos.get("remaining", 1.0) > 1e-6:
                log(f"{pos['symbol']}: dust {pos['remaining']:.2%} bo qua "
                    f"khi dong vi the")
                pos["remaining"] = 0.0
            self._close_position(pos, reason or "ladder_done", price, now)
            return True
        return False

    def _sell_leg(self, pos, frac, why, price, now):
        try:
            r = self.swapper.execute_sell(pos["token"], frac, pos["symbol"])
        except (NoRoute, SwapError) as e:
            log(f"{pos['symbol']} ban {why} THAT BAI: {e} (se thu lai)")
            # hoan tac flag trong decide_exits da tru? remaining da tru o
            # decide_exits (pure) -> can phuc hoi de thu lai poll sau
            pos["remaining"] = min(1.0, pos.get("remaining", 0.0) + frac)
            if why in ("TP1",):
                pos["tp1"] = False
            elif why in ("TP2",):
                pos["tp2"] = False
            return
        except Exception:
            log(f"{pos['symbol']} ban {why} LOI khong xac dinh:\n"
                + traceback.format_exc())
            pos["remaining"] = min(1.0, pos.get("remaining", 0.0) + frac)
            return
        if r.get("simulated"):
            proceeds = r["proceeds_usd"]
            # dry-run: tinh theo gia quote
            tokens = pos["tokens_base"] / (10 ** pos["decimals"])
            proceeds = tokens * frac * price
        else:
            proceeds = r["proceeds_usd"]
        pnl = proceeds - frac * pos["size_usd"]
        pos["realized_usd"] = pos.get("realized_usd", 0.0) + pnl
        self._daily()["realized_usd"] = self._daily().get(
            "realized_usd", 0.0) + pnl
        pos["legs"].append({"frac": round(frac, 4), "why": why,
                            "proceeds_usd": round(proceeds, 4),
                            "pnl_usd": round(pnl, 4),
                            "at": int(now), "tx": r.get("tx")})
        log(f"{'DRY' if self.dry else 'LIVE'} SELL {pos['symbol']} {why} "
            f"{frac:.0%} +${proceeds:.2f} (pnl {pnl:+.2f})")

    def _close_position(self, pos, reason, price, now):
        total_ret = pos.get("realized_usd", 0.0) / pos["size_usd"] \
            if pos["size_usd"] else 0
        rec = {
            "token": pos["token"], "symbol": pos["symbol"],
            "wallet": pos["wallet"], "signal_tid": pos.get("signal_tid"),
            "opened_at": pos["opened_at"], "closed_at": int(now),
            "entry": pos["entry"], "exit": price,
            "size_usd": pos["size_usd"], "legs": pos["legs"],
            "final_ret": round(total_ret, 4),
            "realized_usd": round(pos.get("realized_usd", 0.0), 4),
            "reason": reason, "mode": "dry" if self.dry else "live",
            "entry_tx": pos.get("entry_tx"),
        }
        with open(TRADES_P, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.positions.remove(pos)
        log(f"{'DRY' if self.dry else 'LIVE'} CLOSE {pos['symbol']} "
            f"final={total_ret:+.1%} (${pos.get('realized_usd', 0.0):+.2f}) "
            f"reason={reason}")

    # -- main loop --------------------------------------------------------

    def run_once(self, now=None):
        """Mot vong lap. Tra ve 'stop' neu gap kill switch."""
        now = now or time.time()
        if os.path.exists(STOP_P):
            log("STOP file -> shutdown. VI THE LIVE VAN MO — tu dong tay, "
                "module KHONG tu dong dong.")
            self.save()
            return "stop"
        try:
            self.ingest_signals(now)
        except Exception:
            log("ERROR ingest signals:\n" + traceback.format_exc())
        try:
            self.ingest_alerts()
        except Exception:
            log("ERROR ingest alerts:\n" + traceback.format_exc())
        # stagger price poll giua cac vi the
        for i, p in enumerate(list(self.positions)):
            try:
                # chi poll 1 vi the moi lan goi neu chua den han cua no
                self.manage_one(p, now + i * 0.05)
            except Exception:
                log(f"ERROR manage {p.get('symbol')}:\n"
                    + traceback.format_exc())
        self.save()
        return "ok"


def main():
    cfg = load_config()
    if cfg["mode"] not in ("dry_run", "live"):
        raise SystemExit(f"FATAL: mode '{cfg['mode']}' khong hop le "
                         "(chi nhan dry_run|live)")
    trader = LiveTrader(cfg)
    log(f"LiveTrader start mode={cfg['mode']} "
        f"size=${cfg['trade_size_usd']} maxpos={cfg['max_positions']} "
        f"slippage={cfg['slippage_bps']}bps")
    while True:
        if trader.run_once() == "stop":
            return
        time.sleep(cfg["loop_seconds"])


if __name__ == "__main__":
    main()
