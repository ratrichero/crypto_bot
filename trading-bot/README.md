# trading-bot — OKX futures paper-trade

Bot giao dịch perpetual futures USDT-SWAP trên OKX (chế độ paper-trade; có module
`live_okx.py` để đánh thật qua API khi sẵn sàng).

## Luồng chạy

```
bot.py → warmup nến 5m/15m (REST) → websocket giá real-time (~0.5s/tick)
  → FAST PATH mỗi tick: kiểm tra SL/TP, trigger grid
  → SLOW PATH: refresh nến, detect regime (ADX), tín hiệu scalp, ATR-adaptive grid step
  → optimizer.py (cron sáng): tự chỉnh tham số trong biên an toàn
```

## Chiến thuật

**Universe**: top 30 coin vốn hóa (`build_universe.py`), perp USDT-SWAP.

**Scalp** (`strategy.scalp_signal`): breakout trên nến 5m/15m + lọc RSI(14)
(long khi RSI ≤ 65, short khi RSI ≥ 35) + chỉ đánh khi ADX trend. TP 1.0%, SL 0.4%,
mỗi lệnh 100 USDT margin ×10.

**Grid** (`manage_grid`): chỉ khi regime = ranging (ADX(14) < 22).
- anchor = giá lúc dựng lưới; 5 tầng long tại anchor×(1−k·step), 5 tầng short tại
  anchor×(1+k·step), k=1..5; giá chạm tầng → mở lệnh tại giá tầng.
- step mặc định 0.5%, adaptive: `clamp(ATR(14)/giá, 0.4%, 0.8%)`, áp dụng ở lần rebuild.
- TP = 1 step, **không SL từng lệnh**; rebuild khi |giá/anchor − 1| > 6·step.
- Tối đa 7 lệnh grid; coin thua liên tục bị nghỉ 48h.

**Rủi ro**: dừng ngày khi lỗ > 20% vốn. Kill switch: tạo file `STOP`.

## File chính

| File | Vai trò |
|---|---|
| `bot.py` | Vòng lặp chính, quản trị vị thế |
| `strategy.py` | Tín hiệu scalp, detect regime |
| `paper_engine.py` / `live_okx.py` | Khớp lệnh mô phỏng / thật (chọn qua `mode` trong config) |
| `optimizer.py` | Tự học tham số mỗi sáng |
| `report.py` | Báo cáo P&L |

## Config (`config.example.json`)

`mode`: `paper` | `dry_run` | `live`. Đổi mode cần restart bot.
Key OKX thật (nếu dùng live) để trong file `.okx_key` chmod 600 — không commit.
