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
                 "TRADES_P", "LOG_P", "STOP_P"):
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
                    "priceImpactPct": "0.1"}
        # token -> USDC: dung de dinh gia
        px = self.price_map.get(in_mint, 0.001)
        tokens = amount_base / (10 ** self.DEC)
        return {"outAmount": str(int(tokens * px * 1e6)),
                "otherAmountThreshold": str(int(tokens * px * 1e6 * 0.99)),
                "priceImpactPct": "0.1"}

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
    print(f"\n{PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
