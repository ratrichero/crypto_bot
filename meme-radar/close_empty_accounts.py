#!/usr/bin/env python3
"""Dong token account RONG (so du 0) cua vi live -> lay lai rent (~0.002 SOL
moi account SPL, nhieu hon voi Token-2022).

live_trader tu thu hoi rent cho token ban SACH tu nay ve sau (reclaim_rent).
Script nay don cac account rong TON DONG tu truoc.

  python close_empty_accounts.py            # CHI liet ke (mac dinh, an toan)
  python close_empty_accounts.py --yes      # gui tx dong (can key live)
  python close_empty_accounts.py --yes --batch 8

An toan:
  - Chi dong account amount == 0. On-chain CloseAccount cung TU CHOI account
    con token -> khong the mat token.
  - Bo qua: SOL/wSOL, USDC/USDT, mint dang co vi the (live_positions.json),
    mint dang cho BUY (live_state.json pending_buys), mint live_trader dang tu
    dong (rent_reclaim co tx), account co closeAuthority khac vi, Token-2022
    con phi giu lai (withheldAmount > 0, CloseAccount se fail).
  - Chay song song voi live_trader duoc: neu bot mua lai dung token vua dong,
    tx swap cua Jupiter tu tao lai account (idempotent).
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import live_trader as lt  # noqa: E402  (nap .env)


def active_mints(positions, state):
    """Mint KHONG duoc dong: dang co vi the / dang cho BUY / bot dang dong."""
    out = {p.get("token") for p in positions or [] if p.get("token")}
    for rec in ((state or {}).get("pending_buys") or {}).values():
        tok = ((rec or {}).get("signal") or {}).get("token")
        if tok:
            out.add(tok)
    for mint, rec in ((state or {}).get("rent_reclaim") or {}).items():
        if (rec or {}).get("sig"):
            out.add(mint)
    return out


def _withheld(info):
    for ext in info.get("extensions") or []:
        if isinstance(ext, dict) and ext.get("extension") == "transferFeeAmount":
            try:
                return int((ext.get("state") or {}).get("withheldAmount") or 0)
            except (TypeError, ValueError):
                return 1
    return 0


def select_closable(rows, owner, active):
    """rows: getTokenAccountsByOwner(jsonParsed)['value'] (ca 2 program).
    Tra ve (closable, skipped) - closable: [{pubkey, program, lamports,
    amount, mint}], skipped: [(pubkey, mint, ly_do)]."""
    closable, skipped = [], []
    for item in rows:
        try:
            acct = item["account"]
            info = acct["data"]["parsed"]["info"]
            amount = int(info["tokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError):
            skipped.append((item.get("pubkey"), None, "khong parse duoc"))
            continue
        mint = info.get("mint")
        pk = item.get("pubkey")
        program = acct.get("owner")
        if amount != 0:
            continue
        if mint == lt.SOL_MINT or mint in lt.STABLE_MINTS:
            skipped.append((pk, mint, "SOL/stable"))
        elif mint in active:
            skipped.append((pk, mint, "dang co vi the/pending"))
        elif program not in (lt.TOKEN_PROGRAM, lt.TOKEN_2022_PROGRAM):
            skipped.append((pk, mint, f"program la {program}"))
        elif info.get("closeAuthority") not in (None, owner):
            skipped.append((pk, mint, "closeAuthority khac vi"))
        elif _withheld(info) > 0:
            skipped.append((pk, mint, "Token-2022 con phi giu lai"))
        elif info.get("state") == "frozen":
            skipped.append((pk, mint, "account bi dong bang"))
        else:
            closable.append({"pubkey": pk, "program": program,
                             "lamports": int(acct.get("lamports") or 0),
                             "amount": 0, "mint": mint})
    return closable, skipped


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _load(path, default):
    try:
        return json.load(open(path))
    except (FileNotFoundError, ValueError):
        return default


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--yes", action="store_true",
                    help="gui tx dong account (mac dinh chi liet ke)")
    ap.add_argument("--batch", type=int, default=8,
                    help="so account moi tx (1-10, mac dinh 8)")
    args = ap.parse_args(argv)
    batch = max(1, min(10, args.batch))
    cfg = lt.load_config()
    owner = cfg.get("wallet_address")
    if not owner:
        raise SystemExit("FATAL: config.live.json thieu wallet_address")
    helius = ""
    kp_path = os.path.join(lt.BASE, cfg["helius_key_file"])
    if os.path.exists(kp_path):
        helius = open(kp_path).read().strip()
    lt.register_secret(helius)
    rpc = lt.RpcClient(f"https://mainnet.helius-rpc.com/?api-key={helius}"
                       if helius else "https://api.mainnet-beta.solana.com")
    active = active_mints(_load(lt.POS_P, []), _load(lt.STATE_P, {}))
    rows = rpc._token_account_rows(owner)
    closable, skipped = select_closable(rows, owner, active)
    total = sum(a["lamports"] for a in closable)
    print(f"Vi {owner[:8]}...: {len(rows)} token account, {len(closable)} "
          f"rong dong duoc (~{total / 1e9:.5f} SOL), bo qua {len(skipped)}")
    for a in closable:
        print(f"  dong {a['pubkey']}  mint {a['mint']}  "
              f"{a['lamports'] / 1e9:.5f} SOL")
    for pk, mint, why in skipped:
        print(f"  bo qua {pk}  mint {mint}: {why}")
    if not closable:
        return 0
    if not args.yes:
        print("\nCHI LIET KE. Chay lai voi --yes de gui tx dong account.")
        return 0
    if cfg.get("mode") != "live":
        raise SystemExit("FATAL: --yes chi chay khi config mode=live (can key)")
    kp = lt.load_keypair(cfg)   # fail closed: thieu key / sai vi
    sw = lt.Swapper(rpc, None, kp, cfg, dry_run=False)
    got = 0
    for group in chunks(closable, batch):
        try:
            sig = sw.close_token_accounts(group)
        except Exception as e:
            print(f"  LOI gui tx ({len(group)} account): {lt.redact(e)[:200]}")
            continue
        status = None
        for _ in range(30):
            time.sleep(2)
            try:
                status = rpc.get_sig_status(sig)
            except Exception:
                status = None
            if status in ("confirmed", "finalized", "failed"):
                break
        lam = sum(a["lamports"] for a in group)
        if status in ("confirmed", "finalized"):
            got += lam
            print(f"  OK {sig[:16]}... dong {len(group)} account "
                  f"+{lam / 1e9:.5f} SOL")
        else:
            print(f"  {status or 'chua xac nhan'} {sig[:16]}... "
                  f"({len(group)} account) -> chay lai script de kiem tra")
    print(f"Tong thu hoi: {got / 1e9:.5f} SOL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
