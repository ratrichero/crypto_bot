"""Cong loc rug cho paper radar: vao som chi khi token qua duoc kiem tra rug.

Mo hinh (Cuong chot 22/10/2026): radar copy phai vao that som, nhung chi mo
vi the khi:
  - token chua bi danh dau rugged
  - mint authority + freeze authority da thu hoi (khong in them / dong bang)
  - khong co risk muc "danger" theo RugCheck (gom ca tap trung holder --
    RugCheck tu loai tru account pool/curve khoi top holders)
  - LP cua market chinh da lock/burn >= nguong (pump.fun curve = 100%)
  - diem rui ro score_normalised <= nguong
  - phi chuyen token (Token-2022) khong qua cao

Nguon: RugCheck API full report (api.rugcheck.xyz), cache theo mint.
Fail closed: loi API / khong co du lieu / khong thay market -> khong mo,
ghi decision skipped_rug_* vao paper_entries.jsonl de danh gia lai bo loc.
"""
import time

import requests

DEFAULTS = {
    "rug_gate_enabled": True,
    "rug_max_score_normalised": 50,
    "rug_min_lp_locked_pct": 80.0,
    "rug_max_transfer_fee_pct": 10.0,
    "rug_cache_ttl_sec": 600,
    "rug_timeout_sec": 6,
}

_cache = {}  # mint -> (ts, report|None, ttl)


def params(cfg=None):
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if cfg and cfg.get(k) is not None:
            out[k] = cfg[k]
    return out


def _fetch(mint, timeout):
    try:
        r = requests.get(
            f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",
            timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def main_market_lp_locked_pct(rep):
    """lpLockedPct cua market co thanh khoan USD lon nhat (bo qua pool rac)."""
    best, best_liq = None, -1.0
    for m in rep.get("markets") or []:
        lp = m.get("lp") or {}
        try:
            liq = float(lp.get("baseUSD") or 0) + float(lp.get("quoteUSD") or 0)
        except (TypeError, ValueError):
            continue
        if liq > best_liq:
            best_liq = liq
            v = lp.get("lpLockedPct")
            try:
                best = float(v) if v is not None else None
            except (TypeError, ValueError):
                best = None
    return best


def evaluate(mint, cfg=None):
    """Tra (ok, reason, details). reason la decision skipped_rug_* khi bi loc."""
    p = params(cfg)
    if not p["rug_gate_enabled"]:
        return True, None, {}
    now = time.time()
    ent = _cache.get(mint)
    if ent and now - ent[0] < ent[2]:
        rep = ent[1]
    else:
        rep = _fetch(mint, float(p["rug_timeout_sec"]))
        # Report chua co market (token qua moi) chi cache ngan, tranh khoa
        # token 10 phut trong khi market vua xuat hien.
        ttl = float(p["rug_cache_ttl_sec"]) if (rep and rep.get("markets")) else 60.0
        _cache[mint] = (now, rep, ttl)
    if not rep:
        return False, "skipped_rug_no_data", {}
    det = {"rug_score": rep.get("score_normalised"),
           "rug_lp_locked_pct": main_market_lp_locked_pct(rep)}
    if rep.get("rugged"):
        return False, "skipped_rug_rugged", det
    tok = rep.get("token") or {}
    if rep.get("mintAuthority") or tok.get("mintAuthority"):
        return False, "skipped_rug_mint_authority", det
    if rep.get("freezeAuthority") or tok.get("freezeAuthority"):
        return False, "skipped_rug_freeze_authority", det
    for r in rep.get("risks") or []:
        if str(r.get("level") or "").lower() == "danger":
            det["rug_danger"] = r.get("name")
            return False, "skipped_rug_danger_risk", det
    if det["rug_lp_locked_pct"] is None:
        return False, "skipped_rug_lp_unknown", det
    if det["rug_lp_locked_pct"] < float(p["rug_min_lp_locked_pct"]):
        return False, "skipped_rug_lp_unlocked", det
    score = rep.get("score_normalised")
    if score is not None and float(score) > float(p["rug_max_score_normalised"]):
        return False, "skipped_rug_score_high", det
    tf = (rep.get("transferFee") or {}).get("pct")
    if tf is not None and float(tf) > float(p["rug_max_transfer_fee_pct"]):
        return False, "skipped_rug_transfer_fee", det
    return True, None, det
