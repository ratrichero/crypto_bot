import json, requests

print("Fetching CoinGecko top MC...")
r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
    params={"vs_currency":"usd","order":"market_cap_desc","per_page":60,"page":1,"sparkline":"false"},
    timeout=25)
r.raise_for_status()
coins = r.json()

print("Fetching OKX USDT-SWAP instruments...")
r2 = requests.get("https://www.okx.com/api/v5/public/instruments",
    params={"instType":"SWAP"}, timeout=25)
r2.raise_for_status()
avail = {}
for i in r2.json()["data"]:
    if i["instId"].endswith("-USDT-SWAP"):
        avail[i["instId"].split("-")[0]] = i["instId"]

SKIP = {"usdt","usdc","dai","usde","fdusd","tusd","pyusd","usdd","frax","susde",
        "wbtc","steth","wsteth","weeth","cbeth","reth","weth"}
out = []
for c in sorted(coins, key=lambda x: x.get("market_cap_rank") or 999):
    s = c["symbol"].lower()
    if s in SKIP:
        continue
    sym = c["symbol"].upper()
    if sym in avail:
        out.append({"symbol": sym, "instId": avail[sym],
                    "mc_rank": c.get("market_cap_rank")})
    if len(out) >= 30:
        break

json.dump(out, open("/home/hatch/workspace/trading-bot/universe.json","w"), indent=1)
print("count:", len(out))
print([f"{x['symbol']}(#{x['mc_rank']})" for x in out])
