"""Test chon pair DexScreener (dexscreener.pick_pair) + radar.ds_token.

radar.py doc config.json o top-level nen tach ds_token bang ast, stub requests.
Chay: cd meme-radar && python test_dexscreener.py
"""
import ast
import os
import time

from dexscreener import pick_pair

HERE = os.path.dirname(os.path.abspath(__file__))
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT = "MiNt1111111111111111111111111111111111pump"

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def pair(base, bsym, quote, qsym, price_usd, price_native, liq, mcap=None,
         chain="solana", addr="P"):
    row = {"chainId": chain, "pairAddress": addr,
           "baseToken": {"address": base, "symbol": bsym},
           "quoteToken": {"address": quote, "symbol": qsym},
           "priceUsd": str(price_usd), "priceNative": str(price_native),
           "liquidity": {"usd": liq}}
    if mcap is not None:
        row["marketCap"] = mcap
    return row


# Gia SOL = 150. rows[0] la BONK/SOL (SOL la quote) -> priceUsd = gia BONK.
SOL_ROWS = [
    pair("BONK", "BONK", SOL, "SOL", 0.00002, 0.000000133333, 5_000_000),
    pair(SOL, "SOL", USDC, "USDC", 150.0, 150.0, 20_000_000, addr="SOLUSDC"),
    pair(SOL, "SOL", "USDT", "USDT", 149.0, 149.0, 1_000, addr="RAC"),
]


def test_pick_pair():
    print("== pick_pair ==")
    p = pick_pair(SOL_ROWS, SOL)
    check("gia SOL lay tu pair SOL la base, thanh khoan cao nhat",
          p and p["price"] == 150.0 and p["pair"] == "SOLUSDC", p)
    check("rows[0] (BONK/SOL) se cho gia sai neu dung",
          float(SOL_ROWS[0]["priceUsd"]) != 150.0)
    only_quote = [SOL_ROWS[0]]
    p = pick_pair(only_quote, SOL)
    check("chi co pair SOL la quote -> suy priceUsd/priceNative ~150",
          p and abs(p["price"] - 150.0) < 0.01 and not p["as_base"]
          and p["symbol"] == "SOL" and p["mcap"] == 0.0, p)
    rows = [
        pair(MINT, "MEME", SOL, "SOL", 0.5, 0.0033, 300, mcap=1_000),
        pair(MINT, "MEME", SOL, "SOL", 0.01, 0.0000667, 80_000, mcap=10_000_000,
             addr="MAIN"),
        pair(MINT, "MEME", SOL, "SOL", 9.0, 0.06, 900_000, chain="ethereum"),
    ]
    p = pick_pair(rows, MINT)
    check("token meme: chon pool thanh khoan cao, bo chain khac",
          p and p["pair"] == "MAIN" and p["mcap"] == 10_000_000, p)
    fdv = [dict(pair(MINT, "MEME", SOL, "SOL", 0.01, 0.0001, 10), fdv=5000)]
    check("khong co marketCap -> dung fdv", pick_pair(fdv, MINT)["mcap"] == 5000)
    junk = [None, 1, {"baseToken": None},
            pair(MINT, "MEME", SOL, "SOL", "abc", 0, 10),
            pair("OTHER", "O", SOL, "SOL", 1, 1, 10)]
    check("du lieu rac / khong lien quan -> None", pick_pair(junk, MINT) is None)
    check("khong phai list -> None", pick_pair({"pairs": []}, MINT) is None)
    check("list rong -> None", pick_pair([], MINT) is None)


class FakeResp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeRequests:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return FakeResp(self.data)


def load_ds_token(fake_requests):
    src = open(os.path.join(HERE, "radar.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in ("ds_info", "ds_token", "ds_price")]
    ns = {"time": time, "requests": fake_requests, "pick_pair": pick_pair,
          "_price_cache": {}}
    exec(compile(ast.Module(body=body, type_ignores=[]), "radar.py", "exec"), ns)
    return ns, src


def test_radar_ds_token():
    print("== radar.ds_token khong dung rows[0] ==")
    fr = FakeRequests(SOL_ROWS)
    ns, src = load_ds_token(fr)
    px, sym, mcap = ns["ds_token"](SOL)
    check("gia SOL = 150 (khong phai gia BONK)", px == 150.0, px)
    check("symbol SOL", sym == "SOL", sym)
    ns["ds_token"](SOL)
    check("cache 60s", fr.calls == 1, fr.calls)
    ns2, _ = load_ds_token(FakeRequests([pair("X", "X", SOL, "SOL", 1, 1, 9)]))
    check("khong co pair cho mint -> (None, '?', 0)",
          ns2["ds_token"](MINT) == (None, "?", 0))
    ns3, _ = load_ds_token(FakeRequests(
        [pair(MINT, "meme", SOL, "SOL", 0.01, 0.0001, 80_000, mcap=123456)]))
    check("token: gia/symbol upper/mcap", ns3["ds_token"](MINT)
          == (0.01, "MEME", 123456.0), ns3["ds_token"](MINT))
    check("radar.py khong con rows[0]", "rows[0]" not in src)


if __name__ == "__main__":
    test_pick_pair()
    test_radar_ds_token()
    print(f"{PASSED} passed, {FAILED} failed")
    raise SystemExit(1 if FAILED else 0)
