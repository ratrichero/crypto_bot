import json, os, requests

BASE = os.path.dirname(os.path.abspath(__file__))

# Quy tac universe (theo y Cuong 06/10/2026):
# - Top 50 von hoa, chi coin "that": loai stablecoin/wrapped,
#   loai meme NHO (chi giu DOGE/SHIB/PEPE/PUMP la meme lon co nen tang),
#   loai coin hype moi (denylist tay), loai coin dang bom/xa (|24h| > 40%).
SKIP = {"usdt","usdc","dai","usde","fdusd","tusd","pyusd","usdd","frax","susde",
        "wbtc","steth","wsteth","weeth","cbeth","reth","weth","wbnb"}
DENY = {"WLFI","PI","VVV","NIGHT","STABLE","LIT","CC","ASTER"}  # coin hype moi
MEME_KEEP = {"DOGE","SHIB","PEPE","PUMP"}  # meme lon co nen tang -> giu
PUMP_PCT = 40.0
TOP_N = 50


def fetch(url, params, timeout=30):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def main():
    print("Fetching CoinGecko top MC...")
    coins = []
    for page in (1, 2):
        coins += fetch("https://api.coingecko.com/api/v3/coins/markets",
                       {"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 75, "page": page, "sparkline": "false"})

    print("Fetching meme-token category...")
    meme_ids = {c["id"] for c in fetch(
        "https://api.coingecko.com/api/v3/coins/markets",
        {"vs_currency": "usd", "category": "meme-token",
         "order": "market_cap_desc", "per_page": 250, "page": 1,
         "sparkline": "false"})}

    print("Fetching OKX USDT-SWAP instruments...")
    avail = {}
    for i in fetch("https://www.okx.com/api/v5/public/instruments",
                   {"instType": "SWAP"}, timeout=25)["data"]:
        if i["instId"].endswith("-USDT-SWAP"):
            avail[i["instId"].split("-")[0]] = i["instId"]

    out, removed = [], {"meme": [], "deny": [], "pump": []}
    for c in sorted(coins, key=lambda x: x.get("market_cap_rank") or 999):
        s = c["symbol"].lower()
        if s in SKIP:
            continue
        sym = c["symbol"].upper()
        rank = c.get("market_cap_rank") or 999
        if c["id"] in meme_ids and sym not in MEME_KEEP:
            removed["meme"].append("%s(#%s)" % (sym, rank))
            continue
        if sym in DENY:
            removed["deny"].append("%s(#%s)" % (sym, rank))
            continue
        chg = c.get("price_change_percentage_24h") or 0
        if abs(chg) > PUMP_PCT:
            removed["pump"].append("%s(%s%%)" % (sym, round(chg, 1)))
            continue
        if sym in avail:
            out.append({"symbol": sym, "instId": avail[sym],
                        "mc_rank": rank, "meme": c["id"] in meme_ids})
        if len(out) >= TOP_N:
            break

    p = os.path.join(BASE, "universe.json")
    json.dump(out, open(p, "w"), indent=1)
    print("count:", len(out))
    print("removed meme:", removed["meme"])
    print("removed deny:", removed["deny"])
    print("removed pump:", removed["pump"] or "none")
    print([x["symbol"] for x in out])


if __name__ == "__main__":
    main()
