"""OKX public market data client (no auth needed)."""
import time
import requests

BASE = "https://www.okx.com"
TIMEOUT = 12


def _get(path, params, tries=3):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(BASE + path, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            j = r.json()
            if j.get("code") == "0":
                return j["data"]
            last = Exception("okx code " + str(j.get("code")))
        except Exception as e:
            last = e
        time.sleep(1.5)
    raise last


def get_ticker(inst_id):
    d = _get("/api/v5/market/ticker", {"instId": inst_id})[0]
    return float(d["last"])


def get_candles(inst_id, bar="5m", limit=100):
    rows = _get("/api/v5/market/candles",
                {"instId": inst_id, "bar": bar, "limit": str(limit)})
    out = []
    for x in reversed(rows):  # oldest first
        out.append({
            "ts": int(x[0]),
            "o": float(x[1]),
            "h": float(x[2]),
            "l": float(x[3]),
            "c": float(x[4]),
        })
    return out
