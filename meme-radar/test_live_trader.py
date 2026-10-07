#!/usr/bin/env python3
"""Offline test suite cho live_trader.py — khong can key, khong goi mang that.

Chay:  .venv/bin/python test_live_trader.py
"""
import contextlib
import copy
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import live_trader as lt

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


@contextlib.contextmanager
def isolated():
    """Cach ly moi path runtime cua module vao thu muc tam."""
    tmpd = tempfile.mkdtemp()
    olds = {}
    for name in ("SIG_P", "ALERT_P", "POS_P", "STATE_P",
                 "TRADES_P", "LOG_P", "STOP_P", "RADAR_STOP_P", "PAUSE_P"):
        olds[name] = getattr(lt, name)
        setattr(lt, name, os.path.join(tmpd, name.lower()))
    olds["BASE"] = lt.BASE
    lt.BASE = tmpd
    open(os.path.join(tmpd, "alert_p"), "w").write("")
    try:
        yield tmpd
    finally:
        for name, v in olds.items():
            setattr(lt, name, v)
        shutil.rmtree(tmpd, ignore_errors=True)


def mkpos(entry=1.0, opened_at=1000):
    return {
        "token": "MINT", "symbol": "TST", "wallet": "W",
        "signal_tid": "tid1", "opened_at": opened_at,
        "entry": entry, "size_usd": 10.0, "tokens_base": 10_000_000,
        "decimals": 6, "peak": entry, "remaining": 1.0,
        "realized_usd": 0.0, "tp1": False, "tp2": False,
        "ts_keep": False, "ts_done": False, "smart_exit": False,
        "legs": [],
    }


P = dict(lt.DEFAULTS)


def test_exit_tp_ladder():
    print("== exit ladder: TP1 -> TP2 -> trailing ==")
    pos = mkpos(entry=1.0, opened_at=0)
    now = 60
    a, r = lt.decide_exits(pos, 1.0, now, P)
    check("chua dat TP1 -> khong ban", a == [] and r is None)
    a, r = lt.decide_exits(pos, 1.5, now, P)  # +50%
    check("TP1 ban 1/3", len(a) == 1 and abs(a[0][0] - 0.3334) < 1e-3
          and a[0][1] == "TP1", str(a))
    check("flag tp1", pos["tp1"] is True)
    a, r = lt.decide_exits(pos, 2.0, now, P)  # +100%
    check("TP2 ban them 1/3", len(a) == 1 and abs(a[0][0] - 0.3333) < 1e-3
          and a[0][1] == "TP2", str(a))
    a, r = lt.decide_exits(pos, 2.0, now, P)
    check("dinh 2.0 khong ban", a == [])
    check("peak=2.0", pos["peak"] == 2.0)
    a, r = lt.decide_exits(pos, 1.39, now, P)  # -30.5% tu dinh
    check("trailing ban het", len(a) == 1 and a[0][1] == "TRAIL"
          and r == "trailing", str((a, r)))
    check("remaining ~ 0", pos["remaining"] < 0.01)


def test_exit_sl():
    print("== SL -25% ==")
    pos = mkpos(entry=1.0, opened_at=0)
    a, r = lt.decide_exits(pos, 0.74, 60, P)  # -26%
    check("SL ban het", len(a) == 1 and a[0][1] == "SL" and r == "stop_loss",
          str((a, r)))


def test_trailing_truoc_sl():
    print("== trailing uu tien truoc SL (giong paper) ==")
    pos = mkpos(entry=1.0, opened_at=0)
    lt.decide_exits(pos, 1.5, 60, P)   # TP1
    lt.decide_exits(pos, 2.0, 60, P)   # TP2
    a, r = lt.decide_exits(pos, 1.0, 60, P)  # rot ve entry
    check("trailing thang (khong phai SL)", r == "trailing"
          and a[0][1] == "TRAIL", str((a, r)))


def test_smart_exit():
    print("== smart exit ==")
    pos = mkpos(entry=1.0, opened_at=0)
    pos["smart_exit"] = True
    a, r = lt.decide_exits(pos, 1.1, 60, P)
    check("smart exit ban het", len(a) == 1 and a[0][1] == "SMART_EXIT"
          and r == "smart_exit", str((a, r)))


def test_time_stop():
    print("== time stop 480p ==")
    pos = mkpos(entry=1.0, opened_at=0)
    a, r = lt.decide_exits(pos, 1.25, 481 * 60, P)
    check("TIME_KEEP ban 1/2", len(a) == 1 and abs(a[0][0] - 0.5) < 1e-3
          and a[0][1] == "TIME_KEEP", str(a))
    check("ts_keep armed", pos["ts_keep"] is True and r is None)
    check("con lai 1/2", abs(pos["remaining"] - 0.5) < 1e-3)
    pos2 = mkpos(entry=1.0, opened_at=0)
    a, r = lt.decide_exits(pos2, 1.1, 481 * 60, P)
    check("TIME ban het", len(a) == 1 and a[0][1] == "TIME"
          and r == "time_stop", str((a, r)))
    pos3 = mkpos(entry=1.0, opened_at=0)
    a, r = lt.decide_exits(pos3, 1.1, 479 * 60, P)
    check("chua het 480p -> khong ban", a == [] and r is None)


def test_sizing():
    print("== sizing ==")
    check("$10 @ SOL $150 = 66_666_666 lamports",
          lt.usd_to_lamports(10.0, 150.0) == 66_666_666)


def test_decide_no_crash_on_bad_price():
    print("== gia None/0 khong crash ==")
    pos = mkpos()
    a, r = lt.decide_exits(pos, 0, 60, P)
    check("gia 0 -> khong lam gi", a == [] and r is None)
    a, r = lt.decide_exits(pos, None, 60, P)
    check("gia None -> khong lam gi", a == [] and r is None)


# ---- mocks ----

class FakeJup(lt.JupiterClient):
    """Jupiter gia lap: khong goi mang."""
    DEC = 6

    def __init__(self, price_map=None, sol_usd=150.0):
        self.price_map = price_map or {}
        self._sol = sol_usd
        self.quotes = 0
        self.swaps = 0

    def quote(self, in_mint, out_mint, amount_base, slippage_bps):
        self.quotes += 1
        if in_mint == lt.SOL_MINT and out_mint == lt.USDC_MINT:
            return {"outAmount": str(int(1e6 * self._sol)),
                    "priceImpactPct": "0"}
        if in_mint == lt.SOL_MINT:
            # BUY token: SOL -> token
            px_t = self.price_map.get(out_mint, 0.001)
            tokens = (amount_base / 1e9) * self._sol / px_t
            return {"outAmount": str(int(tokens * 10 ** self.DEC)),
                    "otherAmountThreshold": str(int(tokens * 10 ** self.DEC)),
                    "priceImpactPct": "0.001"}
        px = self.price_map.get(in_mint, 0.001)
        tokens = amount_base / (10 ** self.DEC)
        if out_mint == lt.SOL_MINT:
            # token -> SOL (ban / quote thu khu hoi)
            lamports = int(tokens * px / self._sol * 1e9)
            return {"outAmount": str(lamports),
                    "otherAmountThreshold": str(int(lamports * 0.97)),
                    "priceImpactPct": "0.001"}
        # token -> USDC: dung de dinh gia
        return {"outAmount": str(int(tokens * px * 1e6)),
                "otherAmountThreshold": str(int(tokens * px * 1e6 * 0.99)),
                "priceImpactPct": "0.001"}

    def swap_tx(self, quote, user_pubkey, priority_fee):
        self.swaps += 1
        raise AssertionError("dry_run khong duoc goi swap_tx")

    def sol_price_usd(self):
        return self._sol

    def token_price_usd(self, mint, decimals):
        return self.price_map.get(mint, 0.001)

    def ds_price_usd(self, mint):
        return self.price_map.get(mint)


class FakeRpc(lt.RpcClient):
    def __init__(self):
        pass

    def call(self, method, params):
        raise AssertionError(f"dry_run khong duoc goi RPC {method}")

    def get_mint_decimals(self, mint):
        return 6

    def get_mint_info(self, mint, max_age=300):
        # mint an toan mac dinh (da revoke mint/freeze authority)
        return {"owner": lt.TOKEN_PROGRAM, "data": {"parsed": {
            "type": "mint", "info": {"decimals": 6, "mintAuthority": None,
                                     "freezeAuthority": None}}}}


def b58encode(raw: bytes) -> str:
    alpha = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    enc = ""
    while n > 0:
        n, r = divmod(n, 58)
        enc = alpha[r] + enc
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + (enc or "1")


def test_dry_run_no_send():
    print("== dry_run khong gui gi ==")
    cfg = dict(lt.DEFAULTS, mode="dry_run")
    jup = FakeJup()
    rpc = FakeRpc()
    sw = lt.Swapper(rpc, jup, None, cfg, dry_run=True)
    r = sw.execute_buy("MINT", 10.0, "TST")
    check("buy mo phong, khong tx", r["tx"] is None and r["dry"] is True)
    check("khong goi swap", jup.swaps == 0)
    r2 = sw.execute_sell("MINT", 0.5, "TST")
    check("sell mo phong", r2["simulated"] is True and r2["tx"] is None)


def test_live_no_key_fails():
    print("== live thieu key -> fail closed ==")
    with isolated():
        cfg = dict(lt.DEFAULTS, mode="live", key_file=".khong_ton_tai_123")
        try:
            lt.load_keypair(cfg)
            check("phai SystemExit", False)
        except SystemExit as e:
            check("SystemExit khi thieu key", "khong thay key" in str(e),
                  str(e)[:80])


def test_key_mismatch_fails():
    print("== pubkey khong khop -> fail closed ==")
    from solders.keypair import Keypair
    with isolated() as tmpd:
        kp = Keypair()
        with open(os.path.join(tmpd, "base"), "w") as f:
            f.write(b58encode(bytes(kp)))
        cfg = dict(lt.DEFAULTS, mode="live", key_file="base",
                    wallet_address="KhongKhop1111111111111111111111111111111")
        try:
            lt.load_keypair(cfg)
            check("phai SystemExit khi khop sai", False)
        except SystemExit as e:
            check("SystemExit khi pubkey khong khop", "khong khop" in str(e),
                  str(e)[:80])


def test_key_match_ok():
    print("== pubkey khop -> nap key thanh cong, khong lo key ==")
    from solders.keypair import Keypair
    with isolated() as tmpd:
        kp = Keypair()
        with open(os.path.join(tmpd, "base"), "w") as f:
            f.write(b58encode(bytes(kp)))
        cfg = dict(lt.DEFAULTS, mode="live", key_file="base",
                    wallet_address=str(kp.pubkey()))
        loaded = lt.load_keypair(cfg)
        check("nap key thanh cong", str(loaded.pubkey()) == str(kp.pubkey()))


def test_stop_file():
    print("== STOP file dung vong lap ==")
    with isolated():
        cfg = dict(lt.DEFAULTS, mode="dry_run")
        tr = lt.LiveTrader.__new__(lt.LiveTrader)
        tr.cfg = cfg
        tr.dry = True
        tr.positions = []
        tr.state = {"sig_offset": 0, "alert_offset": 0, "processed": [],
                    "daily": {}}
        open(lt.STOP_P, "w").write("test")
        try:
            res = tr.run_once()
            check("run_once tra ve 'stop'", res == "stop")
        finally:
            if os.path.exists(lt.STOP_P):
                os.unlink(lt.STOP_P)


def test_run_once_dry_e2e():
    print("== e2e dry-run: signal -> open -> TP1 ==")
    with isolated() as tmpd:
        sig = {"detected_at": 1000, "tid": "tid_e2e", "wallet": "W",
               "token": "MINT_E2E", "symbol": "E2E", "amount_usd": 500.0,
               "price_usd": 0.001, "price_now": 0.001, "ts": 1000,
               "tx": "x", "mcap_usd": 60000, "src": "ws"}
        with open(os.path.join(tmpd, "sig_p"), "w") as f:
            f.write(json.dumps(sig) + "\n")
        cfg = dict(lt.DEFAULTS, mode="dry_run", price_poll_seconds=0)
        jup = FakeJup(price_map={"MINT_E2E": 0.001})
        rpc = FakeRpc()
        tr = lt.LiveTrader(cfg, jup=jup, rpc=rpc)
        tr.swapper = lt.Swapper(rpc, jup, None, cfg, dry_run=True)
        tr.state["sig_offset"] = 0  # doc tu dau file (binh thuong bat dau tu cuoi)
        tr.run_once(now=1000)
        check("mo 1 vi the tu signal", len(tr.positions) == 1,
              f"positions={len(tr.positions)}")
        check("entry ~ 0.001", abs(tr.positions[0]["entry"] - 0.001) < 1e-6,
              str(tr.positions[0]["entry"]))
        jup.price_map["MINT_E2E"] = 0.0016  # +60% -> TP1
        tr.run_once(now=1060)
        pos = tr.positions[0]
        check("TP1 kich hoat", pos["tp1"] is True)
        check("con ~2/3", abs(pos["remaining"] - (1 - 0.3334)) < 0.01,
              str(pos["remaining"]))
        check("co leg TP1", any(l["why"] == "TP1" for l in pos["legs"]))
        check("khong goi swap that", jup.swaps == 0)


# ---- PAUSE + rollback leg that bai ----

def _dry_trader(tmpd, price_map):
    cfg = dict(lt.DEFAULTS, mode="dry_run", price_poll_seconds=0)
    jup = FakeJup(price_map=price_map)
    rpc = FakeRpc()
    tr = lt.LiveTrader(cfg, jup=jup, rpc=rpc)
    tr.swapper = lt.Swapper(rpc, jup, None, cfg, dry_run=True)
    tr._clock = lambda: 0.0  # dong ho co dinh: test bien tuoi signal on dinh
    return tr, jup


def _sig(tid, token, ts=1000):
    return {"detected_at": ts, "tid": tid, "wallet": "W", "token": token,
            "symbol": token, "amount_usd": 500.0, "price_usd": 0.001,
            "price_now": 0.001, "ts": ts, "tx": "x", "mcap_usd": 60000,
            "src": "ws"}


def test_pause_still_manages_exits():
    print("== PAUSE: van chay SL, khong mo moi, khong mua tin cu sau PAUSE ==")
    with isolated() as tmpd:
        tr, jup = _dry_trader(tmpd, {"MINT": 0.74, "MINT_NEW": 0.001})
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        with open(lt.SIG_P, "a") as f:
            f.write(json.dumps(_sig("tid_paused", "MINT_NEW")) + "\n")
        open(lt.PAUSE_P, "w").write("")
        res = tr.run_once(now=1000)
        check("run_once tra ve 'paused'", res == "paused", str(res))
        check("SL van chay khi PAUSE (vi the da dong)",
              not any(p["token"] == "MINT" for p in tr.positions),
              str(tr.positions))
        check("khong mo vi the moi khi PAUSE",
              not any(p["token"] == "MINT_NEW" for p in tr.positions))
        check("signal danh dau processed",
              "tid_paused" in tr.state["processed"])
        trades = [json.loads(l) for l in open(lt.TRADES_P)]
        check("ghi trade reason=stop_loss",
              trades and trades[-1]["reason"] == "stop_loss", str(trades))
        os.unlink(lt.PAUSE_P)
        res = tr.run_once(now=1100)
        check("bo PAUSE -> 'ok'", res == "ok", str(res))
        check("bo PAUSE khong mua don signal cu",
              not any(p["token"] == "MINT_NEW" for p in tr.positions),
              str(tr.positions))
        check("khong goi swap that", jup.swaps == 0)


def test_pause_smart_exit():
    print("== PAUSE: sell_cluster van kich hoat smart exit ==")
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {"MINT": 1.1})
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        with open(lt.ALERT_P, "a") as f:
            f.write(json.dumps({"ts": 1000, "type": "sell_cluster",
                                "token": "MINT", "n_wallets": 2}) + "\n")
        open(lt.PAUSE_P, "w").write("")
        tr.run_once(now=1000)
        check("smart exit dong vi the khi PAUSE", tr.positions == [],
              str(tr.positions))


class FlakySwapper:
    """execute_sell nem loi `fails` lan dau, sau do thanh cong (dry)."""

    def __init__(self, fails=1, exc=None):
        self.fails = fails
        self.exc = exc or lt.SwapError("no route tam thoi")
        self.calls = []

    def execute_sell(self, mint, frac, symbol="?", tier=0):
        self.calls.append((mint, frac))
        if self.fails > 0:
            self.fails -= 1
            raise self.exc
        return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                "dry": True, "simulated": True}


def _trader_with(tmpd, price, swapper):
    tr, _ = _dry_trader(tmpd, {"MINT": price})
    tr.swapper = swapper
    return tr


def test_time_stop_retry_after_fail():
    print("== TIME ban that bai -> poll sau thu lai (khong om toi SL) ==")
    with isolated() as tmpd:
        sw = FlakySwapper(fails=1)
        tr = _trader_with(tmpd, 1.0, sw)  # ret 0% -> TIME cat het
        pos = mkpos(entry=1.0, opened_at=0)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        t = 481 * 60
        closed = tr.manage_one(pos, t)
        check("lan 1 that bai -> chua dong", closed is False)
        check("remaining hoan lai 1.0", abs(pos["remaining"] - 1.0) < 1e-9,
              str(pos["remaining"]))
        check("ts_done hoan tac", pos["ts_done"] is False)
        closed = tr.manage_one(pos, t + 30)
        check("lan 2 thu lai TIME va dong", closed is True and not tr.positions)
        check("2 lan goi sell", len(sw.calls) == 2, str(sw.calls))
        trades = [json.loads(l) for l in open(lt.TRADES_P)]
        check("reason=time_stop", trades[-1]["reason"] == "time_stop",
              str(trades[-1]))


def test_time_keep_retry_after_fail():
    print("== TIME_KEEP ban that bai -> hoan tac ts_keep/ts_done ==")
    with isolated() as tmpd:
        sw = FlakySwapper(fails=1)
        tr = _trader_with(tmpd, 1.25, sw)  # +25% -> TIME_KEEP 1/2
        pos = mkpos(entry=1.0, opened_at=0)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        t = 481 * 60
        tr.manage_one(pos, t)
        check("ts_keep hoan tac", pos["ts_keep"] is False)
        check("ts_done hoan tac", pos["ts_done"] is False)
        check("remaining 1.0", abs(pos["remaining"] - 1.0) < 1e-9)
        tr.manage_one(pos, t + 30)
        check("lan 2 TIME_KEEP thanh cong",
              pos["ts_keep"] is True and abs(pos["remaining"] - 0.5) < 1e-9,
              str((pos["ts_keep"], pos["remaining"])))
        check("co leg TIME_KEEP", [l["why"] for l in pos["legs"]] == ["TIME_KEEP"])


def test_unknown_error_rolls_back_tp():
    print("== loi khong xac dinh khi ban TP1 -> hoan tac tp1 ==")
    with isolated() as tmpd:
        sw = FlakySwapper(fails=1, exc=RuntimeError("rpc timeout la"))
        tr = _trader_with(tmpd, 1.6, sw)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        tr.manage_one(pos, 1000)
        check("tp1 hoan tac", pos["tp1"] is False)
        check("remaining 1.0", abs(pos["remaining"] - 1.0) < 1e-9)
        tr.manage_one(pos, 1030)
        check("lan 2 TP1 ban duoc", pos["tp1"] is True
              and [l["why"] for l in pos["legs"]] == ["TP1"])



# ---- khoi luong ban tung phan theo so du that ----

class WalletSwapper:
    """Mo phong execute_sell LIVE: ban `frac` cua so du HIEN TAI tren vi
    (giong Swapper.execute_sell that), theo doi so du token."""

    def __init__(self, balance, price=1.0, decimals=6, fail_whys=()):
        self.balance = balance
        self.price = price
        self.dec = decimals
        self.calls = []
        self.fail_next = list(fail_whys)

    def execute_sell(self, mint, frac, symbol="?", tier=0):
        self.calls.append(round(frac, 4))
        if self.fail_next:
            self.fail_next.pop(0)
            raise lt.SwapError("route tam thoi loi")
        amount = self.balance if frac >= 0.999 else int(self.balance * frac)
        self.balance -= amount
        return {"sold_base": amount,
                "proceeds_usd": amount / 10 ** self.dec * self.price,
                "tx": "sig", "dry": False}


def _live_like(tmpd, price, sw):
    tr, _ = _dry_trader(tmpd, {"MINT": price})
    tr.swapper = sw
    return tr


def test_partial_sell_sizes_vs_wallet():
    print("== TP1 -> TP2 -> TRAIL: so du con 2/3 -> 1/3 -> 0 cua luong goc ==")
    with isolated() as tmpd:
        init = 10_000_000
        sw = WalletSwapper(init)
        tr = _live_like(tmpd, 1.5, sw)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        tr.manage_one(pos, 1000)                       # +50% TP1
        check("TP1 ban 33.34% so du", sw.calls == [0.3334], str(sw.calls))
        check("con ~2/3 luong goc", abs(sw.balance / init - 0.6666) < 1e-3,
              str(sw.balance / init))
        tr.jup.price_map["MINT"] = 2.0
        tr.manage_one(pos, 1030)                       # +100% TP2
        check("TP2 ban 50% so du hien tai", sw.calls[-1] == 0.5, str(sw.calls))
        check("con ~1/3 luong goc", abs(sw.balance / init - 0.3333) < 1e-3,
              str(sw.balance / init))
        tr.jup.price_map["MINT"] = 1.3
        closed = tr.manage_one(pos, 1060)              # trailing -35% tu dinh
        check("TRAIL ban 100% so du", sw.calls[-1] == 1.0, str(sw.calls))
        check("vi sach token", sw.balance == 0 and closed is True)


def test_tp1_tp2_same_poll():
    print("== TP1 + TP2 cung 1 poll (gia nhay +100%) ==")
    with isolated() as tmpd:
        init = 9_000_000
        sw = WalletSwapper(init)
        tr = _live_like(tmpd, 2.0, sw)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        tr.manage_one(pos, 1000)
        check("2 leg: 33.34% roi 50%", sw.calls == [0.3334, 0.5], str(sw.calls))
        check("con ~1/3 luong goc", abs(sw.balance / init - 0.3333) < 1e-3,
              str(sw.balance / init))


def test_time_keep_after_tp1():
    print("== TP1 roi TIME_KEEP: ban 1/2 phan con lai ==")
    with isolated() as tmpd:
        init = 12_000_000
        sw = WalletSwapper(init)
        tr = _live_like(tmpd, 1.5, sw)
        pos = mkpos(entry=1.0, opened_at=0)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        tr.manage_one(pos, 60)                          # TP1
        tr.jup.price_map["MINT"] = 1.3                  # +30% >= 20%
        tr.manage_one(pos, 481 * 60)                    # TIME_KEEP
        check("TIME_KEEP ban 50% so du", sw.calls[-1] == 0.5, str(sw.calls))
        check("con 1/3 luong goc", abs(sw.balance / init - 0.3333) < 1e-3,
              str(sw.balance / init))
        check("remaining khop so du",
              abs(pos["remaining"] - sw.balance / init) < 1e-3,
              str((pos["remaining"], sw.balance / init)))


def test_failed_tp1_then_tp2_same_poll():
    print("== TP1 that bai, TP2 cung poll -> ty le theo so du chua giam ==")
    with isolated() as tmpd:
        init = 9_000_000
        sw = WalletSwapper(init, fail_whys=["TP1"])
        tr = _live_like(tmpd, 2.0, sw)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        tr.manage_one(pos, 1000)
        check("TP2 ban 33.33% so du (TP1 chua ban)",
              sw.calls == [0.3334, 0.3333], str(sw.calls))
        check("remaining khop so du",
              abs(pos["remaining"] - sw.balance / init) < 1e-3,
              str((pos["remaining"], sw.balance / init)))



# ---- price impact: Jupiter tra phan so ----

def test_price_impact_fraction():
    print("== priceImpactPct la phan so (0.0042 = 0.42%) ==")
    check("0.0042 -> 0.42%", abs(lt.price_impact_pct({"priceImpactPct": "0.0042"}) - 0.42) < 1e-9)
    check("0.06 -> 6%", abs(lt.price_impact_pct({"priceImpactPct": "0.06"}) - 6.0) < 1e-9)
    check("am -> 0", lt.price_impact_pct({"priceImpactPct": "-0.01"}) == 0.0)
    check("thieu -> inf (fail closed)", lt.price_impact_pct({}) == float("inf"))
    check("rac -> inf", lt.price_impact_pct({"priceImpactPct": "abc"}) == float("inf"))


class ImpactJup(FakeJup):
    def __init__(self, impact):
        super().__init__()
        self.impact = impact

    def quote(self, in_mint, out_mint, amount_base, slippage_bps):
        q = super().quote(in_mint, out_mint, amount_base, slippage_bps)
        q["priceImpactPct"] = self.impact
        return q

    def swap_tx(self, quote, user_pubkey, priority_fee):
        self.swaps += 1
        return "TX"


def test_price_impact_guard_blocks():
    print("== guard impact: 6% > 5% bi chan, 0.42% qua ==")
    cfg = dict(lt.DEFAULTS, mode="live", max_swap_retries=1)
    sw = lt.Swapper(FakeRpc(), ImpactJup("0.06"), None, cfg, dry_run=False)
    try:
        sw._quote_swap(lt.SOL_MINT, "MINT", 1000)
        check("6% phai bi chan", False)
    except lt.SwapError as e:
        check("6% bi chan", "price impact" in str(e), str(e))
    sw = lt.Swapper(FakeRpc(), ImpactJup("0.0042"), None, cfg, dry_run=False)
    q, tx = sw._quote_swap(lt.SOL_MINT, "MINT", 1000)
    check("0.42% qua guard", tx == "TX")
    # dry-run cung ap guard
    cfgd = dict(lt.DEFAULTS, mode="dry_run")
    swd = lt.Swapper(FakeRpc(), ImpactJup("0.06"), None, cfgd, dry_run=True)
    try:
        swd.execute_buy("MINT", 10.0, "TST")
        check("dry-run 6% phai bi chan", False)
    except lt.SwapError:
        check("dry-run 6% bi chan", True)



# ---- tuoi signal ----

def test_signal_age_helpers():
    print("== tuoi signal ==")
    check("uu tien ts", lt.signal_age_seconds({"ts": 900, "detected_at": 950}, 1000) == 100)
    check("fallback detected_at", lt.signal_age_seconds({"detected_at": 950}, 1000) == 50)
    check("ts ms nhan dien", abs(lt.signal_age_seconds({"ts": 1_700_000_000_000}, 1_700_000_060) - 60) < 1e-6)
    check("khong co ts -> inf", lt.signal_age_seconds({}, 1000) == float("inf"))
    check("ts tuong lai -> 0", lt.signal_age_seconds({"ts": 1100}, 1000) == 0)


def test_stale_signal_skipped():
    print("== signal > 120s bi bo qua, <= 120s duoc mua ==")
    with isolated() as tmpd:
        tr, jup = _dry_trader(tmpd, {"OLD": 0.001, "FRESH": 0.001})
        with open(lt.SIG_P, "a") as f:
            f.write(json.dumps(_sig("tid_old", "OLD", ts=1000)) + "\n")
            f.write(json.dumps(_sig("tid_fresh", "FRESH", ts=1050)) + "\n")
        tr.run_once(now=1170)   # OLD 170s, FRESH 120s
        toks = {p["token"] for p in tr.positions}
        check("OLD (170s) khong mua", "OLD" not in toks, str(toks))
        check("FRESH (120s) duoc mua", "FRESH" in toks, str(toks))
        check("OLD danh dau processed", "tid_old" in tr.state["processed"])


def test_retry_stops_when_stale():
    print("== signal loi tam thoi: retry trong 2p, qua 2p thi bo ==")
    with isolated() as tmpd:
        tr, jup = _dry_trader(tmpd, {"MINT_R": 0.001})
        s = _sig("tid_r", "MINT_R", ts=1000)
        tr._record_signal_failure(s, 1000, "no route tam thoi")
        check("co trong failures", "tid_r" in tr.state["signal_failures"])
        tr.run_once(now=1200)   # retry den han nhung signal da 200s
        check("khong mua signal cu khi retry",
              not any(p["token"] == "MINT_R" for p in tr.positions))
        check("xoa khoi failures", "tid_r" not in tr.state["signal_failures"])
        check("processed", "tid_r" in tr.state["processed"])



# ---- khong lo Helius key ----

def test_redact_secrets():
    print("== redact api-key trong log/loi ==")
    msg = ("429 Client Error: Too Many Requests for url: "
           "https://mainnet.helius-rpc.com/?api-key=abcd1234-SECRET-xyz")
    out = lt.redact(msg)
    check("che api-key trong URL", "SECRET" not in out and "api-key=***" in out, out)
    lt.register_secret("RAWKEY-99887766")
    check("che gia tri da dang ky", "RAWKEY" not in lt.redact("loi RAWKEY-99887766 x"))


def test_rpc_error_redacted():
    print("== RpcClient: loi ket noi khong chua key, log khong chua key ==")
    with isolated():
        rpc = lt.RpcClient("https://127.0.0.1:1/?api-key=TOPSECRET-123456", timeout=1)
        try:
            rpc.call("getBalance", ["x"])
            check("phai loi", False)
        except lt.RpcError as e:
            check("RpcError khong chua key", "TOPSECRET" not in str(e), str(e))
            check("khong chain exception goc", e.__cause__ is None and e.__suppress_context__)
        lt.log("thu: https://x/?api-key=TOPSECRET-123456 het")
        content = open(lt.LOG_P).read()
        check("log file khong chua key", "TOPSECRET" not in content)



# ---- capacity khong tinh airdrop ----

def test_capacity_ignores_airdrops():
    print("== 12 token airdrop tren vi khong chiem slot ==")
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {"NEWTOK": 0.001})
        tr.onchain_tokens = {f"AIRDROP{i}" for i in range(12)}
        tr.unmanaged_tokens = set()
        check("occupancy = 0", tr._occupied_token_count() == 0)
        tr.unmanaged_tokens = {"LOST1"}
        tr.positions = [mkpos()]
        check("occupancy = vi the + unmanaged = 2", tr._occupied_token_count() == 2)
        tr.positions, tr.unmanaged_tokens = [], set()
        with open(lt.SIG_P, "a") as f:
            f.write(json.dumps(_sig("tid_cap", "NEWTOK", ts=1000)) + "\n")
        tr.run_once(now=1000)
        check("van mua duoc khi vi day airdrop",
              any(p["token"] == "NEWTOK" for p in tr.positions))



class BalRpc:
    """RPC gia: chi tra token balances cho reconcile live."""

    def __init__(self, balances):
        self.balances = balances

    def get_token_balances(self, owner):
        return dict(self.balances)


class LiveSwapperStub:
    def __init__(self, rpc):
        self.rpc = rpc
        self.pubkey = "PUB"


def _live_trader_stub(tmpd, balances, price_map=None):
    tr, _ = _dry_trader(tmpd, price_map or {})
    tr.dry = False
    tr.swapper = LiveSwapperStub(BalRpc(balances))
    return tr


def test_reconcile_live_airdrops():
    print("== reconcile live: airdrop khong block, khong chiem slot ==")
    with isolated() as tmpd:
        bal = {f"AIR{i}": {"amount": 5, "decimals": 6} for i in range(15)}
        bal["MINT"] = {"amount": 10_000_000, "decimals": 6}
        tr = _live_trader_stub(tmpd, bal)
        tr.positions = [mkpos()]
        ok = tr.reconcile_onchain(now=1000, force=True)
        check("khong block entry", ok is True and tr.entry_blocked is False)
        check("occupancy = 1", tr._occupied_token_count() == 1,
              str(tr._occupied_token_count()))



# ---- sell khong chac: khong ban trung ----

class SellRpc:
    def __init__(self, balance):
        self.balance = balance

    def get_token_balance_base(self, owner, mint):
        return self.balance, 6

    def get_balance_lamports(self, owner):
        return 10 * 10 ** 9


class SellJup(FakeJup):
    def swap_tx(self, quote, user_pubkey, priority_fee):
        return "TX"


def _swapper_send_fails(rpc, land):
    cfg = dict(lt.DEFAULTS, mode="live", buy_balance_verify_attempts=2,
               buy_balance_verify_seconds=0)
    sw = lt.Swapper(rpc, SellJup(), None, cfg, dry_run=False)
    sw.pubkey = "PUB"

    def boom(_tx):
        if land:
            rpc.balance -= int(rpc.balance * 0.5)
        raise RuntimeError("sendTransaction timeout")
    sw._sign_and_send = boom
    return sw


def test_execute_sell_send_exception():
    print("== execute_sell: send exception -> xac minh so du ==")
    rpc = SellRpc(1_000_000)
    r = _swapper_send_fails(rpc, land=True).execute_sell("MINT", 0.5, "TST")
    check("tx da land -> tra ket qua unconfirmed",
          r.get("unconfirmed") is True and r["sold_base"] == 500_000, str(r))
    rpc = SellRpc(1_000_000)
    try:
        _swapper_send_fails(rpc, land=False).execute_sell("MINT", 0.5, "TST")
        check("phai SwapUncertain", False)
    except lt.SwapUncertain:
        check("chua thay token giam -> SwapUncertain", True)


class UncertainWallet:
    """Swapper + rpc gia cho trader live: dieu khien tung lan ban."""

    def __init__(self, balance, modes):
        self.balance = balance
        self.modes = list(modes)   # moi lan sell: ok | landed_unc | unc
        self.calls = []
        self.rpc = self
        self.pubkey = "PUB"

    def get_token_balance_base(self, owner, mint):
        return self.balance, 6

    def get_balance_lamports(self, owner):
        return 10 * 10 ** 9

    def execute_sell(self, mint, frac, symbol="?", tier=0):
        self.calls.append(round(frac, 4))
        mode = self.modes.pop(0) if self.modes else "ok"
        amount = self.balance if frac >= 0.999 else int(self.balance * frac)
        if mode in ("ok", "landed_unc"):
            self.balance -= amount
        if mode != "ok":
            raise lt.SwapUncertain("sell unconfirmed (test)")
        return {"sold_base": amount, "proceeds_usd": amount / 1e6 * 1.5,
                "tx": "sig", "dry": False}


def _live_wallet_trader(tmpd, price, wallet):
    tr, jup = _dry_trader(tmpd, {"MINT": price})
    tr.dry = False
    tr.swapper = wallet
    return tr, jup


def test_uncertain_partial_landed_no_double_sell():
    print("== TP1 khong chac nhung da land -> khong ban TP1 lan 2 ==")
    with isolated() as tmpd:
        init = 10_000_000
        w = UncertainWallet(init, ["landed_unc"])
        tr, _ = _live_wallet_trader(tmpd, 1.5, w)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        tr.manage_one(pos, 1000)
        check("ghi uncertain_sell", pos.get("uncertain_sell") is not None)
        tr.manage_one(pos, 1030)   # poll sau: xac minh so du -> landed
        check("chi ban 1 lan", w.calls == [0.3334], str(w.calls))
        check("tp1 da bat lai", pos["tp1"] is True)
        check("so du con 2/3", abs(w.balance / init - 0.6666) < 1e-3,
              str(w.balance / init))
        check("remaining khop so du", abs(pos["remaining"] - w.balance / init) < 1e-3)
        check("leg uoc tinh", pos["legs"] and pos["legs"][-1].get("estimated"))


def test_uncertain_partial_not_landed_waits_then_retries():
    print("== TP1 khong chac, chua land: cho het timeout roi moi ban lai ==")
    with isolated() as tmpd:
        init = 10_000_000
        w = UncertainWallet(init, ["unc"])
        tr, _ = _live_wallet_trader(tmpd, 1.5, w)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        tr.manage_one(pos, 1000)
        tr.manage_one(pos, 1030)   # < 90s: pending -> khong ban
        check("trong 90s khong ban lai", w.calls == [0.3334], str(w.calls))
        tr.manage_one(pos, 1100)   # > 90s: not landed -> ban lai
        check("sau timeout ban lai TP1", w.calls == [0.3334, 0.3334], str(w.calls))
        check("so du con 2/3", abs(w.balance / init - 0.6666) < 1e-3)


def test_uncertain_pending_allows_full_sl():
    print("== dang cho xac minh nhung cham SL -> van ban sach ==")
    with isolated() as tmpd:
        w = UncertainWallet(10_000_000, ["unc"])
        tr, jup = _live_wallet_trader(tmpd, 1.5, w)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        tr.manage_one(pos, 1000)
        jup.price_map["MINT"] = 0.7    # -30% -> SL
        closed = tr.manage_one(pos, 1030)
        check("SL ban 100% so du", w.calls[-1] == 1.0, str(w.calls))
        check("dong vi the", closed is True and w.balance == 0)



# ---- vi het token: khong ghi lo gia ----

class EmptyWallet(UncertainWallet):
    def execute_sell(self, mint, frac, symbol="?", tier=0):
        self.calls.append(round(frac, 4))
        return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                "dry": False, "note": "empty"}


def test_empty_wallet_no_fake_loss():
    print("== SL nhung vi da het token -> dong, khong ghi lo -100% ==")
    with isolated() as tmpd:
        w = EmptyWallet(0, [])
        tr, _ = _live_wallet_trader(tmpd, 0.7, w)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        before = tr._daily().get("realized_usd", 0.0)
        closed = tr.manage_one(pos, 1000)
        check("dong vi the", closed is True and not tr.positions)
        check("daily realized khong doi",
              tr._daily().get("realized_usd", 0.0) == before,
              str(tr._daily()))
        rec = json.loads(open(lt.TRADES_P).read().splitlines()[-1])
        check("reason=wallet_empty", rec["reason"] == "wallet_empty", str(rec))
        check("realized 0 (khong -$10)", rec["realized_usd"] == 0.0, str(rec))



def test_reconcile_gone_writes_trade():
    print("== reconcile: token bien mat -> ghi live_trades (khong xoa im lang) ==")
    with isolated() as tmpd:
        tr = _live_trader_stub(tmpd, {})   # vi khong con MINT
        pos = mkpos()
        pos["legs"] = [{"frac": 0.3334, "why": "TP1", "pnl_usd": 1.7}]
        pos["realized_usd"] = 1.7
        tr.positions = [pos]
        tr.reconcile_onchain(now=1000, force=True)
        check("vi the bi dong", tr.positions == [])
        lines = open(lt.TRADES_P).read().splitlines() if os.path.exists(lt.TRADES_P) else []
        rec = json.loads(lines[-1]) if lines else {}
        check("co trade record", rec.get("reason") == "reconcile_wallet_empty", str(rec))
        check("giu legs/realized da co", rec.get("realized_usd") == 1.7
              and len(rec.get("legs", [])) == 1, str(rec))



# ---- DexScreener chon dung pair ----

def test_pick_dexscreener_price():
    print("== DexScreener: chon pair mint la base, thanh khoan cao nhat ==")
    rows = [
        {"baseToken": {"address": "OTHER"}, "quoteToken": {"address": "MINT"},
         "priceUsd": "150", "priceNative": "1000", "liquidity": {"usd": 9e6}},
        {"baseToken": {"address": "MINT"}, "quoteToken": {"address": "SOL"},
         "priceUsd": "0.20", "liquidity": {"usd": 500}},
        {"baseToken": {"address": "MINT"}, "quoteToken": {"address": "USDC"},
         "priceUsd": "0.15", "liquidity": {"usd": 80000}},
    ]
    check("chon 0.15 (base, liq cao nhat)", lt.pick_dexscreener_price(rows, "MINT") == 0.15)
    only_quote = rows[:1]
    check("chi co pair quote -> priceUsd/priceNative",
          abs(lt.pick_dexscreener_price(only_quote, "MINT") - 0.15) < 1e-12)
    check("rong -> None", lt.pick_dexscreener_price([], "MINT") is None)
    check("rac -> None", lt.pick_dexscreener_price({"x": 1}, "MINT") is None)
    check("khong lien quan -> None", lt.pick_dexscreener_price(
        [{"baseToken": {"address": "X"}, "priceUsd": "1"}], "MINT") is None)



# ---- lenh mua cham khong chan exit ----

class SlowBuySwapper:
    def __init__(self, clock, events, buy_seconds=60):
        self.clock = clock
        self.events = events
        self.buy_seconds = buy_seconds

    def execute_buy(self, mint, size_usd, symbol="?", ref_price_usd=None):
        self.events.append(("buy", mint))
        self.clock[0] += self.buy_seconds
        return {"tokens_base": 10_000_000, "decimals": 6, "cost_usd": size_usd,
                "entry_usd": 0.001, "tx": None, "dry": True}

    def execute_sell(self, mint, frac, symbol="?", tier=0):
        self.events.append(("sell", mint))
        return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                "dry": True, "simulated": True}


def test_slow_buy_does_not_block_exits():
    print("== mua cham 60s: SL vi the khac chay xen giua 2 lenh mua ==")
    with isolated() as tmpd:
        tr, jup = _dry_trader(tmpd, {"MINT": 0.7, "A": 0.001, "B": 0.001})
        tr.cfg["price_poll_seconds"] = 20
        clock, events = [0.0], []
        tr._clock = lambda: clock[0]
        tr.swapper = SlowBuySwapper(clock, events)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 995       # vua poll -> chua den han o dau vong
        tr.positions = [pos]
        with open(lt.SIG_P, "a") as f:
            f.write(json.dumps(_sig("tid_a", "A", ts=1000)) + "\n")
            f.write(json.dumps(_sig("tid_b", "B", ts=1000)) + "\n")
        tr.run_once(now=1000)
        check("thu tu: buy A -> sell MINT (SL) -> buy B",
              events == [("buy", "A"), ("sell", "MINT"), ("buy", "B")], str(events))


def test_exits_before_buys():
    print("== run_once: exit chay truoc mua moi ==")
    with isolated() as tmpd:
        tr, jup = _dry_trader(tmpd, {"MINT": 0.7, "A": 0.001})
        clock, events = [0.0], []
        tr._clock = lambda: clock[0]
        tr.swapper = SlowBuySwapper(clock, events, buy_seconds=0)
        pos = mkpos(entry=1.0, opened_at=900)
        pos["price_poll_at"] = 0
        tr.positions = [pos]
        with open(lt.SIG_P, "a") as f:
            f.write(json.dumps(_sig("tid_a", "A", ts=1000)) + "\n")
        tr.run_once(now=1000)
        check("sell truoc buy", events == [("sell", "MINT"), ("buy", "A")], str(events))



def test_age_uses_real_time_after_slow_buy():
    print("== signal thu 2 tinh tuoi theo thoi gian thuc sau lenh mua cham ==")
    with isolated() as tmpd:
        tr, jup = _dry_trader(tmpd, {"A": 0.001, "B": 0.001})
        clock, events = [0.0], []
        tr._clock = lambda: clock[0]
        tr.swapper = SlowBuySwapper(clock, events, buy_seconds=90)
        with open(lt.SIG_P, "a") as f:
            f.write(json.dumps(_sig("tid_a", "A", ts=1000)) + "\n")
            f.write(json.dumps(_sig("tid_b", "B", ts=1000)) + "\n")
        tr.run_once(now=1060)   # A 60s -> mua (90s); B luc do 150s -> bo
        check("chi mua A", events == [("buy", "A")], str(events))
        check("B skipped", "tid_b" in tr.state["processed"])



# ---- kill switch rieng ----

def test_radar_stop_does_not_stop_live():
    print("== STOP (radar) khong tat live; STOP_LIVE moi tat ==")
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {})
        open(lt.RADAR_STOP_P, "w").write("")
        check("STOP radar -> live van chay", tr.run_once(now=1000) == "ok")
        open(lt.STOP_P, "w").write("")
        check("STOP_LIVE -> stop", tr.run_once(now=1010) == "stop")
    check("duong dan that la STOP_LIVE",
          os.path.basename(lt.STOP_P) == "STOP_LIVE"
          and os.path.basename(lt.RADAR_STOP_P) == "STOP")


def test_no_dead_sells_source():
    print("== Khong con nguon sells.jsonl khai bao thua ==")
    with isolated() as tmpd:
        logs = []
        orig = lt.log
        lt.log = logs.append
        try:
            tr, _ = _dry_trader(tmpd, {})
        finally:
            lt.log = orig
        check("trader chi tail signals + alerts",
              sorted(tr.paths) == ["alerts", "signals"], tr.paths)
        check("khong con WARNING nguon sells chua ton tai",
              not any("source sells" in m for m in logs), logs)
        check("config mac dinh khong con sells_jsonl",
              "sells_jsonl" not in lt.DEFAULTS)


# ---- pending BUY: chi la intent cua bot, phai tu giai quyet theo on-chain ----

class PendRpc(BalRpc):
    def __init__(self, balances, statuses=None):
        super().__init__(balances)
        self.statuses = statuses or {}
        self.sig_calls = []

    def get_sig_status(self, sig):
        self.sig_calls.append(sig)
        return self.statuses.get(sig)


def _pending_trader(tmpd, balances, pend, statuses=None):
    tr = _live_trader_stub(tmpd, balances)
    tr.swapper = LiveSwapperStub(PendRpc(balances, statuses))
    tr.state["pending_buys"] = pend
    return tr


def _claudia(started_at, **extra):
    sig = _sig("tid_claudia", "CLAUDIA", ts=started_at)
    sig["amount_usd"] = 982.8           # size cua VI NGUON, khong phai bot
    rec = {"signal": sig, "started_at": started_at, "status": "buy_uncertain"}
    rec.update(extra)
    return {"tid_claudia": rec}


def test_pending_buy_legacy_not_landed_unblocks():
    print("== pending BUY cu (khong tx), vi khong co token -> tu go block ==")
    with isolated() as tmpd:
        tr = _pending_trader(tmpd, {}, _claudia(1000))
        ok = tr.reconcile_onchain(now=1100, force=True)
        check("chua qua han (100s) -> van block",
              ok is False and "tid_claudia" in tr.state["pending_buys"])
        ok = tr.reconcile_onchain(now=1000 + 600, force=True)
        check("qua han, vi khong co token -> go pending + unblock",
              ok is True and tr.state["pending_buys"] == {}
              and tr.entry_blocked is False
              and tr.state.get("block_reason") is None,
              str(tr.state.get("block_reason")))
        check("signal danh dau processed (khong mua lai)",
              "tid_claudia" in tr.state["processed"])


def test_pending_buy_with_tx_resolved_by_chain():
    print("== pending BUY co tx cua bot: doi chieu status on-chain ==")
    with isolated() as tmpd:
        tr = _pending_trader(tmpd, {}, _claudia(1000, tx="SIGFAIL",
                                                sent_at=1000),
                             {"SIGFAIL": "failed"})
        ok = tr.reconcile_onchain(now=1010, force=True)
        check("tx failed on-chain -> go ngay, khong cho het han",
              ok is True and tr.state["pending_buys"] == {})
    with isolated() as tmpd:
        tr = _pending_trader(tmpd, {}, _claudia(1000, tx="SIGOK",
                                                sent_at=1000),
                             {"SIGOK": "finalized"})
        ok = tr.reconcile_onchain(now=5000, force=True)
        check("tx da land ma vi khong co token -> GIU block (kiem tra tay)",
              ok is False and "tid_claudia" in tr.state["pending_buys"]
              and tr.state["pending_buys"]["tid_claudia"]["status"]
              == "landed_no_balance")
    with isolated() as tmpd:
        tr = _pending_trader(tmpd, {}, _claudia(1000, tx="SIGLOST",
                                                sent_at=1000))
        ok = tr.reconcile_onchain(now=1060, force=True)
        check("tx chua thay, chua het han -> giu",
              ok is False and tr.state["pending_buys"])
        ok = tr.reconcile_onchain(now=1200, force=True)
        check("tx khong ton tai sau han blockhash -> go",
              ok is True and tr.state["pending_buys"] == {})


def test_pending_buy_landed_token_recovered():
    print("== pending BUY: token DA ve vi -> recover vi the, khong mat ==")
    with isolated() as tmpd:
        bal = {"CLAUDIA": {"amount": 7_000_000, "decimals": 6}}
        tr = _pending_trader(tmpd, bal, _claudia(1000))
        ok = tr.reconcile_onchain(now=5000, force=True)
        pos = tr.positions[0] if tr.positions else {}
        check("recover tu signal trong pending (signals.jsonl khong co)",
              pos.get("token") == "CLAUDIA" and pos.get("recovered")
              and pos.get("tokens_base") == 7_000_000, str(pos))
        check("pending go + unblock",
              ok is True and tr.state["pending_buys"] == {})


class BuyRpc:
    def __init__(self):
        self.pending = None
        self.sent = []

    def get_balance_lamports(self, owner):
        return 10 * 10 ** 9

    def get_token_balance_base(self, owner, mint):
        return 0, 6

    def send_transaction(self, raw_b64, skip_preflight=False):
        self.sent.append(raw_b64)
        raise RuntimeError("sendTransaction timeout")


def test_buy_signature_saved_before_send():
    print("== BUY: signature cua bot ghi ben vung TRUOC sendTransaction ==")
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    import base64
    kp = Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    unsigned = VersionedTransaction(msg, [kp])
    txb64 = base64.b64encode(bytes(unsigned)).decode()
    expected = str(unsigned.signatures[0])
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {"NEWTOK": 0.001})
        tr.dry = False
        rpc = BuyRpc()
        # token_safety tat: test nay chi kiem thu tu ghi signature/gui tx
        cfg = dict(tr.cfg, mode="live", buy_balance_verify_attempts=1,
                   buy_balance_verify_seconds=0, token_safety=False)
        jup = SellJup(price_map={"NEWTOK": 0.001})
        jup.swap_tx = lambda q, pk, fee: txb64
        sw = lt.Swapper(rpc, jup, kp, cfg, dry_run=False)
        sw.pubkey = "PUB"
        tr.swapper = sw
        seen = {}
        real_save = tr.save

        def save_spy():
            rec = tr.state["pending_buys"].get("tid_buy") or {}
            if rec.get("tx") and not rpc.sent:
                seen["saved_before_send"] = rec["tx"]
            real_save()
        tr.save = save_spy
        tr.entry_blocked = False
        sig = _sig("tid_buy", "NEWTOK", ts=1000)
        tr._attempt_signal(sig, 1000)
        rec = tr.state["pending_buys"].get("tid_buy") or {}
        check("signature ghi + save truoc khi gui",
              seen.get("saved_before_send") == expected, str(seen))
        check("send timeout -> pending giu tx cua bot, status uncertain",
              rec.get("tx") == expected and rec.get("status")
              == "buy_uncertain" and rpc.sent, str(rec))
        check("hook duoc go sau lenh", sw.on_buy_sent is None)


if __name__ == "__main__":
    test_exit_tp_ladder()
    test_exit_sl()
    test_trailing_truoc_sl()
    test_smart_exit()
    test_time_stop()
    test_sizing()
    test_decide_no_crash_on_bad_price()
    test_dry_run_no_send()
    test_live_no_key_fails()
    test_key_mismatch_fails()
    test_key_match_ok()
    test_stop_file()
    test_run_once_dry_e2e()
    test_pause_still_manages_exits()
    test_pause_smart_exit()
    test_no_dead_sells_source()
    test_time_stop_retry_after_fail()
    test_time_keep_retry_after_fail()
    test_unknown_error_rolls_back_tp()
    test_partial_sell_sizes_vs_wallet()
    test_tp1_tp2_same_poll()
    test_time_keep_after_tp1()
    test_failed_tp1_then_tp2_same_poll()
    test_price_impact_fraction()
    test_price_impact_guard_blocks()
    test_signal_age_helpers()
    test_stale_signal_skipped()
    test_retry_stops_when_stale()
    test_redact_secrets()
    test_rpc_error_redacted()
    test_capacity_ignores_airdrops()
    test_reconcile_live_airdrops()
    test_execute_sell_send_exception()
    test_uncertain_partial_landed_no_double_sell()
    test_uncertain_partial_not_landed_waits_then_retries()
    test_uncertain_pending_allows_full_sl()
    test_empty_wallet_no_fake_loss()
    test_reconcile_gone_writes_trade()
    test_pick_dexscreener_price()
    test_slow_buy_does_not_block_exits()
    test_exits_before_buys()
    test_age_uses_real_time_after_slow_buy()
    test_radar_stop_does_not_stop_live()
    test_pending_buy_legacy_not_landed_unblocks()
    test_pending_buy_with_tx_resolved_by_chain()
    test_pending_buy_landed_token_recovered()
    test_buy_signature_saved_before_send()
    print(f"\n{PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
