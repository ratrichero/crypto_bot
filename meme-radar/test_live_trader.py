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
    for name in ("SIG_P", "ALERT_P", "SELL_P", "POS_P", "STATE_P",
                 "TRADES_P", "LOG_P", "STOP_P", "PAUSE_P"):
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
        # token -> USDC: dung de dinh gia
        px = self.price_map.get(in_mint, 0.001)
        tokens = amount_base / (10 ** self.DEC)
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

    def execute_sell(self, mint, frac, symbol="?"):
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

    def execute_sell(self, mint, frac, symbol="?"):
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
    print(f"\n{PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
