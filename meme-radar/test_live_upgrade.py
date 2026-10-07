#!/usr/bin/env python3
"""Test nang cap live_trader (meme radar 1->6):
  (1) thu hoi rent token account   (2) loc token nguy hiem + lo khu hoi
  (3) chong mua duoi vi nguon      (4) thoat khan cap (nang bac slippage)
  (5) gia batch Price API v3       (6) copy exit theo vi nguon
  + Jupiter API key / tach rent khoi gia vao.

Chay: python test_live_upgrade.py   (offline, khong key, khong mang)
"""
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import live_trader as lt  # noqa: E402
from test_live_trader import (FakeJup, FakeRpc, _dry_trader,  # noqa: E402
                              _sig, isolated, mkpos)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def mint_value(info=None, program=None):
    base = {"decimals": 6, "mintAuthority": None, "freezeAuthority": None}
    base.update(info or {})
    return {"owner": program or lt.TOKEN_PROGRAM,
            "data": {"parsed": {"type": "mint", "info": base}}}


def ext(name, state=None):
    return {"extension": name, "state": state or {}}


# ------------------------------------------------------------ (2) an toan

def test_token_risk_reasons():
    print("== (2) token_risk_reasons ==")
    C = dict(lt.DEFAULTS)
    R = lt.token_risk_reasons
    check("mint sach -> []", R(mint_value(), C) == [])
    check("freeze authority", R(mint_value({"freezeAuthority": "X"}), C)
          == ["freeze_authority"])
    check("mint authority", R(mint_value({"mintAuthority": "X"}), C)
          == ["mint_authority"])
    check("tat reject_freeze -> bo qua",
          R(mint_value({"freezeAuthority": "X"}),
            dict(C, reject_freeze_authority=False)) == [])
    fee0 = ext("transferFeeConfig", {
        "olderTransferFee": {"transferFeeBasisPoints": 0},
        "newerTransferFee": {"transferFeeBasisPoints": 0}})
    fee = ext("transferFeeConfig", {
        "olderTransferFee": {"transferFeeBasisPoints": 0},
        "newerTransferFee": {"transferFeeBasisPoints": 300}})
    check("transferFee 0 bps -> ok", R(mint_value({"extensions": [fee0]}), C)
          == [])
    check("transferFee 300 bps -> tu choi",
          R(mint_value({"extensions": [fee]}), C) == ["transfer_fee_300bps"])
    check("transferHook khong programId -> ok",
          R(mint_value({"extensions": [ext("transferHook",
                                           {"programId": None})]}), C) == [])
    check("transferHook co programId -> tu choi",
          R(mint_value({"extensions": [ext("transferHook",
                                           {"programId": "H"})]}), C)
          == ["transfer_hook"])
    check("permanentDelegate",
          R(mint_value({"extensions": [ext("permanentDelegate",
                                           {"delegate": "D"})]}), C)
          == ["permanent_delegate"])
    check("defaultAccountState initialized -> ok",
          R(mint_value({"extensions": [ext("defaultAccountState",
                                           {"accountState": "initialized"})]}),
            C) == [])
    check("defaultAccountState frozen -> tu choi",
          R(mint_value({"extensions": [ext("defaultAccountState",
                                           {"accountState": "frozen"})]}), C)
          == ["default_frozen"])
    check("nonTransferable / pausableConfig",
          R(mint_value({"extensions": [ext("nonTransferable"),
                                       ext("pausableConfig")]}), C)
          == ["nonTransferable", "pausableConfig"])
    check("metadataPointer/tokenMetadata khong bi chan",
          R(mint_value({"extensions": [ext("metadataPointer"),
                                       ext("tokenMetadata")]},
                       lt.TOKEN_2022_PROGRAM), C) == [])
    check("khong parse duoc -> unparsed_mint (fail closed)",
          R({"data": "base64..."}, C) == ["unparsed_mint"] and R(None, C)
          == ["unparsed_mint"])
    check("khong phai mint", R({"data": {"parsed": {"type": "account",
                                                    "info": {}}}}, C)
          == ["not_a_mint"])


class RiskRpc(FakeRpc):
    def __init__(self, value=None, exc=None):
        self.value, self.exc = value, exc

    def get_mint_info(self, mint, max_age=300):
        if self.exc:
            raise self.exc
        return self.value


class LossyJup(FakeJup):
    """Ban lai chi nhan `back` phan SOL (pool mong/thue)."""

    def __init__(self, back=1.0, sell_exc=None, **kw):
        super().__init__(**kw)
        self.back, self.sell_exc = back, sell_exc

    def quote(self, in_mint, out_mint, amount_base, slippage_bps):
        if out_mint == lt.SOL_MINT and in_mint != lt.SOL_MINT:
            if self.sell_exc:
                raise self.sell_exc
            q = super().quote(in_mint, out_mint, amount_base, slippage_bps)
            q["outAmount"] = str(int(int(q["outAmount"]) * self.back))
            return q
        return super().quote(in_mint, out_mint, amount_base, slippage_bps)


def _sw(jup=None, rpc=None, **cfg):
    c = dict(lt.DEFAULTS, mode="dry_run", **cfg)
    return lt.Swapper(rpc or FakeRpc(), jup or FakeJup({"MINT": 0.001}), None,
                      c, dry_run=True)


def _raises(fn, exc_type):
    try:
        fn()
    except exc_type as e:
        return e
    except Exception as e:  # sai loai
        return ("wrong", e)
    return None


def test_entry_checks():
    print("== (2)(3) kiem tra quote mua: honeypot / lo khu hoi / mua duoi ==")
    sw = _sw(rpc=RiskRpc(mint_value({"freezeAuthority": "F"})))
    e = _raises(lambda: sw.execute_buy("MINT", 10, "T"), lt.EntryRejected)
    check("freeze authority -> EntryRejected unsafe_token",
          isinstance(e, lt.EntryRejected) and e.reason == "unsafe_token"
          and "freeze_authority" in str(e), e)
    sw = _sw(rpc=RiskRpc(exc=RuntimeError("rpc down")))
    e = _raises(lambda: sw.execute_buy("MINT", 10, "T"), lt.SwapError)
    check("khong doc duoc mint -> SwapError (retry), KHONG EntryRejected",
          isinstance(e, lt.SwapError)
          and not isinstance(e, lt.EntryRejected), e)
    r = _sw().execute_buy("MINT", 10, "T")
    check("mint sach + pool tot -> mua (dry), ghi round_trip",
          r["dry"] and abs(r["round_trip_loss_pct"]) < 0.1, r)
    sw = _sw(jup=LossyJup(back=0.90, price_map={"MINT": 0.001}))
    e = _raises(lambda: sw.execute_buy("MINT", 10, "T"), lt.EntryRejected)
    check("lo khu hoi 10% > 6% -> round_trip",
          isinstance(e, lt.EntryRejected) and e.reason == "round_trip", e)
    sw = _sw(jup=LossyJup(back=0.90, price_map={"MINT": 0.001}),
             max_round_trip_loss_pct=0)
    check("max_round_trip_loss_pct=0 -> tat", sw.execute_buy("MINT", 10, "T")
          ["dry"])
    sw = _sw(jup=LossyJup(sell_exc=lt.NoRoute("no route"),
                          price_map={"MINT": 0.001}))
    e = _raises(lambda: sw.execute_buy("MINT", 10, "T"), lt.EntryRejected)
    check("khong co route ban -> no_sell_route (honeypot)",
          isinstance(e, lt.EntryRejected) and e.reason == "no_sell_route", e)

    class Resp:
        def __init__(self, code):
            self.status_code = code
    http400 = lt.requests.HTTPError("400 Bad Request")
    http400.response = Resp(400)
    sw = _sw(jup=LossyJup(sell_exc=http400, price_map={"MINT": 0.001}))
    e = _raises(lambda: sw.execute_buy("MINT", 10, "T"), lt.EntryRejected)
    check("HTTP 400 (khong route) -> no_sell_route",
          isinstance(e, lt.EntryRejected) and e.reason == "no_sell_route", e)
    http500 = lt.requests.HTTPError("500")
    http500.response = Resp(500)
    sw = _sw(jup=LossyJup(sell_exc=http500, price_map={"MINT": 0.001}))
    e = _raises(lambda: sw.execute_buy("MINT", 10, "T"), lt.SwapError)
    check("HTTP 500 -> SwapError retry (khong tu choi vinh vien)",
          isinstance(e, lt.SwapError)
          and not isinstance(e, lt.EntryRejected), e)
    # Chong mua duoi: gia quote ~0.001
    e = _raises(lambda: _sw().execute_buy("MINT", 10, "T",
                                          ref_price_usd=0.0005),
                lt.EntryRejected)
    check("gia cao hon vi nguon +100% > 20% -> chase",
          isinstance(e, lt.EntryRejected) and e.reason == "chase", e)
    r = _sw().execute_buy("MINT", 10, "T", ref_price_usd=0.00095)
    check("+5% -> mua, ghi premium_pct", 4 < r["premium_pct"] < 6
          and r["wallet_price_usd"] == 0.00095, r)
    r = _sw(max_entry_premium_pct=0).execute_buy("MINT", 10, "T",
                                                 ref_price_usd=0.0001)
    check("max_entry_premium_pct=0 -> tat", r["dry"])
    r = _sw(token_safety=False, max_round_trip_loss_pct=0).execute_buy(
        "MINT", 10, "T")
    check("tat het kiem tra -> mua nhu cu", r["dry"] and r["decimals"] == 6)


def test_entry_reject_marks_signal():
    print("== EntryRejected -> skipped_<reason>, khong retry, khong block ==")
    with isolated() as tmpd:
        tr, jup = _dry_trader(tmpd, {"NEWTOK": 0.001})
        tr.entry_blocked = False
        s = _sig("tid_chase", "NEWTOK", ts=1000)
        s["wallet_price_usd"] = 0.0004
        ok = tr._attempt_signal(s, 1000)
        check("khong mo", not ok and tr.positions == [])
        check("da processed (khong retry)", "tid_chase"
              in tr.state["processed"]
              and "tid_chase" not in tr.state.get("signal_failures", {}))
        check("dem entry_rejects.chase", tr.state["entry_rejects"]
              == {"chase": 1}, tr.state.get("entry_rejects"))
        check("khong pending / khong block", not tr.state.get(
            "pending_buys", {}).get("tid_chase") and not tr.entry_blocked)
        s2 = _sig("tid_ok", "NEWTOK", ts=1000)
        s2["wallet_price_usd"] = 0.00098
        s2["liquidity_usd"] = 12345
        tr._attempt_signal(s2, 1000)
        p = tr.positions[0] if tr.positions else {}
        check("signal hop le -> mo, luu wallet_price/premium/liquidity",
              p.get("wallet_price_usd") == 0.00098
              and p.get("entry_premium_pct") is not None
              and p.get("liquidity_usd") == 12345
              and p.get("signal_ts") == 1000, p)


# ------------------------------------------------------------ (1) rent

class RentRpc:
    def __init__(self, before, spent_lamports, accts=None, acct_exc=None):
        self.after = before - spent_lamports
        self.accts = accts or []
        self.acct_exc = acct_exc

    def get_balance_lamports(self, owner):
        return self.after

    def get_token_accounts_for_mint(self, owner, mint):
        if self.acct_exc:
            raise self.acct_exc
        return self.accts


def test_rent_split():
    print("== (1) tach rent token account khoi gia vao ==")
    rent = 2039280
    sol = 150.0
    swap = int(10 / sol * 1e9)
    rpc = RentRpc(10 ** 10, swap + rent, [{"lamports": rent, "amount": 1}])
    sw = lt.Swapper(rpc, FakeJup(), None, dict(lt.DEFAULTS), dry_run=False)
    r = sw._buy_result("SIG", "T", 10, sol, 10 ** 10, 10_000 * 10 ** 6, 6,
                       mint="M", had_account=False)
    check("cost khong gom rent (~$10)", abs(r["cost_usd"] - 10) < 0.01, r)
    check("rent_lamports/rent_usd", r["rent_lamports"] == rent
          and abs(r["rent_usd"] - rent / 1e9 * sol) < 1e-3, r)
    check("entry = cost / token", abs(r["entry_usd"] * 10_000 / r["cost_usd"]
                                      - 1) < 1e-5, r)
    r = sw._buy_result("SIG", "T", 10, sol, 10 ** 10, 10_000 * 10 ** 6, 6,
                       mint="M", had_account=True)
    check("da co account truoc -> khong tach rent", r["rent_lamports"] == 0
          and r["cost_usd"] > 10.3, r)
    rpc.acct_exc = RuntimeError("x")
    r = sw._buy_result("SIG", "T", 10, sol, 10 ** 10, 10_000 * 10 ** 6, 6,
                       mint="M", had_account=False)
    check("loi doc account -> tinh nhu cu (khong crash)",
          r["rent_lamports"] == 0, r)


class ReclaimRpc:
    def __init__(self):
        self.accts = {}
        self.status = {}

    def get_token_accounts_for_mint(self, owner, mint):
        return list(self.accts.get(mint, []))

    def get_sig_status(self, sig):
        return self.status.get(sig)


class ReclaimSwapper:
    def __init__(self, rpc):
        self.rpc = rpc
        self.pubkey = "PUB"
        self.closed = []

    def close_token_accounts(self, accounts):
        self.closed.append([a["pubkey"] for a in accounts])
        return f"CLOSE{len(self.closed)}"


def test_rent_reclaim_queue():
    print("== (1) hang doi thu hoi rent ==")
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {})
        tr._queue_rent_reclaim("M1", 0)
        check("dry -> khong xep hang", not tr.state.get("rent_reclaim"))
        tr.dry = False
        rpc = ReclaimRpc()
        sw = ReclaimSwapper(rpc)
        tr.swapper = sw
        tr._queue_rent_reclaim(lt.SOL_MINT, 0)
        check("khong dong SOL", not tr.state.get("rent_reclaim"))
        rpc.accts["M1"] = [{"pubkey": "A1", "program": lt.TOKEN_PROGRAM,
                            "lamports": 2039280, "amount": 0}]
        tr._queue_rent_reclaim("M1", 100, 2039280)
        tr.process_rent_reclaim(100)
        rec = tr.state["rent_reclaim"]["M1"]
        check("gui CloseAccount account so du 0", sw.closed == [["A1"]]
              and rec["sig"] == "CLOSE1", rec)
        tr.process_rent_reclaim(101)
        check("chua confirm -> khong gui lai", len(sw.closed) == 1)
        rpc.status["CLOSE1"] = "confirmed"
        tr.process_rent_reclaim(102)
        check("confirmed -> xoa khoi hang doi", "M1"
              not in tr.state["rent_reclaim"])
        # con token (RPC tre) -> retry, sau give_up bo
        rpc.accts["M2"] = [{"pubkey": "A2", "program": lt.TOKEN_PROGRAM,
                            "lamports": 2039280, "amount": 5}]
        tr._queue_rent_reclaim("M2", 200)
        tr.process_rent_reclaim(200)
        check("con token -> khong dong, cho retry", len(sw.closed) == 1
              and tr.state["rent_reclaim"]["M2"]["retry_at"] == 230)
        tr.process_rent_reclaim(200 + 901)
        check("qua reclaim_give_up_seconds -> bo", "M2"
              not in tr.state["rent_reclaim"])
        # mua lai token dang cho dong -> bo khoi hang doi
        tr._queue_rent_reclaim("M3", 300)
        tr.positions.append(dict(mkpos(), token="M3"))
        tr.process_rent_reclaim(300)
        check("dang co vi the -> khong dong", "M3"
              not in tr.state["rent_reclaim"] and len(sw.closed) == 1)
        tr.positions.clear()
        # tx dong that bai -> gui lai sau retry
        rpc.accts["M4"] = [{"pubkey": "A4", "program": lt.TOKEN_PROGRAM,
                            "lamports": 1, "amount": 0}]
        tr._queue_rent_reclaim("M4", 400)
        tr.process_rent_reclaim(400)
        rpc.status["CLOSE2"] = "failed"
        tr.process_rent_reclaim(401)
        tr.process_rent_reclaim(432)
        check("failed -> gui lai", sw.closed[-1] == ["A4"]
              and len(sw.closed) == 3, sw.closed)
        # account da dong -> xoa
        tr._queue_rent_reclaim("M5", 500)
        tr.process_rent_reclaim(500)
        check("khong con account -> xoa", "M5"
              not in tr.state["rent_reclaim"])
        # gioi han moi vong
        for i in range(5):
            rpc.accts[f"N{i}"] = [{"pubkey": f"B{i}", "amount": 0,
                                   "program": lt.TOKEN_PROGRAM,
                                   "lamports": 1}]
            tr._queue_rent_reclaim(f"N{i}", 600)
        n0 = len(sw.closed)
        tr.process_rent_reclaim(600)
        check("reclaim_max_per_loop = 2", len(sw.closed) - n0 == 2)


def test_close_token_accounts_tx():
    print("== (1) tx CloseAccount (solders) ==")
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    class TxRpc:
        sent = []

        def get_latest_blockhash(self):
            return "11111111111111111111111111111111"

        def send_transaction(self, raw, skip_preflight=False):
            self.sent.append(raw)
            return "x"
    kp = Keypair()
    rpc = TxRpc()
    sw = lt.Swapper(rpc, FakeJup(), kp, dict(lt.DEFAULTS), dry_run=False)
    acct = str(Keypair().pubkey())
    acct2 = str(Keypair().pubkey())
    sig = sw.close_token_accounts([
        {"pubkey": acct, "program": lt.TOKEN_PROGRAM, "amount": 0},
        {"pubkey": acct2, "program": lt.TOKEN_2022_PROGRAM, "amount": 0}])
    tx = VersionedTransaction.from_bytes(base64.b64decode(rpc.sent[0]))
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    ixs = msg.instructions
    check("signature tra ve = chu ky tx", sig == str(tx.signatures[0]))
    check("2 instruction, data [9]", len(ixs) == 2
          and all(bytes(i.data) == bytes([9]) for i in ixs))
    progs = [keys[i.program_id_index] for i in ixs]
    check("dung program SPL + Token-2022", progs == [lt.TOKEN_PROGRAM,
                                                   lt.TOKEN_2022_PROGRAM])
    accs = [[keys[j] for j in bytes(i.accounts)] for i in ixs]
    owner = str(kp.pubkey())
    check("accounts [account, dest=owner, owner]", accs[0] == [acct, owner,
                                                               owner], accs)
    check("fee payer = owner, ky", keys[0] == owner
          and msg.header.num_required_signatures == 1)
    e = _raises(lambda: sw.close_token_accounts(
        [{"pubkey": acct, "program": lt.TOKEN_PROGRAM, "amount": 3}]),
        lt.SwapError)
    check("account con token -> tu choi", isinstance(e, lt.SwapError))
    e = _raises(lambda: sw.close_token_accounts(
        [{"pubkey": acct, "program": "Other111", "amount": 0}]), lt.SwapError)
    check("program la -> tu choi", isinstance(e, lt.SwapError))
    sw.dry = True
    e = _raises(lambda: sw.close_token_accounts(
        [{"pubkey": acct, "program": lt.TOKEN_PROGRAM, "amount": 0}]),
        lt.SwapError)
    check("dry -> khong gui", isinstance(e, lt.SwapError)
          and len(rpc.sent) == 1)


# ------------------------------------------------------------ (4) thoat

def test_exit_tier_params():
    print("== (4) exit_tier_params ==")
    C = dict(lt.DEFAULTS, sell_slippage_bps=300, sell_max_price_impact_pct=10)
    check("bac 0 = cau hinh ban thuong", lt.exit_tier_params(C, 0)
          == (300, 10.0, None))
    s, i, f = lt.exit_tier_params(C, 1)
    check("bac 1", s == 1000 and i == 30 and f["auto"])
    check("bac vuot -> bac cuoi", lt.exit_tier_params(C, 9)[0] == 2500)
    check("khong cau hinh bac -> bac 0", lt.exit_tier_params(
        dict(C, exit_escalation=[]), 2) == (300, 10.0, None))
    sw = _sw()
    check("priority fee auto -> priorityLevelWithMaxLamports",
          sw._priority_fee({"auto": True, "max_lamports": 5})
          == {"priorityLevelWithMaxLamports": {"maxLamports": 5,
                                               "priorityLevel": "veryHigh"}})
    check("priority fee None -> cau hinh goc", sw._priority_fee(None)
          == int(lt.DEFAULTS["priority_fee_lamports"]))


class TierSwapper:
    def __init__(self, fails=1):
        self.fails = fails
        self.tiers = []

    def execute_sell(self, mint, frac, symbol="?", tier=0):
        self.tiers.append(tier)
        if self.fails > 0:
            self.fails -= 1
            raise lt.SwapError("price impact 25% > max 10%")
        return {"sold_base": 0, "proceeds_usd": 0.0, "tx": None,
                "dry": True, "simulated": True}


def test_exit_escalation():
    print("== (4) SL that bai -> nang bac, thu lai ngay ==")
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {"MINT": 0.7})
        sw = TierSwapper(fails=2)
        tr.swapper = sw
        tr.positions = [mkpos(opened_at=0)]
        tr.cfg["price_poll_seconds"] = 20
        tr.manage_one(tr.positions[0], 100)
        p = tr.positions[0]
        check("lan 1 bac 0, that bai -> exit_tier 1", sw.tiers == [0]
              and p["exit_tier"] == 1, (sw.tiers, p.get("exit_tier")))
        tr.manage_one(p, 101)
        check("thu lai NGAY (khong cho price_poll) o bac 1", sw.tiers == [0, 1])
        check("that bai bac 1 -> bac 2", p["exit_tier"] == 2)
        tr.manage_one(p, 102)
        check("bac 2 thanh cong -> dong", sw.tiers == [0, 1, 2]
              and tr.positions == [])
        rec = [json.loads(x) for x in open(lt.TRADES_P)][-1]
        check("trade ghi exit_tier_used 2", rec["exit_tier_used"] == 2
              and rec["reason"] == "stop_loss", rec)
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {"MINT": 1.6})
        sw = TierSwapper(fails=1)
        tr.swapper = sw
        tr.positions = [mkpos(opened_at=0)]
        tr.manage_one(tr.positions[0], 100)
        check("TP1 that bai -> KHONG nang bac", sw.tiers == [0]
              and not tr.positions[0].get("exit_tier"))


# ------------------------------------------------------------ (5) gia

class BatchJup(FakeJup):
    def __init__(self, batch=None, exc=None, **kw):
        super().__init__(**kw)
        self.batch = batch or {}
        self.exc = exc
        self.batch_calls = []

    def token_price_usd(self, mint, decimals):
        self.quotes += 1   # gia tung token = 1 quote Jupiter
        return super().token_price_usd(mint, decimals)

    def prices_usd(self, mints):
        self.batch_calls.append(list(mints))
        if self.exc:
            raise self.exc
        return {m: self.batch[m] for m in mints if m in self.batch}


def _batch_trader(tmpd, jup):
    tr, _ = _dry_trader(tmpd, {})
    tr.jup = jup
    tr.cfg["price_poll_seconds"] = 20
    a = dict(mkpos(opened_at=0), token="A", symbol="A")
    b = dict(mkpos(opened_at=0), token="B", symbol="B")
    tr.positions = [a, b]
    return tr


def test_batch_prices():
    print("== (5) Price API batch ==")
    with isolated() as tmpd:
        jup = BatchJup(batch={"A": 1.0, "B": 1.0},
                       price_map={"A": 1.0, "B": 1.0})
        tr = _batch_trader(tmpd, jup)
        tr.manage_positions(100)
        check("1 request cho 2 vi the, khong quote tung token",
              jup.batch_calls == [["A", "B"]] and jup.quotes == 0,
              (jup.batch_calls, jup.quotes))
        jup.batch["A"] = 0.7
        tr.manage_positions(110)
        check("cache trong price_poll_seconds", len(jup.batch_calls) == 1)
        tr.manage_positions(121)
        check("het cache -> batch moi, SL A theo gia batch",
              len(jup.batch_calls) == 2
              and [p["token"] for p in tr.positions] == ["B"])
    with isolated() as tmpd:
        jup = BatchJup(batch={"A": 1.0}, price_map={"A": 1.0, "B": 1.0})
        tr = _batch_trader(tmpd, jup)
        tr.manage_positions(100)
        q1 = jup.quotes
        check("B khong co trong batch -> quote fallback", q1 == 1, q1)
        tr.manage_positions(121)
        check("fallback gian cach price_fallback_seconds (20s)",
              jup.quotes == 2, jup.quotes)
        tr.cfg["price_fallback_seconds"] = 60
        tr.manage_positions(142)
        check("chua du 60s -> khong quote", jup.quotes == 2, jup.quotes)
    with isolated() as tmpd:
        jup = BatchJup(exc=RuntimeError("429"), price_map={"A": 1.0,
                                                          "B": 1.0})
        tr = _batch_trader(tmpd, jup)
        tr.manage_positions(100)
        check("batch loi -> quote tung token nhu cu", jup.quotes == 2)
    with isolated() as tmpd:
        jup = BatchJup(batch={"A": 1.0, "B": 1.0},
                       price_map={"A": 1.0, "B": 1.0})
        tr = _batch_trader(tmpd, jup)
        tr.cfg["price_batch"] = False
        tr.manage_positions(100)
        check("price_batch=false -> khong goi batch", jup.batch_calls == []
              and jup.quotes == 2)


class FakeResp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_jupiter_client():
    print("== (5) JupiterClient: Price v3 + API key ==")
    calls = []

    def get(url, params=None, timeout=None, headers=None):
        calls.append((url, params, headers))
        ids = params["ids"].split(",")
        return FakeResp({m: {"usdPrice": 0.5, "liquidity": 1}
                         for m in ids if m != "GONE"} | {"BAD": {}})
    j = lt.JupiterClient("https://api.jup.ag", http_get=get, api_key="K" * 12)
    mints = [f"M{i}" for i in range(60)] + ["GONE", "M0"]
    px = j.prices_usd(mints)
    check("chia lo <= 50 id", len(calls) == 2
          and len(calls[0][1]["ids"].split(",")) == 50)
    check("url /price/v3 + header x-api-key", calls[0][0]
          == "https://api.jup.ag/price/v3"
          and calls[0][2] == {"x-api-key": "K" * 12})
    check("token bi Jupiter bo -> khong co trong ket qua", "GONE" not in px
          and len(px) == 60 and px["M5"] == 0.5)
    j2 = lt.JupiterClient("https://lite-api.jup.ag", http_get=get)
    check("khong key -> khong header", j2._headers() is None)
    with isolated() as tmpd:
        old = os.environ.pop("JUPITER_API_KEY", None)
        try:
            j = lt.LiveTrader._make_jupiter(dict(lt.DEFAULTS))
            check("khong key -> giu lite-api", "lite-api" in j.base
                  and j.api_key is None)
            open(os.path.join(tmpd, ".jupiter_key"), "w").write("FILEKEY123\n")
            j = lt.LiveTrader._make_jupiter(dict(lt.DEFAULTS))
            check("file .jupiter_key -> api.jup.ag", j.base
                  == "https://api.jup.ag" and j.api_key == "FILEKEY123")
            os.environ["JUPITER_API_KEY"] = "ENVKEY12345"
            j = lt.LiveTrader._make_jupiter(dict(
                lt.DEFAULTS, jupiter_min_interval_seconds=2))
            check("env uu tien + min_interval", j.api_key == "ENVKEY12345"
                  and j.min_interval == 2)
            check("key bi che trong log", "ENVKEY12345"
                  not in lt.redact("x ENVKEY12345 y"))
        finally:
            os.environ.pop("JUPITER_API_KEY", None)
            if old is not None:
                os.environ["JUPITER_API_KEY"] = old


# ------------------------------------------------------------ (6) copy exit

def test_copy_exit():
    print("== (6) copy exit theo vi nguon ==")
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {"MINT": 1.1})
        p = dict(mkpos(opened_at=1000), signal_ts=1000)
        tr.positions = [p]
        tr._on_wallet_sell({"type": "wallet_sell", "token": "MINT",
                            "wallet": "W", "sold_frac": 0.3, "tid": "s1",
                            "ts": 1100})
        check("ban 30% -> chua thoat", not p.get("copy_exit")
              and abs(p["src_remaining"] - 0.7) < 1e-9)
        tr._on_wallet_sell({"type": "wallet_sell", "token": "MINT",
                            "wallet": "W", "sold_frac": 0.3, "tid": "s1",
                            "ts": 1100})
        check("trung tid -> bo qua", abs(p["src_remaining"] - 0.7) < 1e-9)
        tr._on_wallet_sell({"type": "wallet_sell", "token": "MINT",
                            "wallet": "OTHER", "sold_frac": 1.0, "tid": "o1",
                            "ts": 1100})
        tr._on_wallet_sell({"type": "wallet_sell", "token": "MINT",
                            "wallet": "W", "sold_frac": 1.0, "tid": "old",
                            "ts": 900})
        check("vi khac / lenh truoc khi mua -> bo qua",
              not p.get("copy_exit") and abs(p["src_remaining"] - 0.7) < 1e-9)
        # nap qua alerts.jsonl
        with open(lt.ALERT_P, "a") as f:
            f.write(json.dumps({"type": "wallet_sell", "token": "MINT",
                                "wallet": "W", "sold_frac": 0.4, "tid": "s2",
                                "ts": 1200}) + "\n")
        tr.state["alert_offset"] = 0
        tr.ingest_alerts()
        check("cong don 58% >= 50% -> copy_exit", p.get("copy_exit") is True,
              p.get("src_remaining"))
        tr.manage_one(p, 1300)
        rec = [json.loads(x) for x in open(lt.TRADES_P)][-1]
        check("ban het, reason copy_exit", tr.positions == []
              and rec["reason"] == "copy_exit"
              and rec["legs"][-1]["why"] == "COPY_EXIT", rec)
    with isolated() as tmpd:
        tr, _ = _dry_trader(tmpd, {"MINT": 1.1})
        tr.cfg["copy_exit"] = False
        p = mkpos(opened_at=1000)
        tr.positions = [p]
        tr._on_wallet_sell({"token": "MINT", "wallet": "W", "sold_frac": 1.0,
                            "tid": "x", "ts": 1100})
        check("copy_exit=false -> tat", not p.get("copy_exit"))
    # strategy: SL uu tien hon copy exit; copy exit truoc time stop
    P = dict(lt.DEFAULTS)
    pos = dict(mkpos(opened_at=0), copy_exit=True)
    a, r = lt.decide_exits(pos, 0.7, 60, P)
    check("SL truoc COPY_EXIT", r == "stop_loss" and a[0][1] == "SL")
    pos = dict(mkpos(opened_at=0), copy_exit=True)
    a, r = lt.decide_exits(pos, 1.0, 481 * 60, P)
    check("COPY_EXIT truoc TIME", r == "copy_exit" and a == [(1.0,
                                                              "COPY_EXIT")])
    check("COPY_EXIT la lenh thoat bat buoc (duoc nang bac)",
          "COPY_EXIT" in lt.MUST_EXIT_WHYS)


def test_close_empty_script():
    print("== close_empty_accounts.select_closable ==")
    import close_empty_accounts as ce
    O = "OWNER"

    def row(pk, mint, amount, program=lt.TOKEN_PROGRAM, **info):
        i = {"mint": mint, "owner": O,
             "tokenAmount": {"amount": str(amount), "decimals": 6}}
        i.update(info)
        return {"pubkey": pk, "account": {"owner": program,
                                          "lamports": 2039280,
                                          "data": {"parsed": {"info": i}}}}
    rows = [row("A", "M1", 0), row("B", "M2", 5), row("C", lt.USDC_MINT, 0),
            row("D", lt.SOL_MINT, 0), row("E", "OPEN", 0),
            row("F", "M3", 0, closeAuthority="OTHER"),
            row("G", "M4", 0, lt.TOKEN_2022_PROGRAM, extensions=[
                {"extension": "transferFeeAmount",
                 "state": {"withheldAmount": 7}}]),
            row("H", "M5", 0, lt.TOKEN_2022_PROGRAM),
            row("I", "M6", 0, state="frozen"), {"pubkey": "J"}]
    active = ce.active_mints(
        [{"token": "OPEN"}],
        {"pending_buys": {"t": {"signal": {"token": "PEND"}}},
         "rent_reclaim": {"BOT": {"sig": "s"}, "IDLE": {}}})
    check("active = vi the + pending + bot dang dong",
          active == {"OPEN", "PEND", "BOT"}, active)
    ok, sk = ce.select_closable(rows, O, active)
    check("chi dong A (SPL) va H (Token-2022 khong phi)",
          [a["pubkey"] for a in ok] == ["A", "H"], ok)
    check("account dong co program/lamports/amount 0",
          ok[1]["program"] == lt.TOKEN_2022_PROGRAM
          and ok[0]["lamports"] == 2039280 and ok[0]["amount"] == 0)
    why = {pk: w for pk, _, w in sk}
    check("ly do bo qua", why.get("C") == "SOL/stable"
          and why.get("D") == "SOL/stable"
          and why.get("E") == "dang co vi the/pending"
          and why.get("F") == "closeAuthority khac vi"
          and why.get("G") == "Token-2022 con phi giu lai"
          and why.get("I") == "account bi dong bang"
          and why.get("J") == "khong parse duoc" and "B" not in why, why)
    check("chia lo", [len(c) for c in ce.chunks(list(range(19)), 8)]
          == [8, 8, 3])


def test_graceful_signal_shutdown():
    """pm2 stop/restart gui SIGINT: vong run_once dang chay phai xong,
    khong vong moi, main() tra ve binh thuong (khong KeyboardInterrupt)."""
    import signal as _signal
    print("\n[dung sach khi nhan SIGINT/SIGTERM]")
    calls = []

    class FakeTrader:
        def __init__(self, cfg):
            pass

        def run_once(self):
            calls.append("start")
            os.kill(os.getpid(), _signal.SIGINT)    # giua luc swap
            calls.append("done")
            return "ok"

    saved = (lt.LiveTrader, lt.load_config, lt._SHUTDOWN["signal"],
             _signal.getsignal(_signal.SIGINT),
             _signal.getsignal(_signal.SIGTERM))
    lt.LiveTrader = FakeTrader
    lt.load_config = lambda: {"mode": "dry_run", "trade_size_usd": 1,
                              "max_positions": 1, "slippage_bps": 1,
                              "loop_seconds": 30}
    lt._SHUTDOWN["signal"] = None
    err = None
    t0 = lt.time.time()
    try:
        lt.main()
    except BaseException as e:            # KeyboardInterrupt = loi
        err = e
    finally:
        lt.LiveTrader, lt.load_config, lt._SHUTDOWN["signal"] = saved[:3]
        _signal.signal(_signal.SIGINT, saved[3])
        _signal.signal(_signal.SIGTERM, saved[4])
    check("SIGINT giua run_once: vong do chay xong, khong vong moi",
          calls == ["start", "done"] and err is None, (calls, err))
    check("SIGINT: khong ngu het loop_seconds (30s)",
          lt.time.time() - t0 < 5, lt.time.time() - t0)
    lt._SHUTDOWN["signal"] = None
    slept = lt.time.time()
    lt.sleep_unless_shutdown(0.2)
    check("sleep_unless_shutdown ngu du khi khong co tin hieu",
          lt.time.time() - slept >= 0.19)


if __name__ == "__main__":
    test_token_risk_reasons()
    test_entry_checks()
    test_entry_reject_marks_signal()
    test_rent_split()
    test_rent_reclaim_queue()
    test_close_token_accounts_tx()
    test_exit_tier_params()
    test_exit_escalation()
    test_batch_prices()
    test_jupiter_client()
    test_copy_exit()
    test_close_empty_script()
    test_graceful_signal_shutdown()
    print(f"\n{PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)
