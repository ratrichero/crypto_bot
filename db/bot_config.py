"""Config runtime cua bot (luu DB, cache RAM) + tai khoan dashboard.

Dung chung cho binance-bot (doc/ap dung) va dashboard (sua/luu). Khong phu
thuoc streamlit; moi ham DB nhan 1 ket noi psycopg3 (autocommit hay khong deu
duoc) de test duoc tren Postgres that.

Phan loai config:
- Bi mat / ha tang (API key, DATABASE_URL, mode, use_testnet, hedge_mode,
  exchange_protection...) -> env/config.json, doi phai restart (co y).
- Tham so chien luoc & rui ro (PARAMS ben duoi) -> DB, ap dung nong.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BOT_BINANCE = "binance"
NOTIFY_CHANNEL = "bot_config"


# --------------------------------------------------------------- schema
@dataclass(frozen=True)
class Param:
    key: str            # duong dan cham trong config, vd "grid.tp_pct"
    kind: str           # int | float | bool | enum
    default: Any
    group: str
    label: str
    lo: Optional[float] = None
    hi: Optional[float] = None
    choices: Tuple[str, ...] = ()
    pct: bool = False   # hien thi/nhap theo % (luu dang phan so)
    help: str = ""
    apply: str = "now"  # now | new_lots | rebuild (ngu nghia ap dung)


PARAMS: Tuple[Param, ...] = (
    # ---- Lenh
    Param("order_margin_usdt", "float", 100.0, "Lệnh", "Margin mỗi lệnh (USDT)",
          5, 5000, help="Notional mỗi lot = margin × đòn bẩy.",
          apply="new_lots"),
    Param("leverage", "int", 10, "Lệnh", "Đòn bẩy", 1, 20,
          help="Đặt lại trên sàn ở lệnh mở kế tiếp của mỗi symbol.",
          apply="new_lots"),
    Param("max_total_positions", "int", 10, "Lệnh",
          "Tổng số lot tối đa (grid + scalp)", 1, 60),
    # ---- Grid
    Param("grid.max_positions", "int", 3, "Grid",
          "Số lot grid tối đa (mọi symbol)", 1, 40),
    Param("grid.max_same_side", "int", 2, "Grid",
          "Số lot grid cùng chiều tối đa (mọi symbol, 0 = tắt)", 0, 40,
          help="Altcoin chạy theo BTC: nhiều lot long trên nhiều coin = một "
               "lệnh cược lớn vào chiều tăng. Đạt trần thì không mở thêm lot "
               "cùng chiều ở bất kỳ symbol nào (lệnh chờ LIMIT tính như lot). "
               "Vốn $1k: 2."),
    Param("grid.max_symbols", "int", 0, "Grid",
          "Số symbol grid cùng lúc (0 = không giới hạn)", 0, 30,
          help="Đạt giới hạn thì symbol chưa có lot không được mở grid."),
    Param("grid.levels_each_side", "int", 2, "Grid", "Số tầng mỗi phía", 1, 10,
          apply="rebuild"),
    Param("grid.tp_pct", "float", 0.01, "Grid", "TP mỗi lot (%)",
          0.0, 0.05, pct=True,
          help="0 = TP bằng 1 step (hành vi cũ).", apply="new_lots"),
    Param("grid.sl_pct", "float", 0.03, "Grid", "SL trên sàn mỗi lot (%)",
          0.005, 0.20, pct=True,
          help="SL thảm hoạ đặt trên sàn; basket stop thường cắt trước.",
          apply="new_lots"),
    Param("grid.step_min", "float", 0.01, "Grid", "Độ giãn tối thiểu (%)",
          0.001, 0.05, pct=True, apply="rebuild"),
    Param("grid.step_max", "float", 0.015, "Grid", "Độ giãn tối đa (%)",
          0.001, 0.10, pct=True, apply="rebuild"),
    Param("grid.step_mult", "float", 0.8, "Grid", "Hệ số độ giãn × ATR(15m)",
          0.1, 5.0, apply="rebuild"),
    Param("grid.range_steps", "int", 6, "Grid",
          "Rebuild khi giá lệch anchor quá N step", 2, 30, apply="rebuild"),
    Param("grid.max_entries_per_cycle", "int", 1, "Grid",
          "Số lot mở tối đa mỗi vòng", 1, 10),
    # ---- Grid v2 (range grid 2 chieu theo scanner)
    Param("grid.engine", "enum", "classic", "Grid v2", "Kiểu grid",
          choices=("classic", "range"),
          help="classic = grid quanh anchor (cũ, chỉ khi regime đi ngang); "
               "range = grid 2 chiều trong biên scanner: long nửa dưới, "
               "short nửa trên, chỉ symbol đạt chuẩn + top K (bất kể chế "
               "độ observe/filter). Đổi kiểu khi grid đang flat là an toàn "
               "nhất; lot cũ vẫn được quản lý tới khi đóng.",
          apply="rebuild"),
    Param("grid.max_lots_per_symbol", "int", 2, "Grid v2",
          "Số lot tối đa mỗi symbol (range)", 1, 20,
          help="Cùng với 'Số lot grid tối đa' và 'Số symbol grid cùng lúc'."),
    Param("grid.range_min_levels", "int", 1, "Grid v2",
          "Số tầng tối thiểu mỗi phía để vào top K (range)", 1, 10,
          help="Symbol đạt chuẩn scanner nhưng biên quá hẹp so với độ giãn "
               "(dựng được ít hơn N tầng mỗi phía) bị bỏ khi chọn top K, "
               "nhường chỗ cho symbol xếp sau. 1 = chỉ bỏ symbol không dựng "
               "được tầng nào; 2 = đòi đủ 2 tầng (biên > 4 × độ giãn). "
               "Không vượt 'Số tầng mỗi phía'."),
    Param("grid.boundary_sl_buffer", "float", 0.005, "Grid v2",
          "SL ngoài biên (%)", 0.0, 0.05, pct=True,
          help="SL long = đáy biên × (1 − x), short = đỉnh × (1 + x); không "
               "bao giờ xa hơn 'SL trên sàn mỗi lot'.", apply="new_lots"),
    Param("grid.break_buffer", "float", 0.003, "Grid v2",
          "Biên vỡ khi giá vượt quá (%)", 0.0, 0.05, pct=True,
          help="Biên vỡ -> không mở mới, huỷ lệnh chờ của symbol."),
    Param("grid.trend_exit_adx", "float", 25.0, "Grid v2",
          "ADX 1h coi là chuyển trend", 10, 60,
          help="ADX 1h (scanner) vượt ngưỡng -> coi như biên vỡ."),
    Param("grid.derisk_on_trend", "bool", False, "Grid v2",
          "Cắt lot đang lỗ khi biên vỡ"),
    Param("grid.derisk_loss_pct", "float", 0.01, "Grid v2",
          "Ngưỡng lỗ để cắt khi biên vỡ (%)", 0.001, 0.10, pct=True),
    Param("grid.entry_mode", "enum", "market", "Grid v2", "Kiểu vào lệnh",
          choices=("market", "limit"),
          help="limit = đặt trước LIMIT post-only (GTX, phí maker) tại các "
               "tầng gần giá nhất, lệnh chờ chiếm slot; chỉ dùng với kiểu "
               "grid range. market = vào khi giá cắt tầng (phí taker)."),
    Param("grid.entry_ttl_minutes", "int", 60, "Grid v2",
          "Huỷ lệnh chờ sau (phút)", 5, 1440,
          help="Bot đặt lại nếu tầng vẫn hợp lệ."),
    Param("grid.partial_fill_timeout_seconds", "int", 60, "Grid v2",
          "Khớp một phần: huỷ phần còn lại sau (giây)", 10, 3600,
          help="Phần đã khớp thành 1 lot riêng có SL/TP."),
    Param("grid.max_new_orders_per_cycle", "int", 2, "Grid v2",
          "Số lệnh chờ đặt mới tối đa mỗi vòng", 1, 10),
    Param("grid.limit_min_gap_pct", "float", 0.0005, "Grid v2",
          "Khoảng cách tối thiểu tầng ↔ giá để đặt LIMIT (%)", 0.0, 0.01,
          pct=True, help="Tránh lệnh post-only bị sàn từ chối vì sẽ khớp "
                         "ngay."),
    # ---- Loc chieu xu huong cho grid (task 34, binance-bot/trend_filter.py)
    Param("trend.market_filter", "bool", True, "Xu hướng",
          "Lọc theo xu hướng BTC (mọi coin)",
          help="BTC giảm (dưới EMA 1h + EMA dốc xuống, hoặc giảm mạnh trong "
               "vài giờ) → không mở lot grid LONG mới trên mọi coin; BTC "
               "tăng → không mở SHORT mới. Chỉ chặn mở mới, lot đang mở vẫn "
               "chạy tới TP/SL. Thiếu dữ liệu → chặn (an toàn)."),
    Param("trend.symbol_filter", "bool", True, "Xu hướng",
          "Lọc theo xu hướng từng coin",
          help="Coin dưới EMA 1h và EMA dốc xuống → không mở long grid mới "
               "trên coin đó (ngược lại với short). Đi ngang (EMA phẳng) "
               "→ grid chạy bình thường."),
    Param("trend.ema_period", "int", 50, "Xu hướng", "Chu kỳ EMA (nến 1h)",
          10, 90),
    Param("trend.slope_bars", "int", 6, "Xu hướng",
          "Đo độ dốc EMA qua N nến 1h", 1, 48),
    Param("trend.slope_min_atr", "float", 0.5, "Xu hướng",
          "Độ dốc EMA tối thiểu (× ATR 1h)", 0.0, 5.0,
          help="EMA dịch ≥ x lần ATR(14) 1h trong N nến → có xu hướng. Chuẩn "
               "hoá theo ATR nên coin biến động mạnh/yếu dùng chung ngưỡng. "
               "Nhỏ = nhạy hơn (chặn nhiều hơn); 0.5 ≈ đi ngang bị chặn 1 "
               "phía ~10-18% thời gian."),
    Param("trend.market_move_hours", "int", 4, "Xu hướng",
          "BTC: cửa sổ đo biến động nhanh (giờ)", 1, 48),
    Param("trend.market_move_pct", "float", 0.015, "Xu hướng",
          "BTC: giảm/tăng ≥ x% trong cửa sổ → xu hướng ngay (%)", 0.0, 0.20,
          pct=True, help="Bắt cú dump/pump nhanh mà EMA chưa kịp dốc. "
                         "0 = tắt."),
    Param("trend.refresh_minutes", "int", 5, "Xu hướng",
          "Lấy lại nến 1h mỗi (phút)", 1, 60),
    # ---- Rui ro
    Param("risk.daily_max_loss_pct", "float", 0.10, "Rủi ro",
          "Daily stop (% equity)", 0.01, 0.50, pct=True),
    Param("risk.grid_basket_max_loss_pct", "float", 0.03, "Rủi ro",
          "Basket stop mỗi symbol (% equity)", 0.002, 0.30, pct=True),
    Param("risk.grid_total_max_loss_pct", "float", 0.10, "Rủi ro",
          "Trần lỗ tổng grid (% equity, 0 = tắt)", 0.0, 0.50, pct=True),
    Param("risk.max_notional_mult", "float", 10.0, "Rủi ro",
          "Tổng notional tối đa (× equity)", 1.0, 50.0),
    # ---- Regime
    Param("adx_threshold", "float", 25.0, "Regime", "ADX 15m vào trend", 5, 60),
    Param("adx_range_threshold", "float", 20.0, "Regime",
          "ADX 15m về đi ngang", 5, 60),
    Param("regime_confirm_bars", "int", 2, "Regime",
          "Số nến xác nhận đổi regime", 1, 10),
    # ---- Scalp
    Param("scalp.max_positions", "int", 4, "Scalp", "Số lot scalp tối đa",
          0, 20),
    Param("scalp.tp_pct", "float", 0.01, "Scalp", "TP scalp (%)", 0.001, 0.05,
          pct=True, apply="new_lots"),
    Param("scalp.sl_pct", "float", 0.004, "Scalp", "SL scalp (%)", 0.001,
          0.05, pct=True, apply="new_lots"),
    # ---- Scanner di ngang
    Param("scanner.enabled", "bool", True, "Scanner", "Bật scanner"),
    Param("scanner.mode", "enum", "observe", "Scanner", "Chế độ",
          choices=("observe", "filter"),
          help="observe = chỉ quan sát; filter = grid chỉ mở mới trên "
               "symbol đạt chuẩn (top K)."),
    Param("scanner.top_k", "int", 5, "Scanner",
          "Top K symbol được phép (chế độ filter)", 1, 30),
    Param("scanner.rescan_minutes", "int", 15, "Scanner",
          "Chu kỳ quét mỗi symbol (phút)", 5, 240),
    Param("scanner.adx_1h_max", "float", 20.0, "Scanner", "ADX 1h tối đa",
          5, 50),
    Param("scanner.adx_15m_max", "float", 22.0, "Scanner", "ADX 15m tối đa",
          5, 50),
    Param("scanner.bbw_min_pct", "float", 0.025, "Scanner",
          "Độ rộng Bollinger 1h tối thiểu (%)", 0.002, 0.30, pct=True,
          help="Nên ≥ 2 × TP + phí để biên đủ rộng ăn TP."),
    Param("scanner.bbw_max_pct", "float", 0.10, "Scanner",
          "Độ rộng Bollinger 1h tối đa (%)", 0.005, 0.50, pct=True),
    Param("scanner.bbw_pctile_max", "float", 80.0, "Scanner",
          "Percentile độ rộng BB tối đa (so với ~20 ngày)", 1, 100,
          help="Loại lúc biên đang nở mạnh (dấu hiệu breakout)."),
    Param("scanner.range_hours", "int", 48, "Scanner",
          "Cửa sổ biên giá (giờ)", 6, 168),
    Param("scanner.range_min_pct", "float", 0.025, "Scanner",
          "Biên giá tối thiểu (%)", 0.002, 0.30, pct=True),
    Param("scanner.range_max_pct", "float", 0.12, "Scanner",
          "Biên giá tối đa (%)", 0.005, 0.60, pct=True),
    Param("scanner.min_mid_crosses", "int", 4, "Scanner",
          "Số lần cắt đường giữa biên tối thiểu", 0, 50),
    Param("scanner.chop_min", "float", 45.0, "Scanner",
          "Choppiness Index tối thiểu (trên cửa sổ biên)", 0, 100,
          help="Cao = giá dao động qua lại; random walk ~40, trend ~25."),
    Param("scanner.er_max", "float", 0.35, "Scanner",
          "Efficiency Ratio 1h tối đa", 0.01, 1.0),
)
PARAM_BY_KEY: Dict[str, Param] = {p.key: p for p in PARAMS}
GROUPS: Tuple[str, ...] = tuple(dict.fromkeys(p.group for p in PARAMS))


def get_path(cfg: dict, key: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(cfg: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def defaults() -> Dict[str, Any]:
    return {p.key: p.default for p in PARAMS}


def extract(cfg: dict) -> Dict[str, Any]:
    """Gia tri hien hanh cua moi tham so tunable (thieu -> default schema)."""
    return {p.key: get_path(cfg, p.key, p.default) for p in PARAMS}


def _coerce(p: Param, value: Any) -> Any:
    if p.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError("phải là true/false")
    if p.kind == "enum":
        if value not in p.choices:
            raise ValueError("chỉ nhận %s" % "/".join(p.choices))
        return value
    if isinstance(value, bool):
        raise ValueError("phải là số")
    if p.kind == "int":
        f = float(value)
        if f != int(f):
            raise ValueError("phải là số nguyên")
        out: Any = int(f)
    else:
        out = float(value)
    if out != out or out in (float("inf"), float("-inf")):
        raise ValueError("không hợp lệ")
    if p.lo is not None and out < p.lo:
        raise ValueError("nhỏ hơn mức tối thiểu %s" % _fmt(p, p.lo))
    if p.hi is not None and out > p.hi:
        raise ValueError("lớn hơn mức tối đa %s" % _fmt(p, p.hi))
    return out


def _fmt(p: Param, v: float) -> str:
    return ("%g%%" % round(v * 100, 6)) if p.pct else ("%g" % v)


def validate(flat: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Kiem tra kieu/bien/rang buoc cheo. Tra ve (clean, errors).

    Thieu tham so -> lay default schema; khoa la -> loi (tranh go nham)."""
    errors: List[str] = []
    clean: Dict[str, Any] = {}
    for key in flat:
        if key not in PARAM_BY_KEY:
            errors.append("%s: tham số không tồn tại" % key)
    for p in PARAMS:
        raw = flat.get(p.key, p.default)
        try:
            clean[p.key] = _coerce(p, raw)
        except (TypeError, ValueError) as e:
            errors.append("%s (%s): %s" % (p.label, p.key, e))
    if errors:
        return clean, errors
    if clean["grid.step_min"] > clean["grid.step_max"]:
        errors.append("Độ giãn tối thiểu phải ≤ độ giãn tối đa")
    if clean["adx_range_threshold"] > clean["adx_threshold"]:
        errors.append("ADX về đi ngang phải ≤ ADX vào trend")
    total = clean["risk.grid_total_max_loss_pct"]
    if total > 0 and total > clean["risk.daily_max_loss_pct"]:
        errors.append("Trần lỗ tổng grid phải ≤ daily stop")
    if total > 0 and clean["risk.grid_basket_max_loss_pct"] > total:
        errors.append("Basket stop mỗi symbol phải ≤ trần lỗ tổng grid")
    if clean["scanner.bbw_min_pct"] > clean["scanner.bbw_max_pct"]:
        errors.append("Độ rộng BB tối thiểu phải ≤ tối đa")
    if clean["scanner.range_min_pct"] > clean["scanner.range_max_pct"]:
        errors.append("Biên giá tối thiểu phải ≤ tối đa")
    if clean["grid.max_positions"] > clean["max_total_positions"]:
        errors.append("Số lot grid tối đa phải ≤ tổng số lot tối đa")
    if clean["grid.entry_mode"] == "limit" and clean["grid.engine"] != "range":
        errors.append("Vào lệnh LIMIT chỉ hỗ trợ kiểu grid range")
    if clean["grid.range_min_levels"] > clean["grid.levels_each_side"]:
        errors.append("Số tầng tối thiểu để vào top K phải ≤ số tầng mỗi "
                      "phía")
    if clean["trend.ema_period"] + clean["trend.slope_bars"] > 98:
        errors.append("Chu kỳ EMA + số nến đo độ dốc phải ≤ 98 (bot lấy 99 "
                      "nến 1h)")
    if clean["grid.engine"] == "range":
        if not clean["scanner.enabled"]:
            errors.append("Range grid cần bật scanner (biên lấy từ scanner)")
        if clean["grid.trend_exit_adx"] < clean["scanner.adx_1h_max"]:
            errors.append("ADX 1h chuyển trend phải ≥ ADX 1h tối đa của "
                          "scanner (nếu không biên vỡ ngay khi dựng)")
    return clean, errors


def apply(cfg: dict, flat: Dict[str, Any]) -> List[str]:
    """Ghi gia tri da validate vao cfg (tai cho). Tra ve danh sach khoa doi."""
    changed = []
    for key, value in flat.items():
        if get_path(cfg, key, object()) != value:
            set_path(cfg, key, value)
            changed.append(key)
    return changed


def diff(old: Dict[str, Any], new: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
    return [(p.key, old.get(p.key), new.get(p.key)) for p in PARAMS
            if old.get(p.key) != new.get(p.key)]


# ------------------------------------------------------------------ DDL
DDL = """
CREATE TABLE IF NOT EXISTS bot_config_versions (
    version     BIGSERIAL PRIMARY KEY,
    bot         TEXT NOT NULL,
    config      JSONB NOT NULL,
    author      TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bot_config_versions_bot_idx
    ON bot_config_versions (bot, version DESC);
CREATE TABLE IF NOT EXISTS bot_config_applied (
    bot         TEXT PRIMARY KEY,
    version     BIGINT,
    status      TEXT NOT NULL,
    error       TEXT,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dashboard_users (
    id              BIGSERIAL PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'viewer'
                    CHECK (role IN ('admin', 'viewer')),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TIMESTAMPTZ,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS scanner_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    bot         TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    passed      BOOLEAN NOT NULL,
    score       DOUBLE PRECISION,
    metrics     JSONB,
    reasons     JSONB
);
CREATE INDEX IF NOT EXISTS scanner_snapshots_sym_idx
    ON scanner_snapshots (bot, symbol, ts DESC);
CREATE TABLE IF NOT EXISTS dashboard_sessions (
    token_hash   TEXT PRIMARY KEY,
    username     TEXT NOT NULL REFERENCES dashboard_users (username)
                 ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS dashboard_sessions_user_idx
    ON dashboard_sessions (username);
"""


def ensure_tables(conn) -> None:
    with conn.transaction():
        conn.execute(DDL)


def _rows(conn, sql: str, params: tuple = ()) -> List[dict]:
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


# ------------------------------------------------------- config versions
def latest_version(conn, bot: str = BOT_BINANCE) -> Optional[int]:
    rows = _rows(conn, "SELECT max(version) AS v FROM bot_config_versions "
                       "WHERE bot = %s", (bot,))
    return rows[0]["v"] if rows and rows[0]["v"] is not None else None


def load_version(conn, bot: str = BOT_BINANCE,
                 version: Optional[int] = None) -> Optional[dict]:
    if version is None:
        rows = _rows(conn, "SELECT * FROM bot_config_versions WHERE bot = %s "
                           "ORDER BY version DESC LIMIT 1", (bot,))
    else:
        rows = _rows(conn, "SELECT * FROM bot_config_versions WHERE bot = %s "
                           "AND version = %s", (bot, version))
    if not rows:
        return None
    row = dict(rows[0])
    if isinstance(row["config"], str):
        row["config"] = json.loads(row["config"])
    return row


def save_version(conn, flat: Dict[str, Any], author: str, note: str = "",
                 bot: str = BOT_BINANCE) -> int:
    """Validate roi ghi version moi + NOTIFY. Loi -> ValueError (khong ghi)."""
    clean, errors = validate(flat)
    if errors:
        raise ValueError("; ".join(errors))
    if not author:
        raise ValueError("thiếu author")
    with conn.transaction():
        rows = _rows(conn, "INSERT INTO bot_config_versions (bot, config, "
                           "author, note) VALUES (%s, %s::jsonb, %s, %s) "
                           "RETURNING version",
                     (bot, json.dumps(clean, sort_keys=True), author,
                      note or None))
        version = rows[0]["version"]
        conn.execute("SELECT pg_notify(%s, %s)",
                     (NOTIFY_CHANNEL, "%s:%s" % (bot, version)))
    return int(version)


def history(conn, bot: str = BOT_BINANCE, limit: int = 20) -> List[dict]:
    return _rows(conn, "SELECT version, author, note, created_at, config "
                       "FROM bot_config_versions WHERE bot = %s "
                       "ORDER BY version DESC LIMIT %s", (bot, limit))


def record_applied(conn, version: Optional[int], status: str,
                   error: Optional[str] = None,
                   bot: str = BOT_BINANCE) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO bot_config_applied (bot, version, status, error, "
            "applied_at) VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (bot) DO UPDATE SET version = EXCLUDED.version, "
            "status = EXCLUDED.status, error = EXCLUDED.error, "
            "applied_at = EXCLUDED.applied_at",
            (bot, version, status, (error or None) and str(error)[:500]))


def applied(conn, bot: str = BOT_BINANCE) -> Optional[dict]:
    rows = _rows(conn, "SELECT * FROM bot_config_applied WHERE bot = %s",
                 (bot,))
    return rows[0] if rows else None


# ------------------------------------------------------------ users
ROLES = ("admin", "viewer")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 8
MAX_FAILED = 5
LOCK_MINUTES = 15
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32,
                            **_SCRYPT)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"],
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             dklen=len(expected), n=int(n), r=int(r),
                             p=int(p))
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


_DUMMY_HASH = hash_password(secrets.token_hex(8))


def check_new_credentials(username: str, password: str,
                          confirm: Optional[str] = None) -> List[str]:
    errs = []
    if not USERNAME_RE.match(username or ""):
        errs.append("Tên đăng nhập 3–32 ký tự: chữ, số, _ . -")
    if len(password or "") < MIN_PASSWORD:
        errs.append("Mật khẩu tối thiểu %d ký tự" % MIN_PASSWORD)
    if confirm is not None and password != confirm:
        errs.append("Mật khẩu nhập lại không khớp")
    return errs


def user_count(conn) -> int:
    return int(_rows(conn, "SELECT count(*) AS n FROM dashboard_users")[0]["n"])


def create_first_admin(conn, username: str, password: str) -> bool:
    """Tao admin DAU TIEN. Chi thanh cong khi bang con rong (khoa bang de 2
    nguoi mo dashboard cung luc khong tao duoc 2 admin). False neu da co user."""
    errs = check_new_credentials(username, password)
    if errs:
        raise ValueError("; ".join(errs))
    with conn.transaction():
        conn.execute("LOCK TABLE dashboard_users IN EXCLUSIVE MODE")
        if _rows(conn, "SELECT 1 FROM dashboard_users LIMIT 1"):
            return False
        conn.execute("INSERT INTO dashboard_users (username, password_hash, "
                     "role, created_by) VALUES (%s, %s, 'admin', 'setup')",
                     (username, hash_password(password)))
    return True


def get_user(conn, username: str) -> Optional[dict]:
    rows = _rows(conn, "SELECT * FROM dashboard_users WHERE username = %s",
                 (username,))
    return rows[0] if rows else None


def _require_admin(conn, actor: str) -> None:
    user = get_user(conn, actor or "")
    if not user or not user["is_active"] or user["role"] != "admin":
        raise PermissionError("chỉ admin được thực hiện")


def create_user(conn, actor: str, username: str, password: str,
                role: str = "viewer") -> None:
    _require_admin(conn, actor)
    if role not in ROLES:
        raise ValueError("vai trò không hợp lệ")
    errs = check_new_credentials(username, password)
    if errs:
        raise ValueError("; ".join(errs))
    if get_user(conn, username):
        raise ValueError("tên đăng nhập đã tồn tại")
    with conn.transaction():
        conn.execute("INSERT INTO dashboard_users (username, password_hash, "
                     "role, created_by) VALUES (%s, %s, %s, %s)",
                     (username, hash_password(password), role, actor))


def authenticate(conn, username: str, password: str,
                 now: Optional[datetime] = None) -> Tuple[Optional[dict], str]:
    """Tra ve (user, "") hoac (None, ly do). Sai MAX_FAILED lan -> khoa
    LOCK_MINUTES phut. User khong ton tai van ton cong scrypt (khong lo ten)."""
    now = now or datetime.now(timezone.utc)
    user = get_user(conn, username or "")
    if not user:
        verify_password(password or "", _DUMMY_HASH)
        return None, "Sai tên đăng nhập hoặc mật khẩu"
    if not user["is_active"]:
        verify_password(password or "", _DUMMY_HASH)
        return None, "Tài khoản đã bị khoá"
    locked = user.get("locked_until")
    if locked is not None and locked > now:
        return None, "Tạm khoá do đăng nhập sai nhiều lần, thử lại sau %s" % (
            locked.astimezone(timezone.utc).strftime("%H:%M UTC"))
    if not verify_password(password or "", user["password_hash"]):
        failed = int(user["failed_attempts"] or 0) + 1
        lock = now + timedelta(minutes=LOCK_MINUTES) if failed >= MAX_FAILED \
            else None
        with conn.transaction():
            conn.execute("UPDATE dashboard_users SET failed_attempts = %s, "
                         "locked_until = %s WHERE id = %s",
                         (0 if lock else failed, lock, user["id"]))
        return None, "Sai tên đăng nhập hoặc mật khẩu"
    with conn.transaction():
        conn.execute("UPDATE dashboard_users SET failed_attempts = 0, "
                     "locked_until = NULL, last_login_at = %s WHERE id = %s",
                     (now, user["id"]))
    user = dict(user)
    user.pop("password_hash", None)
    return user, ""


def list_users(conn) -> List[dict]:
    return _rows(conn, "SELECT id, username, role, is_active, created_by, "
                       "created_at, last_login_at, locked_until "
                       "FROM dashboard_users ORDER BY id")


def _active_admins(conn) -> int:
    return int(_rows(conn, "SELECT count(*) AS n FROM dashboard_users "
                           "WHERE role = 'admin' AND is_active")[0]["n"])


def set_active(conn, actor: str, username: str, active: bool) -> None:
    _require_admin(conn, actor)
    user = get_user(conn, username)
    if not user:
        raise ValueError("không tìm thấy tài khoản")
    if (not active and user["role"] == "admin" and user["is_active"]
            and _active_admins(conn) <= 1):
        raise ValueError("không thể khoá admin cuối cùng")
    with conn.transaction():
        conn.execute("UPDATE dashboard_users SET is_active = %s, "
                     "failed_attempts = 0, locked_until = NULL "
                     "WHERE id = %s", (active, user["id"]))
        if not active:
            conn.execute("DELETE FROM dashboard_sessions WHERE username = %s",
                         (user["username"],))


def set_role(conn, actor: str, username: str, role: str) -> None:
    _require_admin(conn, actor)
    if role not in ROLES:
        raise ValueError("vai trò không hợp lệ")
    user = get_user(conn, username)
    if not user:
        raise ValueError("không tìm thấy tài khoản")
    if (user["role"] == "admin" and role != "admin" and user["is_active"]
            and _active_admins(conn) <= 1):
        raise ValueError("không thể hạ quyền admin cuối cùng")
    with conn.transaction():
        conn.execute("UPDATE dashboard_users SET role = %s WHERE id = %s",
                     (role, user["id"]))


def reset_password(conn, actor: str, username: str, password: str) -> None:
    """Admin dat lai mat khau bat ky; user thuong chi doi mat khau cua minh."""
    if actor != username:
        _require_admin(conn, actor)
    if len(password or "") < MIN_PASSWORD:
        raise ValueError("Mật khẩu tối thiểu %d ký tự" % MIN_PASSWORD)
    user = get_user(conn, username)
    if not user:
        raise ValueError("không tìm thấy tài khoản")
    with conn.transaction():
        conn.execute("UPDATE dashboard_users SET password_hash = %s, "
                     "failed_attempts = 0, locked_until = NULL "
                     "WHERE id = %s", (hash_password(password), user["id"]))
        # Doi mat khau -> thu hoi moi phien (dashboard tao lai phien hien tai
        # khi user tu doi).
        conn.execute("DELETE FROM dashboard_sessions WHERE username = %s",
                     (user["username"],))


# --------------------------------------------------------- sessions
# Phien dang nhap dashboard (giu qua F5). Cookie trinh duyet chi chua token
# ngau nhien; DB chi luu SHA-256 cua token -> lo DB khong lo phien. Phien het
# han co dinh SESSION_DAYS sau dang nhap; doi mat khau / khoa tai khoan /
# dang xuat moi thiet bi -> thu hoi.
SESSION_DAYS = 7


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, username: str, days: float = SESSION_DAYS) -> str:
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        conn.execute("DELETE FROM dashboard_sessions WHERE expires_at < now()")
        conn.execute("INSERT INTO dashboard_sessions (token_hash, username, "
                     "expires_at) VALUES (%s, %s, now() + %s)",
                     (_token_hash(token), username, timedelta(days=days)))
    return token


def session_user(conn, token: Optional[str]) -> Optional[dict]:
    """User {username, role} cua phien con han va tai khoan con hoat dong."""
    if not token or len(token) > 200:
        return None
    rows = _rows(conn, "UPDATE dashboard_sessions s SET last_seen_at = now() "
                       "FROM dashboard_users u WHERE s.token_hash = %s "
                       "AND s.expires_at > now() AND u.username = s.username "
                       "AND u.is_active RETURNING u.username, u.role",
                 (_token_hash(token),))
    return dict(rows[0]) if rows else None


def delete_session(conn, token: Optional[str]) -> None:
    if token:
        with conn.transaction():
            conn.execute("DELETE FROM dashboard_sessions WHERE token_hash = %s",
                         (_token_hash(token),))


def delete_user_sessions(conn, username: str) -> int:
    with conn.transaction():
        cur = conn.execute("DELETE FROM dashboard_sessions WHERE username = %s",
                           (username,))
        return cur.rowcount or 0


# --------------------------------------------------------- scanner
def insert_scan(conn, result: dict, bot: str = BOT_BINANCE) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO scanner_snapshots (bot, symbol, ts, passed, score, "
            "metrics, reasons) VALUES (%s, %s, to_timestamp(%s), %s, %s, "
            "%s::jsonb, %s::jsonb)",
            (bot, result["symbol"], float(result["ts"]),
             bool(result["passed"]), result.get("score"),
             json.dumps(result.get("metrics") or {}),
             json.dumps(result.get("reasons") or [])))


def latest_scans(conn, bot: str = BOT_BINANCE,
                 max_age_hours: float = 6) -> List[dict]:
    rows = _rows(conn,
                 "SELECT DISTINCT ON (symbol) symbol, ts, passed, score, "
                 "metrics, reasons FROM scanner_snapshots WHERE bot = %s "
                 "AND ts > now() - make_interval(secs => %s) "
                 "ORDER BY symbol, ts DESC", (bot, max_age_hours * 3600))
    rows.sort(key=lambda r: (not r["passed"], -(r["score"] or 0)))
    return rows


def prune_scans(conn, keep_days: int = 14, bot: str = BOT_BINANCE) -> int:
    with conn.transaction():
        cur = conn.execute("DELETE FROM scanner_snapshots WHERE bot = %s AND "
                           "ts < now() - make_interval(days => %s)",
                           (bot, keep_days))
        return cur.rowcount or 0


def clone_config(flat: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(flat)
