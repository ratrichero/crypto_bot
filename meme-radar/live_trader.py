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
import math
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

from strategy import decide_exits

BASE = os.path.dirname(os.path.abspath(__file__))

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_MINTS = {USDC_MINT, USDT_MINT}
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

SIG_P = os.path.join(BASE, "signals.jsonl")
ALERT_P = os.path.join(BASE, "alerts.jsonl")
SELL_P = os.path.join(BASE, "sells.jsonl")
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
    "slippage_bps": 100,  # buy
    "sell_slippage_bps": 300,  # sell: wider for volatile meme exits
    "priority_fee_lamports": 20000,
    "max_price_impact_pct": 5.0,
    "sell_max_price_impact_pct": 10.0,
    "price_poll_seconds": 20,
    "loop_seconds": 10,
    "confirm_timeout_seconds": 90,
    "confirm_poll_seconds": 2,
    "buy_balance_verify_attempts": 4,
    "buy_balance_verify_seconds": 2,
    "max_swap_retries": 3,
    "fee_buffer_sol": 0.02,
    "daily_stop_pct": 0.20,
    "min_signal_usd": 300,
    "skip_preflight": False,
    # duong dan signal: de trong -> dung file trong thu muc module.
    # Khi live_trader chay tren VPS con radar paper chay may khac,
    # tro 2 truong nay sang thu muc nhan signal forward, vd:
    # "signals_jsonl": "/home/ubuntu/muse_bot/live-signals/signals.jsonl"
    "signals_jsonl": "",
    "alerts_jsonl": "",
    "sells_jsonl": "",
    "reconcile_interval_seconds": 60,
    "signal_retry_seconds": 30,
    "max_signal_retry_seconds": 900,
    "recover_unmanaged_tokens": True,
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
        return int(self.call("getBalance", [pubkey, {"commitment": "confirmed"}])["value"])

    def get_mint_decimals(self, mint):
        if mint in self._decimals_cache:
            return self._decimals_cache[mint]
        res = self.call("getAccountInfo",
                        [mint, {"encoding": "jsonParsed"}])
        dec = res["value"]["data"]["parsed"]["info"]["decimals"]
        self._decimals_cache[mint] = dec
        return dec

    def _token_account_rows(self, owner):
        """Read both SPL Token programs; never silently omit Token-2022."""
        rows = []
        query_errors = []
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            try:
                res = self.call(
                    "getTokenAccountsByOwner",
                    [owner, {"programId": program}, {
                        "encoding": "jsonParsed", "commitment": "confirmed",
                    }],
                )
                rows.extend(res.get("value", []))
            except Exception as e:
                query_errors.append(f"{program[:8]}: {e}")
        if query_errors:
            raise RpcError("token account scan incomplete: "
                           + "; ".join(query_errors)[:300])
        return rows

    @staticmethod
    def _token_info(item):
        return item["account"]["data"]["parsed"]["info"]

    def get_token_balance_base(self, owner, mint):
        """Return (base_units, decimals), including Token-2022 accounts."""
        total = 0
        dec = None
        for item in self._token_account_rows(owner):
            info = self._token_info(item)
            if info.get("mint") != mint:
                continue
            token_amount = info["tokenAmount"]
            total += int(token_amount["amount"])
            dec = int(token_amount["decimals"])
        return total, dec

    def get_token_balances(self, owner):
        """Return all non-zero SPL and Token-2022 balances by mint."""
        balances = {}
        for item in self._token_account_rows(owner):
            try:
                info = self._token_info(item)
                token_amount = info["tokenAmount"]
                amount = int(token_amount["amount"])
                if amount <= 0:
                    continue
                mint = info["mint"]
                dec = int(token_amount["decimals"])
                balances[mint] = {
                    "amount": balances.get(mint, {}).get("amount", 0) + amount,
                    "decimals": dec,
                }
            except (KeyError, TypeError, ValueError):
                continue
        return balances

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


class SwapUncertain(SwapError):
    """A transaction may have landed; never blindly retry this operation."""


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
        deadline = time.time() + float(self.cfg.get("confirm_timeout_seconds", 90))
        poll_seconds = float(self.cfg.get("confirm_poll_seconds", 2))
        while time.time() < deadline:
            st = self.rpc.get_sig_status(sig)
            if st in ("confirmed", "finalized"):
                return True
            if st == "failed":
                raise SwapError(f"tx failed on-chain: {sig[:12]}")
            time.sleep(min(poll_seconds, max(0, deadline - time.time())))
        # Some RPC providers lag getSignatureStatuses while the transaction
        # is already available in history. This is only a status fallback;
        # BUY still requires token-balance delta verification afterwards.
        try:
            tx = self.rpc.call(
                "getTransaction",
                [sig, {"encoding": "json",
                       "maxSupportedTransactionVersion": 0}],
            )
            if tx and tx.get("meta") and tx["meta"].get("err") is None:
                log(f"confirm fallback: tx {sig[:12]}... thanh cong "
                    "(getTransaction), tiep tuc verify balance")
                return True
        except Exception as e:
            log(f"confirm fallback loi: {e}")
        return False

    def _wait_for_buy_delta(self, mint, before_base):
        """Verify a submitted BUY by token balance, even if status RPC lags."""
        attempts = max(1, int(self.cfg.get("buy_balance_verify_attempts", 4)))
        delay = float(self.cfg.get("buy_balance_verify_seconds", 2))
        last_error = None
        last_dec = None
        for i in range(attempts):
            try:
                after_base, dec = self.rpc.get_token_balance_base(
                    self.pubkey, mint)
                last_dec = dec if dec is not None else last_dec
                delta = after_base - before_base
                if delta > 0:
                    return delta, last_dec, None
            except Exception as e:
                last_error = e
            if i + 1 < attempts:
                time.sleep(delay)
        return 0, last_dec, last_error

    def _buy_result(self, sig, symbol, size_usd, sol_usd, bal_before,
                    token_delta, dec):
        if dec is None or token_delta <= 0:
            raise SwapUncertain(
                f"BUY {symbol} verify khong co token delta hop le")
        try:
            bal_after = self.rpc.get_balance_lamports(self.pubkey)
            spent_usd = max(bal_before - bal_after, 0) / 1e9 * sol_usd
        except Exception as e:
            # Token delta is the authoritative execution proof; SOL P&L is
            # only accounting, so retain a conservative cost fallback.
            spent_usd = size_usd
            log(f"BUY {symbol}: khong doc duoc SOL balance sau tx ({e}); "
                f"dung cost=${size_usd:.2f}")
        if spent_usd <= 0:
            spent_usd = size_usd
        entry_usd = spent_usd / (token_delta / (10 ** dec))
        log(f"LIVE BUY {symbol} OK nhan {token_delta/(10**dec):.4f} token "
            f"@{entry_usd:.8f} tx={(sig or 'unknown')[:12]}...")
        return {"tokens_base": token_delta, "decimals": dec,
                "cost_usd": round(spent_usd, 4), "entry_usd": entry_usd,
                "tx": sig, "dry": False}

    def _quote_swap(self, in_mint, out_mint, amount_base,
                    slippage_bps=None, max_price_impact_pct=None):
        slippage_bps = (self.cfg["slippage_bps"] if slippage_bps is None
                        else slippage_bps)
        max_price_impact_pct = (
            self.cfg["max_price_impact_pct"]
            if max_price_impact_pct is None else max_price_impact_pct)
        last = None
        for _ in range(self.cfg["max_swap_retries"]):
            try:
                q = self.jup.quote(in_mint, out_mint, amount_base,
                                   slippage_bps)
            except NoRoute as e:
                # Route availability can be transient, especially on meme
                # exits. Re-quote instead of turning a no-route into an
                # uncertain on-chain operation.
                last = e
                time.sleep(1)
                continue
            except Exception as e:
                last = e
                time.sleep(1)
                continue
            try:
                pi = float(q.get("priceImpactPct") or 0)
            except (TypeError, ValueError):
                pi = 0
            if pi > max_price_impact_pct:
                raise SwapError(f"price impact {pi}% > max "
                                f"{max_price_impact_pct}% -> skip")
            try:
                txb64 = self.jup.swap_tx(q, self.pubkey, self._priority_fee())
            except Exception as e:
                last = e
                time.sleep(1)
                continue
            return q, txb64
        raise SwapError(f"quote/swap failed sau {self.cfg['max_swap_retries']} "
                        f"lan thu: {last}")

    def execute_buy(self, mint, size_usd, symbol="?"):
        """Mua token bang SOL tri gia size_usd. Tra ve dict ket qua."""
        try:
            sol_usd = self.jup.sol_price_usd()
            if not sol_usd or sol_usd <= 0:
                raise ValueError("SOL price khong hop le")
        except Exception as e:
            # No transaction has been submitted yet, so the signal may retry.
            raise SwapError(f"khong lay duoc SOL price truoc BUY: {e}")
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
        try:
            bal = self.rpc.get_balance_lamports(self.pubkey)
        except Exception as e:
            raise SwapError(f"khong doc duoc SOL balance truoc BUY: {e}")
        need = lamports + int(self.cfg["fee_buffer_sol"] * 1_000_000_000)
        if bal < need:
            raise SwapError(
                f"insufficient SOL: co {bal/1e9:.4f}, can "
                f"{need/1e9:.4f} (goc + fee buffer)")
        bal_before = bal
        try:
            tokens_before, dec_before = self.rpc.get_token_balance_base(
                self.pubkey, mint)
        except Exception as e:
            # Still before quote/sign/send: retry instead of calling this
            # uncertain or placing a second transaction.
            raise SwapError(
                f"khong doc duoc token balance truoc BUY {symbol}: {e}")
        q, txb64 = self._quote_swap(SOL_MINT, mint, lamports)
        try:
            sig = self._sign_and_send(txb64)
        except Exception as e:
            # sendTransaction can time out after the validator accepted it.
            # Verify the token delta before deciding that BUY failed.
            delta, dec, verify_error = self._wait_for_buy_delta(
                mint, tokens_before)
            if delta > 0:
                log(f"LIVE BUY {symbol} send exception nhung token da ve "
                    "vi -> coi la thanh cong")
                return self._buy_result(
                    None, symbol, size_usd, sol_usd, bal_before, delta,
                    dec if dec is not None else dec_before)
            if verify_error:
                raise SwapUncertain(
                    f"BUY {symbol} send exception + verify loi: {verify_error}")
            raise SwapUncertain(
                f"BUY {symbol} send exception, khong thay token: {e}")
        log(f"LIVE BUY {symbol} tx={sig[:12]}... cho confirm")

        confirm_error = None
        status_unknown = False
        try:
            confirmed = self._confirm(sig)
        except SwapError as e:
            # A known on-chain failure is retryable, but still verify first:
            # RPC/provider responses can disagree with token state.
            confirmed = False
            confirm_error = e
        except Exception as e:
            confirmed = False
            confirm_error = e
            status_unknown = True

        delta, dec, verify_error = self._wait_for_buy_delta(
            mint, tokens_before)
        if delta > 0:
            if not confirmed:
                log(f"LIVE BUY {symbol}: status chua chac/timeout nhung "
                    "token delta da xac nhan")
            return self._buy_result(
                sig, symbol, size_usd, sol_usd, bal_before, delta,
                dec if dec is not None else dec_before)
        if verify_error:
            raise SwapUncertain(
                f"BUY {symbol} status={confirm_error or 'timeout'}; "
                f"verify token loi: {verify_error}")
        if confirm_error is not None and not status_unknown:
            raise confirm_error
        raise SwapUncertain(
            f"BUY {symbol} unconfirmed/unknown {sig[:12]} (da verify, "
            "khong thay token)")

    def execute_sell(self, mint, frac, symbol="?"):
        """Ban frac so token DANG CO tren vi. Tra ve dict ket qua."""
        if self.dry:
            # dry-run: khong co so du that -> mo phong hoan toan theo gia quote
            log(f"DRY_RUN SELL {symbol} {frac:.0%} (mo phong, khong gui tx)")
            return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                    "dry": True, "simulated": True}
        try:
            bal_base, dec = self.rpc.get_token_balance_base(self.pubkey, mint)
        except Exception as e:
            raise SwapError(f"khong doc duoc token balance truoc SELL: {e}")
        if bal_base <= 0 or dec is None:
            return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                    "dry": False, "note": "empty"}
        amount = bal_base if frac >= 0.999 else int(bal_base * frac)
        if amount <= 0:
            return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                    "dry": False, "note": "dust"}
        try:
            sol_usd = self.jup.sol_price_usd()
            sol_before = self.rpc.get_balance_lamports(self.pubkey)
        except Exception as e:
            raise SwapError(f"khong lay duoc gia/balance truoc SELL: {e}")
        sell_slippage = int(self.cfg.get(
            "sell_slippage_bps", self.cfg["slippage_bps"]))
        sell_impact = float(self.cfg.get(
            "sell_max_price_impact_pct", self.cfg["max_price_impact_pct"]))
        q, txb64 = self._quote_swap(
            mint, SOL_MINT, amount,
            slippage_bps=sell_slippage,
            max_price_impact_pct=sell_impact,
        )
        sig = self._sign_and_send(txb64)
        log(f"LIVE SELL {symbol} {frac:.0%} tx={sig[:12]}... cho confirm "
            f"(slippage={sell_slippage}bps, impact<={sell_impact:g}%)")
        confirmed = self._confirm(sig)
        if not confirmed:
            # Do not retry blindly: inspect the actual token balance first.
            try:
                nb, _ = self.rpc.get_token_balance_base(self.pubkey, mint)
            except Exception as e:
                raise SwapUncertain(
                    f"SELL {symbol} timeout, khong doc duoc balance: {e}")
            if nb < bal_base * 0.9:
                est = int(q.get("otherAmountThreshold", 0)) / 1e9 * sol_usd
                log(f"LIVE SELL {symbol} timeout nhung token da di "
                    f"-> tinh theo threshold ~${est:.2f} (CANH BAO)")
                return {"sold_base": bal_base - nb, "proceeds_usd": round(est, 4),
                        "tx": sig, "dry": False, "unconfirmed": True}
            raise SwapUncertain(f"sell unconfirmed: {sig[:12]} (se reconcile)")
        try:
            sol_after = self.rpc.get_balance_lamports(self.pubkey)
            nb, _ = self.rpc.get_token_balance_base(self.pubkey, mint)
        except Exception as e:
            raise SwapUncertain(
                f"SELL {symbol} da confirm nhung khong doc duoc balance: {e}")
        sold_base = max(bal_base - nb, 0)
        if sold_base <= 0:
            raise SwapUncertain(
                f"SELL {symbol} da confirm nhung token balance khong giam")
        proceeds_usd = max(sol_after - sol_before, 0) / 1e9 * sol_usd
        log(f"LIVE SELL {symbol} OK +${proceeds_usd:.2f} tx={sig[:12]}...")
        return {"sold_base": sold_base, "proceeds_usd": round(proceeds_usd, 4),
                "tx": sig, "dry": False}


# ---------------------------------------------------------------- trader


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as e:
            log(f"WARNING file state khong doc duoc {path}: {e} -> dung default")
    return default


def save_json(path, obj):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _configured_path(cfg, key, default):
    """Resolve relative signal paths against this module, not process cwd."""
    value = cfg.get(key) or default
    return value if os.path.isabs(value) else os.path.join(BASE, value)


def sig_path(cfg):
    """Duong dan signals.jsonl (configurable de chay cross-machine)."""
    return _configured_path(cfg, "signals_jsonl", SIG_P)


def alert_path(cfg):
    return _configured_path(cfg, "alerts_jsonl", ALERT_P)


def sell_path(cfg):
    return _configured_path(cfg, "sells_jsonl", SELL_P)


def tail_new(path, offset):
    """Read complete appended JSONL records without losing a partial tail.

    Offsets are byte offsets. A producer may be in the middle of writing the
    last line; that line stays unread until a later call completes it.
    """
    rows = []
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return rows, offset
    if offset < 0 or offset > size:
        offset = 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        cursor = offset
        while True:
            line_start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                # Do not advance past an incomplete append.
                cursor = line_start
                break
            cursor = handle.tell()
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # A complete malformed line cannot be retried forever; callers
                # keep reading later records instead of blocking the stream.
                continue
    return rows, cursor


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
        self.paths = {
            "signals": os.path.abspath(sig_path(self.cfg)),
            "alerts": os.path.abspath(alert_path(self.cfg)),
            "sells": os.path.abspath(sell_path(self.cfg)),
        }
        self.state = {
            "sig_offset": 0, "alert_offset": 0, "sell_offset": 0,
            "processed": [], "processed_sells": [],
            "signal_failures": {}, "pending_buys": {},
            "source_files": {}, "daily": {}, **st,
        }
        self.entry_blocked = False
        self.onchain_tokens = set()
        self.unmanaged_tokens = set()
        self._last_reconcile = 0
        self._init_source_offsets(not bool(st))
        self._price_last = {}
        self._log_source_health()

    def _init_source_offsets(self, first_start):
        """Bind persisted offsets to the configured files.

        A path change or inode rotation must not silently reuse an offset from
        another machine/file. On first start we intentionally skip historical
        signals; on-chain recovery handles already-held tokens separately.
        """
        key_to_offset = {
            "signals": "sig_offset",
            "alerts": "alert_offset",
            "sells": "sell_offset",
        }
        source_files = self.state.setdefault("source_files", {})
        for key, path in self.paths.items():
            meta = source_files.get(key) or {}
            try:
                st = os.stat(path)
            except FileNotFoundError:
                if first_start:
                    self.state[key_to_offset[key]] = 0
                source_files[key] = {"path": path, "inode": None,
                                     "size": 0, "missing": True}
                continue
            path_changed = meta.get("path") not in (None, path)
            inode_changed = (meta.get("inode") is not None
                             and meta.get("inode") != st.st_ino)
            if first_start or path_changed or inode_changed:
                self.state[key_to_offset[key]] = st.st_size
                if path_changed or inode_changed:
                    log(f"source {key} thay file -> bo qua history, "
                        f"bat dau tai EOF: {path}")
            elif self.state[key_to_offset[key]] > st.st_size:
                self.state[key_to_offset[key]] = 0
            source_files[key] = {
                "path": path, "inode": st.st_ino, "size": st.st_size,
            }

    def _log_source_health(self):
        for key, path in self.paths.items():
            if os.path.exists(path):
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = -1
                log(f"source {key}: {path} ({size} bytes, "
                    f"offset={self._offset_key(key)})")
            else:
                log(f"WARNING source {key} CHUA TON TAI: {path}")

    @staticmethod
    def _offset_key(key):
        return {"signals": "sig_offset", "alerts": "alert_offset",
                "sells": "sell_offset"}[key]

    def _tail_source(self, key):
        path = self.paths[key]
        offset_key = self._offset_key(key)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return [], self.state[offset_key]
        meta = self.state.setdefault("source_files", {}).get(key, {})
        if meta.get("missing"):
            # The source did not exist at startup; consume the file created
            # afterwards. If deployment forwards a historical file, configure
            # it before starting the trader so startup can bind at EOF.
            self.state[offset_key] = 0
            log(f"source {key} vua xuat hien -> doc tu dau: {path}")
        elif (meta.get("path") != path or
              (meta.get("inode") is not None and meta["inode"] != st.st_ino)):
            self.state[offset_key] = 0
        if self.state[offset_key] > st.st_size:
            self.state[offset_key] = 0
        rows, offset = tail_new(path, self.state[offset_key])
        self.state[offset_key] = offset
        self.state.setdefault("source_files", {})[key] = {
            "path": path, "inode": st.st_ino, "size": st.st_size,
        }
        return rows, offset

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
        if (not self.dry and d.get("day") == self._today()
                and "risk_unavailable" not in d):
            # Old state files predate fail-closed daily accounting.
            d["risk_unavailable"] = True
            log("daily state cu khong co portfolio baseline -> block entry")
        if d.get("day") != self._today():
            # ngay moi: chup portfolio (SOL trong vi) lam moc daily stop
            try:
                sol_usd = self.jup.sol_price_usd()
                bal = (self.swapper.rpc.get_balance_lamports(
                    self.cfg["wallet_address"]) / 1e9 if not self.dry
                    else 1000.0)
                d.update({"day": self._today(), "realized_usd": 0.0,
                          "day_start_portfolio_usd": round(bal * sol_usd, 2),
                          "risk_unavailable": False})
            except Exception as e:
                d.update({"day": self._today(), "realized_usd": 0.0,
                          "day_start_portfolio_usd": 1000.0,
                          "risk_unavailable": not self.dry})
                log(f"daily reset: khong do duoc portfolio ({e}) -> "
                    f"{'block entry' if not self.dry else 'moc 1000'}")
        return d

    def _daily_halted(self):
        d = self._daily()
        if not self.dry and d.get("risk_unavailable"):
            return True
        base = d.get("day_start_portfolio_usd") or 1.0
        return d.get("realized_usd", 0.0) < -self.cfg["daily_stop_pct"] * base

    def _token_price(self, mint, dec):
        try:
            return self.jup.token_price_usd(mint, dec)
        except Exception:
            return self.jup.ds_price_usd(mint)

    def _recent_signal_by_token(self, limit=5000):
        """Load recent buy signals for startup recovery, independent of offset."""
        rows = []
        path = self.paths["signals"]
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(row, dict) and row.get("token"):
                        rows.append(row)
                        if len(rows) > limit:
                            del rows[:len(rows) - limit]
        except FileNotFoundError:
            pass
        latest = {}
        for row in rows:
            latest[row["token"]] = row
        return latest

    def reconcile_onchain(self, now=None, force=False):
        """Reconcile wallet token balances before allowing new entries.

        This closes the blind spot between a confirmed swap and the local JSON
        save, and recovers a position after restart when its buy signal still
        exists. Unknown non-zero tokens are never sold automatically; they
        block new entries and are logged for manual review.
        """
        if self.dry:
            self.entry_blocked = False
            return True
        now = time.time() if now is None else now
        if (not force and now - self._last_reconcile
                < float(self.cfg.get("reconcile_interval_seconds", 60))):
            return not self.entry_blocked
        self._last_reconcile = now
        try:
            balances = self.swapper.rpc.get_token_balances(self.swapper.pubkey)
        except Exception as e:
            self.entry_blocked = True
            log(f"CRITICAL reconcile token balances loi: {e} -> block entry")
            return False
        known = self._recent_signal_by_token()
        self.onchain_tokens = {
            mint for mint, bal in balances.items()
            if int(bal.get("amount", 0) or 0) > 0
            and mint not in STABLE_MINTS
            and mint != SOL_MINT
        }
        log(f"RECONCILE wallet: {len(self.onchain_tokens)} non-stable token "
            f"balances, tracked={len(self.positions)}, "
            f"occupancy={len(self.onchain_tokens | {p.get('token') for p in self.positions})}")
        changed = False
        unmanaged = []
        pending = self.state.setdefault("pending_buys", {})
        for pos in list(self.positions):
            bal = balances.get(pos.get("token"), {})
            actual = int(bal.get("amount", 0) or 0)
            if actual <= 0:
                log(f"RECONCILE: {pos.get('symbol')} khong con token "
                    "tren vi -> bo local position, khong ban lai")
                self.positions.remove(pos)
                changed = True
                continue
            pos["onchain_tokens_base"] = actual
            pending.pop(str(pos.get("signal_tid") or ""), None)
            initial = int(pos.get("tokens_base", 0) or 0)
            if initial <= 0:
                pos["tokens_base"] = actual
                pos["decimals"] = int(bal.get("decimals", pos.get("decimals", 0)))
                initial = actual
                changed = True
            if initial > 0:
                expected = initial * float(pos.get("remaining", 1.0))
                if actual < expected * 0.98:
                    pos["remaining"] = min(1.0, actual / initial)
                    log(f"RECONCILE: {pos.get('symbol')} balance giam -> "
                        f"remaining={pos['remaining']:.4f}")
                    changed = True
        local_tokens = {p.get("token") for p in self.positions}
        for mint, bal in balances.items():
            amount = int(bal.get("amount", 0) or 0)
            if amount <= 0 or mint in (SOL_MINT, USDC_MINT) or mint in local_tokens:
                continue
            signal = known.get(mint)
            if not signal or not self.cfg.get("recover_unmanaged_tokens", True):
                unmanaged.append(mint)
                continue
            entry = signal.get("price_now") or signal.get("price_usd")
            if not entry or float(entry) <= 0:
                unmanaged.append(mint)
                continue
            pos = {
                "token": mint, "symbol": signal.get("symbol", "?"),
                "wallet": signal.get("wallet", ""),
                "signal_tid": signal.get("tid"),
                "opened_at": int(signal.get("ts") or signal.get("detected_at") or now),
                "entry": float(entry),
                "size_usd": float(self.cfg["trade_size_usd"]),
                "tokens_base": amount, "onchain_tokens_base": amount,
                "decimals": int(bal.get("decimals", 0)), "peak": float(entry),
                "remaining": 1.0, "realized_usd": 0.0,
                "tp1": False, "tp2": False, "ts_keep": False,
                "ts_done": False, "smart_exit": False, "legs": [],
                "entry_tx": signal.get("tx"), "recovered": True,
                "price_poll_at": 0,
            }
            self.positions.append(pos)
            local_tokens.add(mint)
            tid = str(signal.get("tid") or "")
            if tid:
                self.state.setdefault("processed", []).append(tid)
                self.state.setdefault("pending_buys", {}).pop(tid, None)
            changed = True
            log(f"RECOVER position {pos['symbol']} balance={amount} "
                f"entry~{entry} (khong mua lai)")
        self.unmanaged_tokens = set(unmanaged)
        self.state["unmanaged_tokens"] = unmanaged[-100:]
        pending_uncertain = bool(pending)
        self.entry_blocked = bool(unmanaged or pending_uncertain)
        if pending_uncertain:
            log("CRITICAL pending BUY intent chua reconcile xong -> block entry")
        if unmanaged:
            log(f"CRITICAL unmanaged token(s) tren vi: "
                f"{', '.join(x[:10] + '...' for x in unmanaged)} -> block entry")
        if changed:
            self.save()
        return not self.entry_blocked

    def _mark_processed(self, tid, status="opened"):
        processed = self.state.setdefault("processed", [])
        if tid not in processed:
            processed.append(tid)
        failures = self.state.setdefault("signal_failures", {})
        failures.pop(tid, None)
        self.state["processed"] = processed[-3000:]
        self.save()
        log(f"signal {tid[:12]}... -> {status}")

    def _record_signal_failure(self, signal, now, error):
        tid = signal["tid"]
        failures = self.state.setdefault("signal_failures", {})
        old = failures.get(tid, {})
        attempts = int(old.get("attempts", 0)) + 1
        base = float(self.cfg.get("signal_retry_seconds", 30))
        ceiling = float(self.cfg.get("max_signal_retry_seconds", 900))
        retry_after = now + min(ceiling, base * (2 ** min(attempts - 1, 5)))
        failures[tid] = {
            "signal": signal, "attempts": attempts,
            "last_error": str(error)[:300], "retry_at": retry_after,
        }
        self.save()
        log(f"signal {tid[:12]}... FAIL attempt={attempts}, "
            f"retry_at={datetime.fromtimestamp(retry_after, tz=timezone.utc).isoformat()}: "
            f"{error}")

    def _retry_failed_signals(self, now):
        """Retry transient buy failures even after the JSONL offset advanced."""
        failures = self.state.setdefault("signal_failures", {})
        due = []
        for tid, rec in list(failures.items()):
            if float(rec.get("retry_at", 0)) <= now and rec.get("signal"):
                due.append(rec["signal"])
        return due

    # -- signal intake -------------------------------------------------

    def _occupied_token_count(self):
        tracked = {
            p.get("token") for p in self.positions
            if p.get("remaining", 1.0) > 0
        }
        return len(tracked | set(self.onchain_tokens))

    def _attempt_signal(self, s, now):
        tid = str(s.get("tid") or "")
        if not tid or tid in self.state.setdefault("processed", []):
            return False
        if not isinstance(s.get("token"), str) or not s.get("token"):
            self._mark_processed(tid, "skipped_invalid_token")
            return False
        try:
            amount_usd = float(s.get("amount_usd") or 0)
        except (TypeError, ValueError):
            self._mark_processed(tid, "skipped_invalid_amount")
            return False
        if not math.isfinite(amount_usd):
            self._mark_processed(tid, "skipped_invalid_amount")
            return False
        if amount_usd < self.cfg["min_signal_usd"]:
            self._mark_processed(tid, "skipped_below_minimum")
            return False
        occupied = self._occupied_token_count()
        if occupied >= self.cfg["max_positions"]:
            self._mark_processed(tid, "skipped_capacity")
            log(f"skip {s.get('symbol')}: wallet/positions da co "
                f"{occupied}/{self.cfg['max_positions']} token")
            return False
        if self._daily_halted():
            self._mark_processed(tid, "skipped_daily_stop")
            log("DAILY STOP: dung mo vi the moi hom nay")
            return False
        if self.entry_blocked:
            self._record_signal_failure(
                s, now, "entry blocked: on-chain reconciliation pending")
            return False
        pending = self.state.setdefault("pending_buys", {})
        pending[tid] = {"signal": s, "started_at": int(now),
                        "status": "buy_intent"}
        self.save()  # durable intent before a network side effect
        try:
            self._open_from_signal(s, now)
        except SwapUncertain as e:
            pending[tid]["status"] = "buy_uncertain"
            pending[tid]["error"] = str(e)[:300]
            self.entry_blocked = True
            self.save()
            log(f"BUY {tid[:12]}... khong chac ket qua -> block entry: {e}")
            return False
        except SwapError as e:
            pending.pop(tid, None)
            self._record_signal_failure(s, now, e)
            return False
        except Exception as e:
            pending[tid]["status"] = "buy_uncertain"
            pending[tid]["error"] = str(e)[:300]
            self.entry_blocked = True
            self.save()
            log(f"BUY {tid[:12]}... khong chac ket qua -> block entry: {e}")
            return False
        pending.pop(tid, None)
        self._mark_processed(tid, "opened")
        return True

    def ingest_signals(self, now):
        sigs, _ = self._tail_source("signals")
        opened = 0
        # Retry previously failed signals first
        for s in self._retry_failed_signals(now):
            try:
                if self._attempt_signal(s, now):
                    opened += 1
            except Exception:
                log("ERROR retry signal:\n" + traceback.format_exc())
        for s in sigs:
            try:
                if self._attempt_signal(s, now):
                    opened += 1
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
        alerts, _ = self._tail_source("alerts")
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
            self.reconcile_onchain(now)
        except Exception:
            log("ERROR reconcile:\n" + traceback.format_exc())
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
