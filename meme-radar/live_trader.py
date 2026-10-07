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
  - kill switch: file STOP_LIVE trong thu muc nay -> dung nhe nhang
    (file STOP la cua radar paper, live trader chi canh bao, khong dung).
    CHU Y: STOP_LIVE KHONG tu dong dong vi the dang mo — phai xu ly tay.
  - PAUSE: ngung mo moi, van quan ly exit cac vi the dang mo.
  - daily stop: dung mo moi khi lo thuc te trong ngay < -daily_stop_pct
  - khong bao gio log private key

Chay:  .venv/bin/python live_trader.py   (doc config.live.json)
"""
import base64
import json
import math
import os
import re
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

from dexscreener import pick_pair
from strategy import decide_exits
from txparse import parse_tx

BASE = os.path.dirname(os.path.abspath(__file__))

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_MINTS = {USDC_MINT, USDT_MINT}
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

SIG_P = os.path.join(BASE, "signals.jsonl")
ALERT_P = os.path.join(BASE, "alerts.jsonl")
POS_P = os.path.join(BASE, "live_positions.json")
STATE_P = os.path.join(BASE, "live_state.json")
TRADES_P = os.path.join(BASE, "live_trades.jsonl")
LOG_P = os.path.join(BASE, "live_trader.log")
# Kill switch RIENG cho live trader. File STOP chung thu muc la cua radar.py
# (paper) — truoc day ca 2 cung doc STOP nen dung 1 cai la tat ca 2.
STOP_P = os.path.join(BASE, "STOP_LIVE")
RADAR_STOP_P = os.path.join(BASE, "STOP")
PAUSE_P = os.path.join(BASE, "PAUSE")
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
    # Ke toan P&L tu chinh tx (getTransaction: so du truoc/sau + phi) thay
    # vi getBalance sau confirm (node RPC lag -> so cu -> proceeds sai).
    # Khong doc duoc tx -> dung so du nhu cu.
    "tx_accounting": True,
    "tx_accounting_attempts": 3,
    "tx_accounting_wait_seconds": 1.0,
    "max_swap_retries": 3,
    "fee_buffer_sol": 0.02,
    "daily_stop_pct": 0.20,
    "min_signal_usd": 300,
    # Bo qua signal cu hon N giay (tinh tu thoi diem vi nguon giao dich,
    # fallback detected_at). Ap dung ca khi retry va backlog sau restart.
    "max_signal_age_seconds": 120,
    # Intent BUY cua bot ma vi khong co token va tx khong land/khong ton tai
    # sau N giay (blockhash Solana het han ~60-90s) -> bo, go block entry.
    "pending_buy_expire_seconds": 180,
    "skip_preflight": False,
    # duong dan signal: de trong -> dung file trong thu muc module.
    # Khi live_trader chay tren VPS con radar paper chay may khac,
    # tro 2 truong nay sang thu muc nhan signal forward, vd:
    # "signals_jsonl": "/home/ubuntu/muse_bot/live-signals/signals.jsonl"
    "signals_jsonl": "",
    "alerts_jsonl": "",
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
    # ---- Jupiter: lite-api dang bi giam rate dan roi khai tu. Co API key
    # (env JUPITER_API_KEY hoac file .jupiter_key, chmod 600) -> tu chuyen
    # sang api.jup.ag. Keyless api.jup.ag chi 0.5 req/s -> dat
    # jupiter_min_interval_seconds = 2.
    "jupiter_api_key_file": ".jupiter_key",
    "jupiter_min_interval_seconds": 0.0,
    # Jupiter tra 401 voi key (key sai/het han/bi thu hoi) -> CRITICAL 1 lan,
    # gui lai NGAY request do len jupiter_fallback_base KHONG key (de lenh ban
    # khong ket), thu lai key sau jupiter_key_retry_seconds. "" = tat.
    "jupiter_fallback_base": "https://lite-api.jup.ag",
    "jupiter_key_retry_seconds": 1800,
    # ---- (1) thu hoi rent token account (~0.00204 SOL/token) sau khi ban
    # sach. Chi dong account so du 0 (on-chain cung tu choi neu con token).
    "reclaim_rent": True,
    "reclaim_max_per_loop": 2,
    "reclaim_retry_seconds": 30,
    "reclaim_give_up_seconds": 900,
    # ---- (2) loc token nguy hiem truoc khi mua (fail closed)
    "token_safety": True,
    "reject_freeze_authority": True,
    "reject_mint_authority": True,
    # Extension Token-2022 bi tu choi (ten theo jsonParsed). transferFeeConfig
    # chi tu choi khi phi > 0; defaultAccountState chi khi = frozen;
    # transferHook chi khi co programId; permanentDelegate khi co delegate.
    "reject_token2022_extensions": [
        "transferFeeConfig", "transferHook", "permanentDelegate",
        "nonTransferable", "defaultAccountState", "pausableConfig",
    ],
    # Quote thu ban lai luong token se nhan: lo khu hoi > N% -> bo (pool
    # mong / thue / honeypot). 0 = tat.
    "max_round_trip_loss_pct": 6.0,
    # ---- (3) chong mua duoi: gia minh > gia vi nguon khop (tu tx) qua N%
    # -> bo. 0 = tat. Signal khong co wallet_price_usd -> bo qua kiem tra.
    "max_entry_premium_pct": 20.0,
    # ---- (4) thoat khan cap: SL/TRAIL/SMART_EXIT/COPY_EXIT/TIME ban that
    # bai -> lan sau dung bac ke tiep (lan dau dung sell_slippage_bps /
    # sell_max_price_impact_pct). priority_fee: null = nhu priority_fee_lamports.
    "exit_escalation": [
        {"slippage_bps": 1000, "max_impact_pct": 30.0,
         "priority_fee": {"auto": True, "max_lamports": 200000,
                          "level": "veryHigh"}},
        {"slippage_bps": 2500, "max_impact_pct": 60.0,
         "priority_fee": {"auto": True, "max_lamports": 1000000,
                          "level": "veryHigh"}},
    ],
    # ---- (5) gia: Jupiter Price API v3 lay 1 request cho moi vi the (<=50).
    # Token Price API bo qua (khong du tin cay) -> quote tung token, gian
    # cach price_fallback_seconds.
    "price_batch": True,
    "price_fallback_seconds": 20,
    # ---- (6) copy exit: vi nguon (vi minh copy) ban >= N% luong dang giu
    # (cong don nhieu lenh) -> ban het. Nguon: alert wallet_sell cua radar.
    "copy_exit": True,
    "copy_exit_min_sold_frac": 0.5,
}

# Lenh thoat bat buoc (cat lo / bao ve): duoc nang bac slippage khi that bai.
MUST_EXIT_WHYS = ("SL", "TRAIL", "SMART_EXIT", "COPY_EXIT", "TIME")


def token_risk_reasons(mint_value, cfg):
    """Ly do tu choi token tu getAccountInfo(mint, jsonParsed)['value'].

    [] = an toan theo cac kiem tra cau hinh. Khong doc duoc cau truc ->
    ['unparsed_mint'] (fail closed)."""
    try:
        parsed = mint_value["data"]["parsed"]
        info = parsed["info"]
    except (KeyError, TypeError):
        return ["unparsed_mint"]
    if parsed.get("type") not in (None, "mint"):
        return ["not_a_mint"]
    out = []
    if cfg.get("reject_freeze_authority", True) and info.get("freezeAuthority"):
        out.append("freeze_authority")
    if cfg.get("reject_mint_authority", True) and info.get("mintAuthority"):
        out.append("mint_authority")
    deny = set(cfg.get("reject_token2022_extensions") or [])
    for ext in info.get("extensions") or []:
        if not isinstance(ext, dict):
            continue
        name = ext.get("extension")
        if name not in deny:
            continue
        state = ext.get("state") or {}
        if name == "transferFeeConfig":
            bps = max(int((state.get(k) or {}).get("transferFeeBasisPoints")
                          or 0)
                      for k in ("olderTransferFee", "newerTransferFee"))
            if bps <= 0:
                continue
            out.append(f"transfer_fee_{bps}bps")
        elif name == "transferHook":
            if state.get("programId"):
                out.append("transfer_hook")
        elif name == "permanentDelegate":
            if state.get("delegate"):
                out.append("permanent_delegate")
        elif name == "defaultAccountState":
            if str(state.get("accountState")).lower() == "frozen":
                out.append("default_frozen")
        else:
            out.append(name)
    return out


def exit_tier_params(cfg, tier):
    """(slippage_bps, max_impact_pct, priority_fee|None) cho bac thoat."""
    base = (int(cfg.get("sell_slippage_bps", cfg["slippage_bps"])),
            float(cfg.get("sell_max_price_impact_pct",
                          cfg["max_price_impact_pct"])), None)
    tiers = cfg.get("exit_escalation") or []
    if tier <= 0 or not tiers:
        return base
    t = tiers[min(tier, len(tiers)) - 1] or {}
    return (int(t.get("slippage_bps", base[0])),
            float(t.get("max_impact_pct", base[1])),
            t.get("priority_fee"))


def load_config(path=CFG_P):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        cfg.update(json.load(open(path)))
    return cfg


# ---------------------------------------------------------------- log

_SECRET_PATTERNS = [
    # api-key trong URL/query (Helius, ...): ?api-key=XXX / &api_key=XXX
    re.compile(r"(?i)(api[-_]?key=)[^&\s'\"<>]+"),
]
_SECRET_VALUES = set()


def register_secret(value):
    """Dang ky 1 gia tri bi mat (vd. Helius key) de redact o moi noi."""
    if value and len(value) >= 8:
        _SECRET_VALUES.add(value)


def redact(text):
    """Che secret trong chuoi bat ky truoc khi log/luu state."""
    text = str(text)
    for v in _SECRET_VALUES:
        text = text.replace(v, "***")
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1***", text)
    return text


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}Z] {redact(msg)}"
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


def signal_event_ts(signal):
    """Thoi diem su kien cua signal (unix giay): uu tien `ts` (blockTime /
    luc ws nhan), fallback `detected_at`. Tu nhan dien ms. None neu khong co."""
    for key in ("ts", "detected_at"):
        try:
            v = float(signal.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if v > 1e12:  # milliseconds
            v /= 1000.0
        if v > 0:
            return v
    return None


def signal_age_seconds(signal, now):
    """Tuoi signal (giay). Khong co timestamp -> inf (fail closed)."""
    ts = signal_event_ts(signal)
    if ts is None:
        return float("inf")
    return max(0.0, now - ts)


def pick_dexscreener_price(rows, mint):
    """Gia USD cua `mint` tu danh sach pair DexScreener (xem
    dexscreener.pick_pair: uu tien pair mint la base, thanh khoan cao nhat;
    khong co thi suy tu pair mint la quote; khong suy duoc -> None)."""
    p = pick_pair(rows, mint)
    return p["price"] if p else None


def price_impact_pct(quote):
    """Price impact theo PHAN TRAM (5.0 = 5%) tu Jupiter quote.

    Jupiter /swap/v1/quote tra `priceImpactPct` dang PHAN SO (0.0042 =
    0.42%), khong phai phan tram -> nhan 100 de so voi max_price_impact_pct
    (cau hinh theo %). Am (gia co loi) -> 0. Khong doc duoc -> inf (fail
    closed: bo qua lenh thay vi bo qua guard).
    """
    try:
        frac = float(quote.get("priceImpactPct"))
    except (TypeError, ValueError, AttributeError):
        return float("inf")
    if frac != frac:  # NaN
        return float("inf")
    return max(0.0, frac * 100.0)




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
        try:
            r = requests.post(self.url, json={
                "jsonrpc": "2.0", "id": self._id,
                "method": method, "params": params,
            }, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
        except requests.RequestException as e:
            # Message cua requests chua full URL (?api-key=...) -> redact,
            # `from None` de traceback khong in lai exception goc.
            raise RpcError(f"{method}: {type(e).__name__}: "
                           f"{redact(e)[:200]}") from None
        except ValueError as e:
            raise RpcError(f"{method}: JSON khong hop le: {redact(e)[:200]}") from None
        if d.get("error"):
            raise RpcError(redact(d["error"])[:200])
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

    def get_mint_info(self, mint, max_age=300):
        """getAccountInfo(mint, jsonParsed)['value'] (cache max_age giay).
        Mint khong ton tai -> RpcError (fail closed)."""
        cache = self.__dict__.setdefault("_mint_info_cache", {})
        hit = cache.get(mint)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        res = self.call("getAccountInfo",
                        [mint, {"encoding": "jsonParsed",
                                "commitment": "confirmed"}])
        value = (res or {}).get("value")
        if not value:
            raise RpcError(f"mint {mint[:10]}... khong ton tai")
        cache[mint] = (time.time(), value)
        try:
            self._decimals_cache[mint] = int(
                value["data"]["parsed"]["info"]["decimals"])
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        return value

    def get_token_accounts_for_mint(self, owner, mint):
        """Moi token account cua owner cho mint (ca SPL va Token-2022):
        [{pubkey, program, lamports, amount}]."""
        res = self.call("getTokenAccountsByOwner",
                        [owner, {"mint": mint},
                         {"encoding": "jsonParsed", "commitment": "confirmed"}])
        out = []
        for item in res.get("value", []):
            acct = item.get("account") or {}
            info = acct["data"]["parsed"]["info"]
            out.append({
                "pubkey": item["pubkey"],
                "program": acct.get("owner"),
                "lamports": int(acct.get("lamports") or 0),
                "amount": int(info["tokenAmount"]["amount"]),
            })
        return out

    def get_latest_blockhash(self):
        res = self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return res["value"]["blockhash"]

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

    def get_transaction(self, sig):
        """Tx da confirm (jsonParsed) hoac None neu node chua index."""
        return self.call("getTransaction", [sig, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0}])


# ---------------------------------------------------------------- Jupiter


class NoRoute(Exception):
    pass


class SwapError(Exception):
    pass


class EntryRejected(SwapError):
    """Tu choi mua TRUOC khi gui bat ky tx nao (token nguy hiem, mua duoi,
    lo khu hoi). Khong retry: signal danh dau skipped_<reason>."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class SwapUncertain(SwapError):
    """A transaction may have landed; never blindly retry this operation."""


class JupiterClient:
    def __init__(self, base="https://lite-api.jup.ag", timeout=20,
                 http_get=None, http_post=None, api_key=None,
                 min_interval=0.0, fallback_base=None,
                 key_retry_seconds=1800, clock=None):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._get = http_get or requests.get
        self._post = http_post or requests.post
        self._sol_usd = (0, 0.0)
        self.api_key = api_key or None
        self.min_interval = float(min_interval or 0.0)
        self._last_req = 0.0
        # 401 voi key -> chay tam khong key tren fallback_base
        self.fallback_base = (fallback_base or "").rstrip("/") or None
        self.key_retry_seconds = float(key_retry_seconds or 0)
        self._clock = clock or time.time
        self.key_rejected_at = 0.0       # >0: key dang bi Jupiter tu choi
        self.key_reject_count = 0

    def _key_active(self):
        """Co dung key cho request nay khong (het han cho -> thu lai key)."""
        if not getattr(self, "api_key", None):
            return False
        if not getattr(self, "key_rejected_at", 0):
            return True
        if self.key_retry_seconds > 0 and \
                self._clock() - self.key_rejected_at >= self.key_retry_seconds:
            log("Jupiter: thu lai API key sau %.0f phut chay khong key"
                % ((self._clock() - self.key_rejected_at) / 60))
            self.key_rejected_at = 0.0
            return True
        return False

    def _headers(self):
        key = getattr(self, "api_key", None)
        if key and getattr(self, "key_rejected_at", 0):
            return None
        return {"x-api-key": key} if key else None

    def _url(self, path, keyed):
        if keyed or not getattr(self, "key_rejected_at", 0) or \
                not getattr(self, "fallback_base", None):
            return f"{self.base}{path}"
        return f"{self.fallback_base}{path}"

    def _request(self, method, path, **kw):
        """GET/POST qua 1 cho: 401 khi dang dung key -> bo key, gui lai ngay
        len fallback_base khong key. An toan cho /swap: 401 = Jupiter chua
        dung tx nao (va endpoint chi dung tx, khong gui len chain)."""
        fn = self._get if method == "GET" else self._post
        keyed = self._key_active()
        self._throttle()
        r = fn(self._url(path, keyed), timeout=self.timeout,
               headers={"x-api-key": self.api_key} if keyed else None, **kw)
        if keyed and getattr(r, "status_code", 200) == 401 and \
                getattr(self, "fallback_base", None):
            self.key_rejected_at = self._clock()
            self.key_reject_count += 1
            retry_min = self.key_retry_seconds / 60
            if self.key_reject_count == 1:
                log("CRITICAL Jupiter tu choi API key (401) -> chay KHONG key "
                    f"tren {self.fallback_base}, thu lai key sau "
                    f"{retry_min:.0f} phut. Thay key: env JUPITER_API_KEY "
                    "(.env goc) hoac .jupiter_key, tao tai portal.jup.ag")
            else:
                log(f"Jupiter: key van bi tu choi (401, lan "
                    f"{self.key_reject_count}) -> tiep tuc khong key, thu lai "
                    f"sau {retry_min:.0f} phut")
            self._throttle()
            r = fn(self._url(path, False), timeout=self.timeout, headers=None,
                   **kw)
        return r

    def _throttle(self):
        """Gian cach toi thieu giua 2 request (keyless api.jup.ag 0.5 rps)."""
        gap = float(getattr(self, "min_interval", 0.0) or 0.0)
        if gap <= 0:
            return
        wait = getattr(self, "_last_req", 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def quote(self, in_mint, out_mint, amount_base, slippage_bps):
        r = self._request("GET", "/swap/v1/quote", params={
            "inputMint": in_mint, "outputMint": out_mint,
            "amount": str(int(amount_base)),
            "slippageBps": str(int(slippage_bps)),
        })
        r.raise_for_status()
        q = r.json()
        if not isinstance(q, dict) or "outAmount" not in q:
            raise NoRoute(f"no route: {str(q)[:150]}")
        return q

    def prices_usd(self, mints):
        """Jupiter Price API v3: {mint: usdPrice} cho toi da 50 mint/request.
        Token khong co gia tin cay bi Jupiter BO KHOI response (khong co
        key) -> khong co trong ket qua."""
        out = {}
        mints = [m for m in dict.fromkeys(mints) if m]
        for i in range(0, len(mints), 50):
            chunk = mints[i:i + 50]
            r = self._request("GET", "/price/v3",
                              params={"ids": ",".join(chunk)})
            r.raise_for_status()
            d = r.json() or {}
            for m in chunk:
                try:
                    px = float((d.get(m) or {}).get("usdPrice"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if px > 0 and math.isfinite(px):
                    out[m] = px
        return out

    def swap_tx(self, quote, user_pubkey, priority_fee):
        r = self._request("POST", "/swap/v1/swap", json={
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": priority_fee,
        })
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
        return pick_dexscreener_price(r.json(), mint)


# ---------------------------------------------------------------- swapper


class Swapper:
    def __init__(self, rpc, jup, keypair, cfg, dry_run):
        self.rpc = rpc
        self.jup = jup
        self.kp = keypair
        self.cfg = cfg
        self.dry = dry_run
        self.pubkey = str(keypair.pubkey()) if keypair else None

    def _priority_fee(self, override=None):
        pf = self.cfg["priority_fee_lamports"] if override is None else override
        if isinstance(pf, dict) and pf.get("auto"):
            return {"priorityLevelWithMaxLamports": {
                "maxLamports": pf.get("max_lamports", 1000000),
                "priorityLevel": pf.get("level", "veryHigh")}}
        return int(pf)

    def _sign_and_send(self, swap_b64, on_signed=None):
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
        signed = VersionedTransaction(tx.message, [self.kp])
        raw_b64 = base64.b64encode(bytes(signed)).decode()
        if on_signed is not None:
            # Signature = chu ky dau tien, biet TRUOC khi gui. Ghi ben vung
            # truoc sendTransaction: neu send timeout/crash sau khi validator
            # da nhan, reconcile van tra duoc tx that cua bot.
            on_signed(str(signed.signatures[0]))
        return self.rpc.send_transaction(raw_b64, self.cfg["skip_preflight"])

    def close_token_accounts(self, accounts):
        """Gui 1 tx CloseAccount (SPL/Token-2022, instruction 9) cho cac token
        account so du 0 -> rent ve vi. Tra ve signature (biet truoc khi gui).
        On-chain tu choi neu account con token, nen khong the mat token."""
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction
        if self.dry or self.kp is None:
            raise SwapError("close account chi chay o mode live")
        owner = self.kp.pubkey()
        ixs = []
        for a in accounts:
            if int(a.get("amount", 1)) != 0:
                raise SwapError(f"account {a.get('pubkey')} con token -> "
                                "khong dong")
            if a.get("program") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
                raise SwapError(f"program la {a.get('program')} -> khong dong")
            ixs.append(Instruction(
                Pubkey.from_string(a["program"]), bytes([9]),
                [AccountMeta(Pubkey.from_string(a["pubkey"]), False, True),
                 AccountMeta(owner, False, True),
                 AccountMeta(owner, True, False)]))
        if not ixs:
            raise SwapError("khong co account de dong")
        blockhash = self.rpc.get_latest_blockhash()
        msg = MessageV0.try_compile(owner, ixs, [], Hash.from_string(blockhash))
        tx = VersionedTransaction(msg, [self.kp])
        sig = str(tx.signatures[0])
        self.rpc.send_transaction(base64.b64encode(bytes(tx)).decode(),
                                  self.cfg["skip_preflight"])
        return sig

    def _tx_accounting(self, sig, mint):
        """So lieu THAT cua vi tu tx (txparse.parse_tx): sol_delta (da gom
        phi), fee, fee_payer, token_delta, rent_open/close. None -> caller
        dung so du getBalance nhu cu (rpc khong ho tro / loi / chua index /
        tx loi). Chi la ke toan: KHONG bao gio quyet dinh tx land hay chua."""
        if not sig or not self.pubkey or not self.cfg.get("tx_accounting",
                                                          True):
            return None
        fn = getattr(self.rpc, "get_transaction", None)
        if fn is None:
            return None
        attempts = max(1, int(self.cfg.get("tx_accounting_attempts", 3)))
        wait = float(self.cfg.get("tx_accounting_wait_seconds", 1.0))
        for i in range(attempts):
            try:
                tx = fn(sig)
            except Exception as e:
                log(f"ke toan tx {sig[:12]}...: loi doc tx "
                    f"({redact(e)[:120]}) -> dung so du")
                return None
            if tx:
                try:
                    p = parse_tx(tx, self.pubkey, mint)
                except Exception as e:
                    log(f"ke toan tx {sig[:12]}...: parse loi ({e}) "
                        "-> dung so du")
                    return None
                return p if p and p["ok"] else None
            if i + 1 < attempts:
                time.sleep(wait)
        log(f"ke toan tx {sig[:12]}...: node chua tra tx -> dung so du")
        return None

    @staticmethod
    def _fee_usd(acct, sol_usd):
        if not acct or not acct.get("fee_payer"):
            return None
        return round(acct["fee"] / 1e9 * sol_usd, 6)

    def _buy_signed(self, sig):
        hook = getattr(self, "on_buy_sent", None)
        if hook is not None:
            hook(sig)

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
                    token_delta, dec, mint=None, had_account=True,
                    extra=None):
        if dec is None or token_delta <= 0:
            raise SwapUncertain(
                f"BUY {symbol} verify khong co token delta hop le")
        measured = True
        acct = self._tx_accounting(sig, mint) if mint else None
        if acct and (acct["token_delta"] <= 0 or acct["sol_delta"] >= 0):
            acct = None   # tx khong giong 1 lenh mua -> dung so du
        try:
            if acct:
                spent_usd = -acct["sol_delta"] / 1e9 * sol_usd
            else:
                bal_after = self.rpc.get_balance_lamports(self.pubkey)
                spent_usd = max(bal_before - bal_after, 0) / 1e9 * sol_usd
        except Exception as e:
            # Token delta is the authoritative execution proof; SOL P&L is
            # only accounting, so retain a conservative cost fallback.
            spent_usd = size_usd
            measured = False
            log(f"BUY {symbol}: khong doc duoc SOL balance sau tx ({e}); "
                f"dung cost=${size_usd:.2f}")
        if spent_usd <= 0:
            spent_usd = size_usd
            measured = False
        # Rent token account moi (lan dau mua token nay) nam trong SOL da chi
        # nhung KHONG phai gia token va duoc lay lai khi dong account -> tach
        # ra khoi gia vao (truoc day entry bi doi ~3-4% voi lenh $10).
        rent_lamports = 0
        if acct:
            rent_lamports = int(acct["rent_open"])
        elif measured and not had_account and mint:
            try:
                accts = self.rpc.get_token_accounts_for_mint(self.pubkey, mint)
                rent_lamports = sum(int(a.get("lamports") or 0) for a in accts)
            except Exception as e:
                log(f"BUY {symbol}: khong doc duoc rent token account ({e}) "
                    "-> tinh vao gia vao nhu cu")
        rent_usd = rent_lamports / 1e9 * sol_usd
        if rent_usd >= spent_usd:
            rent_lamports, rent_usd = 0, 0.0
        cost_usd = spent_usd - rent_usd
        entry_usd = cost_usd / (token_delta / (10 ** dec))
        log(f"LIVE BUY {symbol} OK nhan {token_delta/(10**dec):.4f} token "
            f"@{entry_usd:.8f} (rent {rent_lamports/1e9:.5f} SOL tach rieng) "
            f"tx={(sig or 'unknown')[:12]}...")
        out = {"tokens_base": token_delta, "decimals": dec,
               "cost_usd": round(cost_usd, 4), "entry_usd": entry_usd,
               "rent_lamports": rent_lamports,
               "rent_usd": round(rent_usd, 4),
               "fee_usd": self._fee_usd(acct, sol_usd),
               "acct": "tx" if acct else "balance",
               "tx": sig, "dry": False}
        out.update(extra or {})
        return out

    def _entry_safety(self, mint, symbol):
        """Kiem tra mint qua RPC TRUOC khi quote. Tra ve decimals (hoac None
        neu tat kiem tra). Khong doc duoc -> SwapError (retry, KHONG mua)."""
        if not self.cfg.get("token_safety", True):
            return None
        try:
            info = self.rpc.get_mint_info(mint)
        except Exception as e:
            raise SwapError(f"khong doc duoc mint {symbol} de kiem tra an "
                            f"toan: {redact(e)[:150]}")
        reasons = token_risk_reasons(info, self.cfg)
        if reasons:
            raise EntryRejected("unsafe_token", ",".join(reasons))
        try:
            return int(info["data"]["parsed"]["info"]["decimals"])
        except (KeyError, TypeError, ValueError):
            return None

    def _entry_quote_check(self, mint, symbol, lamports, sol_usd, dec,
                           ref_price_usd, report):
        """Ham kiem tra quote mua (chay ngay truoc khi build tx):
        - chong mua duoi: gia minh vs gia vi nguon khop;
        - quote thu ban lai: khong co route / lo khu hoi qua nguong -> bo."""
        def check(q):
            out = int(q.get("outAmount") or 0)
            if out <= 0:
                raise SwapError("quote mua outAmount = 0")
            d = dec
            if d is None:
                try:
                    d = self.rpc.get_mint_decimals(mint)
                except Exception:
                    d = None   # khong co decimals -> bo qua kiem tra gia
            my_px = ((lamports / 1e9 * sol_usd) / (out / 10 ** d)
                     if d is not None else None)
            report["quote_price_usd"] = my_px
            maxp = float(self.cfg.get("max_entry_premium_pct") or 0)
            if my_px and ref_price_usd and ref_price_usd > 0:
                prem = (my_px / ref_price_usd - 1) * 100
                report["wallet_price_usd"] = ref_price_usd
                report["premium_pct"] = round(prem, 2)
                if maxp > 0 and prem > maxp:
                    raise EntryRejected(
                        "chase", f"{symbol} gia ~{my_px:.4g} cao hon vi nguon "
                        f"{ref_price_usd:.4g} {prem:+.1f}% > {maxp:g}%")
            maxrt = float(self.cfg.get("max_round_trip_loss_pct") or 0)
            if maxrt > 0:
                slip = int(self.cfg.get("sell_slippage_bps",
                                        self.cfg["slippage_bps"]))
                try:
                    q2 = self.jup.quote(mint, SOL_MINT, out, slip)
                except NoRoute as e:
                    raise EntryRejected("no_sell_route", str(e)[:120])
                except requests.HTTPError as e:
                    code = getattr(getattr(e, "response", None),
                                   "status_code", None)
                    if code == 400:
                        raise EntryRejected("no_sell_route", str(e)[:120])
                    raise SwapError(f"quote ban thu loi: {e}")
                except Exception as e:
                    raise SwapError(f"quote ban thu loi: {e}")
                back = int(q2.get("outAmount") or 0)
                loss = (1 - back / lamports) * 100 if lamports > 0 else 100.0
                report["round_trip_loss_pct"] = round(loss, 2)
                if loss > maxrt:
                    raise EntryRejected(
                        "round_trip", f"{symbol} lo khu hoi {loss:.1f}% > "
                        f"{maxrt:g}% (pool mong/thue/honeypot)")
        return check

    def _quote_swap(self, in_mint, out_mint, amount_base,
                    slippage_bps=None, max_price_impact_pct=None,
                    check=None, priority_fee=None):
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
            pi = price_impact_pct(q)
            if pi > max_price_impact_pct:
                raise SwapError(f"price impact {pi:.2f}% > max "
                                f"{max_price_impact_pct}% -> skip")
            if check is not None:
                # Chua gui gi: moi loi o day la loi "chac chan khong mua"
                # (SwapError -> retry), KHONG duoc thanh SwapUncertain/block.
                try:
                    check(q)
                except SwapError:
                    raise
                except Exception as e:
                    raise SwapError(f"kiem tra quote loi: {e}")
            try:
                txb64 = self.jup.swap_tx(q, self.pubkey,
                                         self._priority_fee(priority_fee))
            except Exception as e:
                last = e
                time.sleep(1)
                continue
            return q, txb64
        raise SwapError(f"quote/swap failed sau {self.cfg['max_swap_retries']} "
                        f"lan thu: {last}")

    def execute_buy(self, mint, size_usd, symbol="?", ref_price_usd=None):
        """Mua token bang SOL tri gia size_usd. Tra ve dict ket qua.
        ref_price_usd: gia vi nguon khop (chong mua duoi)."""
        try:
            sol_usd = self.jup.sol_price_usd()
            if not sol_usd or sol_usd <= 0:
                raise ValueError("SOL price khong hop le")
        except Exception as e:
            # No transaction has been submitted yet, so the signal may retry.
            raise SwapError(f"khong lay duoc SOL price truoc BUY: {e}")
        lamports = usd_to_lamports(size_usd, sol_usd)
        dec_safe = self._entry_safety(mint, symbol)
        report = {}
        check = self._entry_quote_check(mint, symbol, lamports, sol_usd,
                                        dec_safe, ref_price_usd, report)
        if self.dry:
            q = self.jup.quote(SOL_MINT, mint, lamports,
                               self.cfg["slippage_bps"])
            pi = price_impact_pct(q)
            if pi > self.cfg["max_price_impact_pct"]:
                raise SwapError(f"price impact {pi:.2f}% > max "
                                f"{self.cfg['max_price_impact_pct']}% -> skip")
            check(q)
            dec = (dec_safe if dec_safe is not None
                   else self.rpc.get_mint_decimals(mint))
            tokens = int(q["outAmount"]) / (10 ** dec)
            price = size_usd / tokens if tokens > 0 else 0
            log(f"DRY_RUN BUY {symbol} ${size_usd:.2f} -> {tokens:.4f} token "
                f"@~${price:.8f} (khong gui tx)")
            out = {"tokens_base": int(q["outAmount"]), "decimals": dec,
                   "cost_usd": size_usd, "entry_usd": price, "tx": None,
                   "dry": True}
            out.update(report)
            return out
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
        had_account = dec_before is not None
        q, txb64 = self._quote_swap(SOL_MINT, mint, lamports, check=check)
        try:
            sig = self._sign_and_send(txb64, on_signed=self._buy_signed)
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
                    dec if dec is not None else dec_before, mint=mint,
                    had_account=had_account, extra=report)
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
                dec if dec is not None else dec_before, mint=mint,
                had_account=had_account, extra=report)
        if verify_error:
            raise SwapUncertain(
                f"BUY {symbol} status={confirm_error or 'timeout'}; "
                f"verify token loi: {verify_error}")
        if confirm_error is not None and not status_unknown:
            raise confirm_error
        raise SwapUncertain(
            f"BUY {symbol} unconfirmed/unknown {sig[:12]} (da verify, "
            "khong thay token)")

    def execute_sell(self, mint, frac, symbol="?", tier=0):
        """Ban frac so token DANG CO tren vi. Tra ve dict ket qua.
        tier >= 1: bac thoat khan cap (slippage/impact/phi uu tien cao hon,
        xem exit_escalation)."""
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
        sell_slippage, sell_impact, sell_fee = exit_tier_params(
            self.cfg, int(tier or 0))
        q, txb64 = self._quote_swap(
            mint, SOL_MINT, amount,
            slippage_bps=sell_slippage,
            max_price_impact_pct=sell_impact,
            priority_fee=sell_fee,
        )
        # Token da roi vi it nhat ~90% luong ban -> coi la tx da land.
        landed_below = bal_base - amount * 0.9

        def _unconfirmed_result(sig_, nb_, why_):
            # Co chu ky -> thu doc tx: neu node da co thi dung so THAT thay
            # vi uoc tinh theo nguong slippage (luon thap hon thuc te).
            a_ = self._tx_accounting(sig_, mint) if sig_ else None
            if a_ and a_["token_delta"] < 0:
                got = max(a_["sol_delta"], 0) / 1e9 * sol_usd
                log(f"LIVE SELL {symbol} {why_} nhung tx da co tren chain "
                    f"-> so that +${got:.2f}")
                return {"sold_base": -a_["token_delta"],
                        "proceeds_usd": round(got, 4), "tx": sig_,
                        "dry": False, "acct": "tx",
                        "fee_usd": self._fee_usd(a_, sol_usd)}
            est = int(q.get("otherAmountThreshold", 0)) / 1e9 * sol_usd
            log(f"LIVE SELL {symbol} {why_} nhung token da di "
                f"-> tinh theo threshold ~${est:.2f} (CANH BAO)")
            return {"sold_base": bal_base - nb_, "proceeds_usd": round(est, 4),
                    "tx": sig_, "dry": False, "unconfirmed": True}

        try:
            sig = self._sign_and_send(txb64)
        except Exception as e:
            # sendTransaction co the timeout SAU khi validator da nhan tx.
            # Khong duoc coi la that bai (retry se ban trung) -> kiem tra so du.
            attempts = max(1, int(self.cfg.get("buy_balance_verify_attempts", 4)))
            delay = float(self.cfg.get("buy_balance_verify_seconds", 2))
            for i in range(attempts):
                try:
                    nb, _ = self.rpc.get_token_balance_base(self.pubkey, mint)
                    if nb <= landed_below:
                        return _unconfirmed_result(None, nb, "send exception")
                except Exception:
                    pass
                if i + 1 < attempts:
                    time.sleep(delay)
            raise SwapUncertain(
                f"SELL {symbol} send exception, chua thay token giam: {e}")
        log(f"LIVE SELL {symbol} {frac:.0%} tx={sig[:12]}... cho confirm "
            f"(slippage={sell_slippage}bps, impact<={sell_impact:g}%"
            f"{f', bac thoat {tier}' if tier else ''})")
        confirmed = self._confirm(sig)
        if not confirmed:
            # Do not retry blindly: inspect the actual token balance first.
            try:
                nb, _ = self.rpc.get_token_balance_base(self.pubkey, mint)
            except Exception as e:
                raise SwapUncertain(
                    f"SELL {symbol} timeout, khong doc duoc balance: {e}")
            if nb <= landed_below:
                return _unconfirmed_result(sig, nb, "timeout")
            raise SwapUncertain(f"sell unconfirmed: {sig[:12]} (se reconcile)")
        acct = self._tx_accounting(sig, mint)
        if acct and acct["token_delta"] < 0:
            sold_base = -acct["token_delta"]
            proceeds_usd = max(acct["sol_delta"], 0) / 1e9 * sol_usd
        else:
            acct = None
            try:
                sol_after = self.rpc.get_balance_lamports(self.pubkey)
                nb, _ = self.rpc.get_token_balance_base(self.pubkey, mint)
            except Exception as e:
                raise SwapUncertain(
                    f"SELL {symbol} da confirm nhung khong doc duoc "
                    f"balance: {e}")
            sold_base = max(bal_base - nb, 0)
            if sold_base <= 0:
                raise SwapUncertain(
                    f"SELL {symbol} da confirm nhung token balance khong "
                    "giam")
            proceeds_usd = max(sol_after - sol_before, 0) / 1e9 * sol_usd
        log(f"LIVE SELL {symbol} OK +${proceeds_usd:.2f} tx={sig[:12]}..."
            f"{'' if acct else ' (so du)'}")
        return {"sold_base": sold_base, "proceeds_usd": round(proceeds_usd, 4),
                "tx": sig, "dry": False, "acct": "tx" if acct else "balance",
                "fee_usd": self._fee_usd(acct, sol_usd)}


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
        self.jup = jup or self._make_jupiter(cfg)
        helius_key = ""
        kp_path = os.path.join(BASE, cfg["helius_key_file"])
        if os.path.exists(kp_path):
            helius_key = open(kp_path).read().strip()
        register_secret(helius_key)
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
        }
        self.state = {
            "sig_offset": 0, "alert_offset": 0,
            "processed": [],
            "signal_failures": {}, "pending_buys": {},
            "source_files": {}, "daily": {}, **st,
        }
        self.entry_blocked = False
        self.onchain_tokens = set()
        self.unmanaged_tokens = set()
        self._last_reconcile = 0
        self._clock = time.monotonic  # injectable cho test
        self._init_source_offsets(not bool(st))
        self._price_last = {}
        self._log_source_health()

    @staticmethod
    def _make_jupiter(cfg):
        """JupiterClient voi API key (env JUPITER_API_KEY uu tien, roi file
        jupiter_api_key_file). Co key ma base la lite-api -> api.jup.ag (key
        khong dung duoc tren lite-api, lite-api dang bi khai tu)."""
        key = os.environ.get("JUPITER_API_KEY", "").strip()
        if not key:
            kp = os.path.join(BASE, cfg.get("jupiter_api_key_file")
                              or ".jupiter_key")
            if os.path.exists(kp):
                key = open(kp).read().strip()
        base = cfg["jupiter_base"]
        if key:
            register_secret(key)
            if "lite-api.jup.ag" in base:
                base = "https://api.jup.ag"
            log(f"Jupiter: dung API key (da che), base={base}")
        elif "lite-api.jup.ag" in base:
            log("CANH BAO Jupiter lite-api dang bi giam rate va se khai tu. "
                "Nen tao API key (developers.jup.ag) -> env JUPITER_API_KEY "
                "hoac file .jupiter_key")
        fb = cfg.get("jupiter_fallback_base", DEFAULTS["jupiter_fallback_base"])
        if not key or (fb or "").rstrip("/") == base.rstrip("/"):
            fb = None                    # khong key / trung base -> vo nghia
        return JupiterClient(
            base, api_key=key or None,
            min_interval=float(cfg.get("jupiter_min_interval_seconds") or 0),
            fallback_base=fb,
            key_retry_seconds=float(cfg.get(
                "jupiter_key_retry_seconds",
                DEFAULTS["jupiter_key_retry_seconds"]) or 0))

    def _init_source_offsets(self, first_start):
        """Bind persisted offsets to the configured files.

        A path change or inode rotation must not silently reuse an offset from
        another machine/file. On first start we intentionally skip historical
        signals; on-chain recovery handles already-held tokens separately.
        """
        key_to_offset = {
            "signals": "sig_offset",
            "alerts": "alert_offset",
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
        return {"signals": "sig_offset", "alerts": "alert_offset"}[key]

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
        for rec in (self.state.get("pending_buys") or {}).values():
            sig_row = (rec or {}).get("signal") or {}
            if sig_row.get("token"):
                known.setdefault(sig_row["token"], sig_row)
        self.onchain_tokens = {
            mint for mint, bal in balances.items()
            if int(bal.get("amount", 0) or 0) > 0
            and mint not in STABLE_MINTS
            and mint != SOL_MINT
        }
        log(f"RECONCILE wallet: {len(self.onchain_tokens)} non-stable token "
            f"balances, tracked={len(self.positions)}")
        changed = False
        unmanaged = []
        pending = self.state.setdefault("pending_buys", {})
        for pos in list(self.positions):
            bal = balances.get(pos.get("token"), {})
            actual = int(bal.get("amount", 0) or 0)
            if actual <= 0:
                log(f"RECONCILE: {pos.get('symbol')} khong con token "
                    "tren vi -> dong local position (ghi live_trades, P&L "
                    "phan con lai KHONG XAC DINH), khong ban lai")
                # Ghi trade record thay vi xoa im lang (truoc day mat lich su
                # + legs da ban). Khong cong gi vao daily (khong biet gia).
                self._close_position(pos, "reconcile_wallet_empty", None, now)
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
                # Kiem tra: co phai airdrop/dust khong?
                # Neu token chua tung xuat hien trong signal history -> co the la airdrop
                # Chi block neu token da tung duoc bot biet (co signal) nhung mat track
                if not signal:
                    log(f"INFO token la tren vi (co the airdrop/dust): "
                        f"{mint[:10]}... amount={amount} -> KHONG block entry")
                    continue
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
        ignored = (self.onchain_tokens - local_tokens) - self.unmanaged_tokens
        log(f"RECONCILE occupancy={self._occupied_token_count()}/"
            f"{self.cfg['max_positions']} (bo qua {len(ignored)} token "
            f"airdrop/dust khong co signal)")
        if self._resolve_pending_buys(balances, now):
            changed = True
        pending_uncertain = bool(pending)
        self.entry_blocked = bool(unmanaged or pending_uncertain)
        # Ghi ly do block de dashboard hien thi
        block_reasons = []
        if pending_uncertain:
            block_reasons.append("pending BUY chua reconcile")
            log("CRITICAL pending BUY intent chua reconcile xong -> block entry")
        if unmanaged:
            block_reasons.append(f"unmanaged tokens: {len(unmanaged)}")
            log(f"CRITICAL unmanaged token(s) tren vi: "
                f"{', '.join(x[:10] + '...' for x in unmanaged)} -> block entry")
        self.state["entry_blocked"] = self.entry_blocked
        self.state["block_reason"] = "; ".join(block_reasons) if block_reasons else None
        self.state["block_since"] = self.state.get("block_since") if self.entry_blocked else None
        if self.entry_blocked and not self.state.get("block_since"):
            import time as _t
            self.state["block_since"] = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime())
        # Luon save khi trang thai block thay doi
        if changed or self.state.get("entry_blocked") != self.entry_blocked:
            self.save()
        elif self.entry_blocked:
            # Cap nhat block status ngay ca khi khong co thay doi khac
            self.save()
        return not self.entry_blocked

    def _resolve_pending_buys(self, balances, now):
        """Giai quyet intent BUY cua bot theo du lieu on-chain.

        Truoc day pending chi duoc go khi token VE vi (position/recover);
        lenh mua KHONG land -> khong duong nao go -> block entry vinh vien.
        - Vi co token: de recover/position xu ly (khong dong o day).
        - Co tx cua bot: failed on-chain -> bo; da confirm ma vi khong co
          token -> GIU block + CRITICAL (can kiem tra tay); khong tim thay
          tx sau pending_buy_expire_seconds (blockhash het han) -> bo.
        - Chua co tx (chua ky/gui, hoac pending cu truoc ban sua nay): qua
          pending_buy_expire_seconds ma vi khong co token -> bo.
        Tra ve True neu state thay doi."""
        pending = self.state.setdefault("pending_buys", {})
        if not pending:
            return False
        expire = float(self.cfg.get("pending_buy_expire_seconds", 180))
        tracked = {str(p.get("signal_tid") or "") for p in self.positions}
        changed = False
        for tid, rec in list(pending.items()):
            rec = rec or {}
            sig_row = rec.get("signal") or {}
            mint = sig_row.get("token")
            symbol = sig_row.get("symbol", "?")
            if tid in tracked:
                pending.pop(tid, None)
                changed = True
                continue
            if mint and int((balances.get(mint) or {}).get("amount", 0)
                            or 0) > 0:
                continue                       # recover xu ly
            age = now - float(rec.get("sent_at") or rec.get("started_at")
                              or now)
            tx = rec.get("tx")
            status = None
            if tx:
                try:
                    status = self.swapper.rpc.get_sig_status(tx)
                except Exception as e:
                    log(f"pending BUY {symbol}: khong doc duoc status tx "
                        f"{tx[:12]}...: {redact(e)[:120]} -> giu")
                    continue
            if status == "failed":
                why = f"tx {tx[:12]}... failed on-chain"
            elif status in ("processed", "confirmed", "finalized"):
                if not rec.get("landed_warned"):
                    log(f"CRITICAL pending BUY {symbol}: tx {tx[:12]}... "
                        f"{status} nhung vi KHONG co token -> giu block, can "
                        "kiem tra tay")
                    rec["landed_warned"] = True
                    rec["status"] = "landed_no_balance"
                    changed = True
                continue
            elif age >= expire:
                why = (f"tx {tx[:12]}... khong ton tai on-chain" if tx
                       else "khong co tx cua bot") + \
                    f", vi khong co token sau {age:.0f}s"
            else:
                continue
            pending.pop(tid, None)
            processed = self.state.setdefault("processed", [])
            if tid not in processed:
                processed.append(tid)
            self.state["processed"] = processed[-3000:]
            log(f"RESOLVE pending BUY {symbol} ({tid[:12]}...): {why} -> "
                "lenh mua KHONG land, go block")
            changed = True
        return changed

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
            "last_error": redact(error)[:300], "retry_at": retry_after,
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
        """So slot dang chiem = vi the dang quan ly + token bot tung mua
        nhung mat track (unmanaged). Token la tren vi khong co trong signal
        history (airdrop/spam/dust) KHONG tinh — neu khong vi meme bi airdrop
        rac se day slot va bot am tham ngung mua."""
        tracked = {
            p.get("token") for p in self.positions
            if p.get("remaining", 1.0) > 0
        }
        return len(tracked | set(self.unmanaged_tokens))

    def _attempt_signal(self, s, now):
        tid = str(s.get("tid") or "")
        if not tid or tid in self.state.setdefault("processed", []):
            return False
        if not isinstance(s.get("token"), str) or not s.get("token"):
            self._mark_processed(tid, "skipped_invalid_token")
            return False
        # Signal cu (backlog sau restart, retry keo dai) -> khong mua duoi
        age = signal_age_seconds(s, now)
        max_age = float(self.cfg.get("max_signal_age_seconds", 120))
        if age > max_age:
            self._mark_processed(tid, "skipped_stale")
            self.state.setdefault("pending_buys", {}).pop(tid, None)
            log(f"signal {tid[:12]}... ({s.get('symbol', '?')}) cu "
                f"{age:.0f}s > {max_age:.0f}s -> bo qua")
            return False
        # Dedup: khong mo vi the moi neu da co cung token dang mo
        mint = s.get("token")
        for p in self.positions:
            if p.get("token") == mint:
                self._mark_processed(tid, "skipped_duplicate_token")
                log(f"signal {tid[:12]}... -> skipped_duplicate_token "
                    f"({s.get('symbol', '?')} da co vi the mo)")
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
        # Intent cua CHINH bot (khong phai signal cua vi smart money; "signal"
        # chi luu de recover). Ghi truoc khi gui tx; signature cua bot duoc
        # them ngay khi ky (on_buy_sent) -> _resolve_pending_buys doi chieu.
        pending[tid] = {"signal": s, "started_at": int(now),
                        "status": "buy_intent"}
        self.save()  # durable intent before a network side effect

        def _on_sent(sig, rec=pending[tid]):
            rec["tx"] = sig
            rec["sent_at"] = int(time.time())
            rec["status"] = "buy_sent"
            self.save()
        self.swapper.on_buy_sent = _on_sent
        try:
            self._open_from_signal(s, now)
        except EntryRejected as e:
            # Tu choi TRUOC khi gui tx (khong co side effect) -> khong retry.
            pending.pop(tid, None)
            self.state.setdefault("entry_rejects", {})[e.reason] = \
                self.state.setdefault("entry_rejects", {}).get(e.reason, 0) + 1
            log(f"BO QUA {s.get('symbol', '?')}: {e}")
            self._mark_processed(tid, f"skipped_{e.reason}")
            return False
        except SwapUncertain as e:
            pending[tid]["status"] = "buy_uncertain"
            pending[tid]["error"] = redact(e)[:300]
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
            pending[tid]["error"] = redact(e)[:300]
            self.entry_blocked = True
            self.save()
            log(f"BUY {tid[:12]}... khong chac ket qua -> block entry: {e}")
            return False
        finally:
            self.swapper.on_buy_sent = None
        pending.pop(tid, None)
        self._mark_processed(tid, "opened")
        return True

    def ingest_signals(self, now):
        """Mo vi the tu signal. Moi lenh mua co the block toi ~100s (confirm
        + verify) -> sau moi lan thu, chay lai exit cho cac vi the khac (gate
        price_poll_seconds tranh poll thua) de SL/trailing khong bi tre."""
        sigs, _ = self._tail_source("signals")
        opened = 0
        t0 = self._clock()
        queue = [(s, "ERROR retry signal") for s in self._retry_failed_signals(now)]
        queue += [(s, "ERROR mo vi the") for s in sigs]
        for s, err in queue:
            # thoi diem thuc (tuoi signal/opened_at dung ca khi lenh truoc block)
            cur = now + max(0.0, self._clock() - t0)
            try:
                if self._attempt_signal(s, cur):
                    opened += 1
            except Exception:
                log(err + ":\n" + traceback.format_exc())
            elapsed = self._clock() - t0
            if elapsed >= 1.0 and self.positions:
                self.manage_positions(now + elapsed)
        return opened

    @staticmethod
    def _wallet_price(s):
        try:
            px = float(s.get("wallet_price_usd") or 0)
        except (TypeError, ValueError):
            return None
        return px if px > 0 and math.isfinite(px) else None

    def _open_from_signal(self, s, now):
        mint = s["token"]
        symbol = s.get("symbol", "?")
        size = float(self.cfg["trade_size_usd"])
        ref = self._wallet_price(s)
        # Swapper/stub cu khong nhan ref_price_usd -> chi truyen khi co.
        kw = {"ref_price_usd": ref} if ref else {}
        if self.dry:
            r = self.swapper.execute_buy(mint, size, symbol, **kw)
            entry = r["entry_usd"] or s.get("price_now") or s.get("price_usd")
            dec = r["decimals"]
            cost = size
            tx = None
        else:
            r = self.swapper.execute_buy(mint, size, symbol, **kw)
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
            "signal_ts": signal_event_ts(s),
            "rent_lamports": int(r.get("rent_lamports") or 0),
            # Phi mang (base+priority) da nam trong size_usd/realized; ghi
            # rieng de dashboard hien "truoc phi" (~ cach app vi tinh).
            "fee_usd": float(r.get("fee_usd") or 0.0),
            "fee_known": r.get("fee_usd") is not None,
            "wallet_price_usd": ref,
            "entry_premium_pct": r.get("premium_pct"),
            "round_trip_loss_pct": r.get("round_trip_loss_pct"),
            "liquidity_usd": s.get("liquidity_usd"),
        }
        self.positions.append(pos)
        log(f"{'DRY' if self.dry else 'LIVE'} OPEN {symbol} @{entry:.8f} "
            f"size=${cost:.2f} (copy {pos['wallet'][:8]}...)")

    # -- sell-cluster intake --------------------------------------------
    # Smart exit (>=2 vi smart money xa cung token trong cua so) den tu
    # alerts.jsonl type=sell_cluster do radar.py ghi. Khong co nguon
    # "sells.jsonl" rieng: truoc day duoc khai bao nhung khong ai ghi/doc.

    def ingest_alerts(self):
        alerts, _ = self._tail_source("alerts")
        for a in alerts:
            if a.get("type") == "wallet_sell":
                self._on_wallet_sell(a)
                continue
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

    def _on_wallet_sell(self, a):
        """Copy exit: vi nguon cua vi the ban token -> cong don phan da ban;
        >= copy_exit_min_sold_frac -> bat co copy_exit (ban het)."""
        if not self.cfg.get("copy_exit", True):
            return
        tok, wallet = a.get("token"), a.get("wallet")
        try:
            frac = min(1.0, max(0.0, float(a.get("sold_frac"))))
        except (TypeError, ValueError):
            return
        ts = signal_event_ts(a) or 0
        need = float(self.cfg.get("copy_exit_min_sold_frac", 0.5))
        for p in self.positions:
            if (p.get("token") != tok or p.get("wallet") != wallet
                    or p.get("remaining", 1.0) <= 0 or p.get("copy_exit")):
                continue
            # Chi tinh lenh ban SAU lenh mua cua vi (dung sai dong ho 5s).
            since = p.get("signal_ts") or p.get("opened_at") or 0
            if ts and ts < float(since) - 5:
                continue
            seen = p.setdefault("src_sell_tids", [])
            tid = a.get("tid")
            if tid and tid in seen:
                continue
            if tid:
                seen.append(tid)
                del seen[:-20]
            p["src_remaining"] = round(
                float(p.get("src_remaining", 1.0)) * (1.0 - frac), 6)
            sold = 1.0 - p["src_remaining"]
            if sold >= need - 1e-9:
                p["copy_exit"] = True
                log(f"COPY EXIT: vi nguon {wallet[:8]}... da ban {sold:.0%} "
                    f"{p.get('symbol')} -> ban het theo")
            else:
                log(f"vi nguon {wallet[:8]}... ban {frac:.0%} "
                    f"{p.get('symbol')} (cong don {sold:.0%} < {need:.0%}) "
                    "-> giu")

    # -- exit engine -----------------------------------------------------

    def manage_one(self, pos, now, price=None, batch_ok=False):
        """Poll gia 1 vi the, chay exit ladder, thuc hien ban. Tra ve True
        neu vi the da dong han.

        price: gia tu Price API batch (manage_positions). batch_ok=True ma
        price None = Jupiter bo token nay khoi Price API -> quote tung token
        nhung gian cach price_fallback_seconds (do ton rate limit)."""
        if now - pos.get("price_poll_at", 0) < self.cfg["price_poll_seconds"]:
            return False
        if price is None and batch_ok:
            gap = float(self.cfg.get("price_fallback_seconds", 20))
            if now - pos.get("fallback_poll_at", 0) < gap:
                return False
            pos["fallback_poll_at"] = now
        pos["price_poll_at"] = now
        if price is None:
            try:
                price = self._token_price(pos["token"], pos["decimals"])
            except Exception as e:
                log(f"khong lay duoc gia {pos['symbol']}: {e}")
                return False
        if not price:
            return False
        # `held` = phan vi the (theo luong mua ban dau) DANG CON tren vi
        # truoc khi ban. decide_exits tra frac theo luong BAN DAU, con
        # execute_sell ban theo ty le so du HIEN TAI -> phai quy doi.
        # Leg ban tung phan truoc do "khong chac" (tx co the da land):
        # chua ro so du -> CHAN leg tung phan (tranh ban trung), chi cho
        # leg ban sach (an toan vi ban toan bo so du thuc).
        pending_uncertain = (pos.get("uncertain_sell") is not None
                             and self._resolve_uncertain_sell(pos, now, price)
                             == "pending")
        held = float(pos.get("remaining", 1.0))
        actions, reason = decide_exits(pos, price, now, self.cfg)
        for frac, why in actions:
            if (pending_uncertain
                    and self.balance_fraction(frac, held) < 1.0):
                self._rollback_leg(pos, frac, why)
                log(f"{pos['symbol']} hoan {why}: leg ban truoc chua ro ket "
                    "qua, cho xac minh so du")
                continue
            if self._sell_leg(pos, frac, why, price, now, held=held):
                held -= frac
                if pending_uncertain and pos.get("remaining", 1.0) <= 0.005:
                    pos.pop("uncertain_sell", None)
        # Chi dong vi the khi thuc su het token (cac leg thanh cong).
        # Neu leg that bai, remaining duoc hoan tac -> poll sau thu lai.
        if pos.get("remaining", 1.0) <= 0.005:
            if pos.get("remaining", 1.0) > 1e-6:
                log(f"{pos['symbol']}: dust {pos['remaining']:.2%} bo qua "
                    f"khi dong vi the")
                pos["remaining"] = 0.0
            self._close_position(
                pos, pos.get("close_reason") or reason or "ladder_done",
                price, now)
            return True
        return False

    @staticmethod
    def _rollback_leg(pos, frac, why):
        """Hoan tac thay doi cua decide_exits khi leg ban that bai.

        decide_exits da tru `remaining` va bat flag TRUOC khi lenh ban chay.
        Neu khong hoan tac flag, dieu kien do se khong bao gio kich hoat lai
        (vd. ts_done=True -> time stop khong thu lai, vi the bi om toi SL).
        TRAIL/SL/SMART_EXIT khong co flag mot-lan nen tu thu lai poll sau.
        """
        pos["remaining"] = min(1.0, pos.get("remaining", 0.0) + frac)
        if why == "TP1":
            pos["tp1"] = False
        elif why == "TP2":
            pos["tp2"] = False
        elif why == "TIME_KEEP":
            pos["ts_done"] = False
            pos["ts_keep"] = False
        elif why == "TIME":
            pos["ts_done"] = False

    @staticmethod
    def _apply_leg_flags(pos, why):
        """Nguoc cua _rollback_leg (phan flag): leg da thuc su land."""
        if why == "TP1":
            pos["tp1"] = True
        elif why == "TP2":
            pos["tp2"] = True
        elif why == "TIME_KEEP":
            pos["ts_done"] = True
            pos["ts_keep"] = True
        elif why == "TIME":
            pos["ts_done"] = True

    def _resolve_uncertain_sell(self, pos, now, price):
        """Xac minh leg ban "khong chac" qua so du on-chain.

        Tra ve 'landed' | 'not_landed' | 'pending'. 'pending' khi chua doc
        duoc so du, hoac so du chua giam nhung tx van co the land (truoc
        confirm_timeout_seconds ~ blockhash con hieu luc).
        """
        u = pos.get("uncertain_sell") or {}
        if self.dry:
            pos.pop("uncertain_sell", None)
            return "not_landed"
        initial = int(pos.get("tokens_base", 0) or 0)
        try:
            actual, _ = self.swapper.rpc.get_token_balance_base(
                self.swapper.pubkey, pos["token"])
        except Exception as e:
            log(f"{pos['symbol']}: xac minh leg {u.get('why')} loi doc so "
                f"du ({e}) -> cho")
            return "pending"
        if initial <= 0:
            pos.pop("uncertain_sell", None)
            return "not_landed"
        held, frac = float(u.get("held", 0)), float(u.get("frac", 0))
        if actual <= (held - frac * 0.5) * initial:
            sold = max(0.0, held - actual / initial)
            pos["remaining"] = min(float(pos.get("remaining", 1.0)),
                                   actual / initial)
            self._apply_leg_flags(pos, u.get("why"))
            px = float(u.get("price") or price or 0)
            proceeds = sold * initial / (10 ** pos["decimals"]) * px
            pnl = proceeds - sold * pos["size_usd"]
            pos["realized_usd"] = pos.get("realized_usd", 0.0) + pnl
            self._daily()["realized_usd"] = self._daily().get(
                "realized_usd", 0.0) + pnl
            pos["legs"].append({"frac": round(sold, 4), "why": u.get("why"),
                                "proceeds_usd": round(proceeds, 4),
                                "pnl_usd": round(pnl, 4), "at": int(now),
                                "tx": None, "estimated": True})
            pos.pop("uncertain_sell", None)
            log(f"CANH BAO {pos['symbol']} leg {u.get('why')} DA LAND (xac "
                f"minh so du): ban {sold:.2%}, uoc tinh +${proceeds:.2f}")
            return "landed"
        wait = float(self.cfg.get("confirm_timeout_seconds", 90))
        if now - float(u.get("at", 0)) >= wait:
            pos.pop("uncertain_sell", None)
            log(f"{pos['symbol']} leg {u.get('why')} KHONG land (so du "
                "khong giam sau timeout) -> cho phep ban lai")
            return "not_landed"
        return "pending"

    @staticmethod
    def balance_fraction(frac, held):
        """Quy doi frac (theo luong mua BAN DAU) -> ty le so du HIEN TAI.

        Vd. sau TP1 con held=0.6666; TP2 frac=0.3333 -> ban 50% so du.
        Leg cuoi (frac ~ held) tra 1.0 de ban sach, khong de lai dust.
        """
        if held <= 0 or frac >= held - 1e-3:
            return 1.0
        return max(0.0, min(1.0, frac / held))

    def _sell_leg(self, pos, frac, why, price, now, held=None):
        """Ban 1 leg. Tra ve True neu thanh cong, False neu da hoan tac.

        frac: phan cua luong mua ban dau. held: phan dang con tren vi truoc
        leg nay (mac dinh = remaining + frac, vi decide_exits da tru frac).
        """
        if held is None:
            held = float(pos.get("remaining", 0.0)) + frac
        bal_frac = self.balance_fraction(frac, held)
        must_exit = why in MUST_EXIT_WHYS
        tier = int(pos.get("exit_tier", 0) or 0) if must_exit else 0
        # Swapper/stub cu khong nhan tier -> chi truyen khi > 0.
        kw = {"tier": tier} if tier else {}
        try:
            r = self.swapper.execute_sell(pos["token"], bal_frac,
                                          pos["symbol"], **kw)
        except Exception as e:
            definite = (isinstance(e, (NoRoute, SwapError))
                        and not isinstance(e, SwapUncertain))
            if definite:
                log(f"{pos['symbol']} ban {why} THAT BAI: {e} (se thu lai)")
                if must_exit:
                    # Lenh cat lo/bao ve that bai chac chan (khong land):
                    # lan sau nang slippage/impact/phi uu tien, thu lai ngay
                    # vong ke (khong cho price_poll_seconds).
                    n_tiers = len(self.cfg.get("exit_escalation") or [])
                    pos["exit_tier"] = min(tier + 1, n_tiers)
                    pos["price_poll_at"] = 0
                    pos["fallback_poll_at"] = 0
                    if pos["exit_tier"] > tier:
                        log(f"{pos['symbol']} {why}: nang bac thoat "
                            f"{tier} -> {pos['exit_tier']}")
            else:
                log(f"{pos['symbol']} ban {why} KHONG CHAC ket qua:\n"
                    + traceback.format_exc())
            self._rollback_leg(pos, frac, why)
            if not definite and bal_frac < 1.0:
                # Leg tung phan co the da land: KHONG retry ngay (se ban
                # trung). Ghi lai de xac minh qua so du on-chain.
                pos["uncertain_sell"] = {
                    "why": why, "frac": frac, "held": held, "at": now,
                    "price": price,
                }
                log(f"{pos['symbol']} {why}: cho xac minh so du truoc khi "
                    "ban tung phan tiep")
            return False
        if r.get("note") in ("empty", "dust"):
            # Vi khong con token de ban (ban tay / chuyen di / dust): KHONG
            # biet tien thu ve -> khong ghi lo gia (truoc day ghi -100% phan
            # nay, co the kich hoat nham daily stop). Dong vi the.
            pos["legs"].append({"frac": round(frac, 4), "why": why,
                                "balance_frac": round(bal_frac, 4),
                                "proceeds_usd": None, "pnl_usd": 0.0,
                                "at": int(now), "tx": None,
                                "note": f"wallet_{r['note']}"})
            pos["remaining"] = 0.0
            pos["close_reason"] = f"wallet_{r['note']}"
            log(f"CANH BAO {pos['symbol']} {why}: vi khong con token "
                f"({r['note']}) -> dong vi the, P&L phan con lai KHONG XAC "
                "DINH (khong tinh vao daily)")
            return True
        if must_exit and pos.get("exit_tier"):
            pos["exit_tier_used"] = max(int(pos.get("exit_tier_used", 0)),
                                        int(pos["exit_tier"]))
            pos.pop("exit_tier", None)
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
        leg = {"frac": round(frac, 4), "why": why,
               "balance_frac": round(bal_frac, 4),
               "proceeds_usd": round(proceeds, 4),
               "pnl_usd": round(pnl, 4),
               "at": int(now), "tx": r.get("tx")}
        if r.get("unconfirmed"):
            leg["estimated"] = True
        if r.get("acct"):
            leg["acct"] = r["acct"]
        if r.get("fee_usd") is not None:
            leg["fee_usd"] = r["fee_usd"]
            pos["fee_usd"] = float(pos.get("fee_usd") or 0.0) + r["fee_usd"]
        elif not r.get("simulated"):
            pos["fee_known"] = False
        pos["legs"].append(leg)
        log(f"{'DRY' if self.dry else 'LIVE'} SELL {pos['symbol']} {why} "
            f"{frac:.0%} goc ({bal_frac:.0%} so du) +${proceeds:.2f} "
            f"(pnl {pnl:+.2f})")
        return True

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
            "signal_ts": pos.get("signal_ts"),
            "wallet_price_usd": pos.get("wallet_price_usd"),
            "entry_premium_pct": pos.get("entry_premium_pct"),
            "round_trip_loss_pct": pos.get("round_trip_loss_pct"),
            "liquidity_usd": pos.get("liquidity_usd"),
            "rent_lamports": pos.get("rent_lamports", 0),
            "exit_tier_used": pos.get("exit_tier_used", 0),
            "fee_usd": round(float(pos.get("fee_usd") or 0.0), 6),
            "fee_known": bool(pos.get("fee_known", False)),
        }
        with open(TRADES_P, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.positions.remove(pos)
        self._queue_rent_reclaim(pos.get("token"), now,
                                 pos.get("rent_lamports", 0))
        log(f"{'DRY' if self.dry else 'LIVE'} CLOSE {pos['symbol']} "
            f"final={total_ret:+.1%} (${pos.get('realized_usd', 0.0):+.2f}) "
            f"reason={reason}")

    # -- main loop --------------------------------------------------------

    def _batch_prices(self, now):
        """Gia moi vi the qua 1 request Price API (gian cach
        price_poll_seconds). Tra ve (prices, ok); ok=False khi API loi /
        khong ho tro -> manage_one quote tung token nhu cu."""
        if not self.cfg.get("price_batch", True):
            return {}, False
        mints = [p["token"] for p in self.positions
                 if p.get("remaining", 1.0) > 0 and p.get("token")]
        if not mints:
            return {}, False
        cache = getattr(self, "_batch_cache", None)
        gap = float(self.cfg["price_poll_seconds"])
        if (cache and now - cache["at"] < gap
                and set(mints) <= set(cache["mints"])):
            return cache["prices"], cache["ok"]
        try:
            prices, ok = self.jup.prices_usd(mints), True
        except Exception as e:
            if not getattr(self, "_batch_warned", False):
                log(f"Price API batch loi ({redact(e)[:120]}) -> quote tung "
                    "token")
                self._batch_warned = True
            prices, ok = {}, False
        self._batch_cache = {"at": now, "mints": mints, "prices": prices,
                             "ok": ok}
        return prices, ok

    def manage_positions(self, now):
        """Chay exit ladder cho moi vi the (stagger price poll)."""
        prices, batch_ok = self._batch_prices(now)
        for i, p in enumerate(list(self.positions)):
            try:
                if batch_ok:
                    self.manage_one(p, now + i * 0.05,
                                    price=prices.get(p.get("token")),
                                    batch_ok=True)
                else:
                    self.manage_one(p, now + i * 0.05)
            except Exception:
                log(f"ERROR manage {p.get('symbol')}:\n"
                    + traceback.format_exc())

    # -- (1) thu hoi rent token account -----------------------------------

    def _queue_rent_reclaim(self, mint, now, rent_lamports=0):
        if self.dry or not mint or not self.cfg.get("reclaim_rent", True):
            return
        if mint == SOL_MINT or mint in STABLE_MINTS:
            return
        q = self.state.setdefault("rent_reclaim", {})
        if mint not in q:
            q[mint] = {"queued_at": int(now), "attempts": 0,
                       "rent_lamports": int(rent_lamports or 0)}

    def process_rent_reclaim(self, now):
        """Dong token account so du 0 cua token da ban sach -> rent ve vi.
        Khong chan vong lap: gui tx roi vong sau moi kiem tra ket qua."""
        q = self.state.setdefault("rent_reclaim", {})
        if self.dry or not q or not self.cfg.get("reclaim_rent", True):
            return 0
        active = {p.get("token") for p in self.positions}
        for rec in (self.state.get("pending_buys") or {}).values():
            active.add(((rec or {}).get("signal") or {}).get("token"))
        rpc = self.swapper.rpc
        give_up = float(self.cfg.get("reclaim_give_up_seconds", 900))
        retry = float(self.cfg.get("reclaim_retry_seconds", 30))
        budget = int(self.cfg.get("reclaim_max_per_loop", 2))
        done = 0
        for mint, rec in list(q.items()):
            if budget <= 0:
                break
            if mint in active:
                q.pop(mint, None)     # mua lai: dong khi vi the moi dong
                continue
            if now < float(rec.get("retry_at", 0)):
                continue
            sig = rec.get("sig")
            if sig:
                try:
                    st = rpc.get_sig_status(sig)
                except Exception as e:
                    rec["retry_at"] = now + retry
                    log(f"RENT {mint[:8]}...: loi doc status ({e}) -> cho")
                    continue
                if st in ("confirmed", "finalized"):
                    q.pop(mint, None)
                    done += 1
                    log(f"THU HOI RENT {mint[:8]}... "
                        f"+{rec.get('closing_lamports', 0)/1e9:.5f} SOL "
                        f"tx={sig[:12]}...")
                    continue
                if st == "failed" or now - float(rec.get("sent_at", now)) > \
                        float(self.cfg.get("confirm_timeout_seconds", 90)):
                    rec.pop("sig", None)
                    rec["retry_at"] = now + retry
                    log(f"RENT {mint[:8]}...: tx dong account "
                        f"{'that bai' if st == 'failed' else 'het han'} "
                        "-> thu lai")
                continue
            if (rec.get("attempts", 0) >= 3
                    or now - float(rec.get("queued_at", now)) > give_up):
                q.pop(mint, None)
                log(f"RENT {mint[:8]}...: bo thu hoi sau "
                    f"{rec.get('attempts', 0)} lan (con dust/loi)")
                continue
            budget -= 1
            try:
                accts = rpc.get_token_accounts_for_mint(self.swapper.pubkey,
                                                        mint)
            except Exception as e:
                rec["retry_at"] = now + retry
                log(f"RENT {mint[:8]}...: loi doc token account ({e})")
                continue
            empty = [a for a in accts if int(a.get("amount", 1)) == 0]
            if not empty:
                if not accts:
                    q.pop(mint, None)  # da dong (tay / lan truoc)
                else:
                    # So du chua ve 0 (RPC tre / dust) -> thu lai sau, toi
                    # reclaim_give_up_seconds thi bo.
                    rec["retry_at"] = now + retry
                continue
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            try:
                rec["sig"] = self.swapper.close_token_accounts(empty)
                rec["sent_at"] = int(now)
                rec["closing_lamports"] = sum(a["lamports"] for a in empty)
                log(f"RENT {mint[:8]}...: gui dong {len(empty)} account "
                    f"({rec['closing_lamports']/1e9:.5f} SOL)")
            except Exception as e:
                rec["retry_at"] = now + retry
                log(f"RENT {mint[:8]}...: gui dong account loi: "
                    f"{redact(e)[:150]}")
        return done

    def skip_signals_paused(self, now):
        """Dang PAUSE: doc signal moi + signal retry den han, danh dau
        skipped_paused (khong mua). Tra ve so signal da bo qua."""
        sigs, _ = self._tail_source("signals")
        processed = self.state.setdefault("processed", [])
        n = 0
        for s in list(self._retry_failed_signals(now)) + list(sigs):
            tid = str(s.get("tid") or "")
            if not tid or tid in processed:
                continue
            self._mark_processed(tid, "skipped_paused")
            n += 1
        if n:
            log(f"PAUSE: bo qua {n} signal (khong mo vi the moi)")
        return n

    def run_once(self, now=None):
        """Mot vong lap. Tra ve 'stop' neu gap kill switch, 'paused' neu
        dang PAUSE (van reconcile + smart exit + exit ladder), 'ok' neu binh
        thuong."""
        now = now or time.time()
        if os.path.exists(STOP_P):
            log("STOP_LIVE file -> shutdown. VI THE LIVE VAN MO — tu dong "
                "tay, module KHONG tu dong dong.")
            self.save()
            return "stop"
        if os.path.exists(RADAR_STOP_P):
            if not getattr(self, "_warned_radar_stop", False):
                log("CANH BAO: thay file STOP (cua radar paper) — live trader "
                    "VAN CHAY. Muon dung live trader: tao file STOP_LIVE; "
                    "chi ngung mo moi: tao file PAUSE.")
                self._warned_radar_stop = True
        else:
            self._warned_radar_stop = False
        paused = os.path.exists(PAUSE_P)
        try:
            self.reconcile_onchain(now)
        except Exception:
            log("ERROR reconcile:\n" + traceback.format_exc())
        # sell_cluster (smart exit) va exit ladder chay TRUOC mua moi (lenh
        # mua co the block lau) va chay CA KHI PAUSE
        try:
            self.ingest_alerts()
        except Exception:
            log("ERROR ingest alerts:\n" + traceback.format_exc())
        self.manage_positions(now)
        try:
            self.process_rent_reclaim(now)
        except Exception:
            log("ERROR rent reclaim:\n" + traceback.format_exc())
        try:
            if paused:
                # PAUSE: KHONG mo lenh moi. Van tieu thu signal (danh dau
                # skipped_paused) de khi bo PAUSE khong mua don tin cu.
                self.skip_signals_paused(now)
            else:
                self.ingest_signals(now)
        except Exception:
            log("ERROR ingest signals:\n" + traceback.format_exc())
        self.save()
        return "paused" if paused else "ok"


# Dung sach khi nhan SIGINT/SIGTERM (pm2 stop/restart, Ctrl+C): chi dat co,
# thoat GIUA 2 vong lap -> khong cat ngang swap/ghi state. Lan 2 -> dung ngay.
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


def sleep_unless_shutdown(seconds):
    """Ngu tung nhip ngan de dung nhanh khi co tin hieu."""
    end = time.time() + max(0.0, float(seconds))
    while not shutdown_requested():
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(0.5, left))


def main():
    install_signal_handlers()
    cfg = load_config()
    if cfg["mode"] not in ("dry_run", "live"):
        raise SystemExit(f"FATAL: mode '{cfg['mode']}' khong hop le "
                         "(chi nhan dry_run|live)")
    trader = LiveTrader(cfg)
    log(f"LiveTrader start mode={cfg['mode']} "
        f"size=${cfg['trade_size_usd']} maxpos={cfg['max_positions']} "
        f"slippage={cfg['slippage_bps']}bps")
    while not shutdown_requested():
        if trader.run_once() == "stop":
            return
        sleep_unless_shutdown(cfg["loop_seconds"])
    log("LiveTrader: signal -> dung (giua 2 vong lap)")


if __name__ == "__main__":
    main()
