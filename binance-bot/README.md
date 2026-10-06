# binance-bot — Binance USDT-M futures trader (scalp + grid)

Port của `trading-bot/` (OKX) sang Binance USDT-M futures. **Ưu tiên Binance
vì volume lớn hơn**; code OKX giữ nguyên không đụng.

## Kiến trúc

```
binance_bot.py      Vong lap chinh: WS gia real-time -> SL/TP + grid trigger
                    (~0.5s); nen/regime/scalp tren slow loop. Kill switch: file STOP.
live_binance.py     Engine dat lenh that qua ccxt (binanceusdm), che do
                    dry_run | live. SL/TP do process tu canh gia roi dong
                    market reduce-only (giong bot OKX).
binance_ws.py       WS public: 1 combined stream !miniTicker@arr cho moi symbol.
binance_client.py   REST public: klines, ticker 24h, exchangeInfo.
build_universe.py   Top USDT-M perp theo quote volume 24h -> universe.json.
strategy.py         Tin hieu scalp + regime (copy y het tu trading-bot/).
indicators.py       EMA/RSI/ADX/ATR thuan python (copy y het tu trading-bot/).
config.example.json Mau config day du, co ghi chu tung tham so.
test_binance.py     Smoke test offline (39 checks).
```

`strategy.py` và `indicators.py` là bản copy verbatim từ `trading-bot/` để
module này tự chứa (deploy 1 thư mục lên VPS là chạy, không phụ thuộc chéo).
Nếu sửa logic chiến thuật, sửa cả hai nơi.

## Luật chiến thuật (giống hệt bot OKX)

- **Regime**: ADX(14) trên nến 15m. `< adx_threshold` → ranging → chạy grid;
  `>=` → trending → scalp. **Điểm khởi đầu: `adx_threshold = 24`,
  `grid.step_mult = 0.8`** — là giá trị optimizer tự tune được từ bot OKX
  paper ngày 06/10/2026.
- **Scalp**: trend filter EMA(20) trên 15m; entry khi nến 5m đóng cửa phá
  đỉnh/đáy N nến gần nhất + lọc RSI (long RSI<65, short RSI>35).
  TP 1%, SL 0.4% (net R:R ~1.8 sau phí). Tối đa 4 vị thế scalp;
  sau SL nghỉ 30 phút.
- **Grid hai chiều**: 5 tầng mỗi bên quanh anchor; step =
  `clamp(step_mult * ATR(14)/giá, 0.4%, 0.8%)`; TP = 1 step, không SL từng
  lệnh; rebuild khi giá lệch anchor quá 6 step. Tối đa 7 lệnh grid.
- **Size**: mỗi lệnh 100 USDT margin ×10 = 1000 USDT notional; tối đa 10 vị thế.
- **Daily stop**: lỗ ≥20% equity trong ngày → đóng hết, nghỉ hết ngày.
- **Kill switch**: tạo file `STOP` trong thư mục này → bot shutdown gọn.

## Điểm khác biệt so với bot OKX (lý do)

1. **Hedge mode bắt buộc.** Grid OKX mở long và short cùng lúc trên cùng
   instrument. Binance one-way mode sẽ net hai chiều này vào nhau → bot tự
   bật hedge (dual-side) lúc khởi động, mọi lệnh kèm `positionSide`
   LONG/SHORT. Nếu tài khoản không chuyển được (đang có vị thế one-way),
   bot dừng với thông báo rõ ràng.
2. **Đơn vị khối lượng.** Binance đặt lệnh theo số lượng base asset
   (ví dụ BTC), làm tròn XUỐNG theo `stepSize`, kiểm tra `minQty` và
   `minNotional` từ exchangeInfo trước khi gửi.
3. **Đóng lệnh** dùng market + `reduceOnly=true`.
4. **Phí**: taker Binance USDT-M 0.05% (`fee_rate = 0.0005`), giống OKX.
   Funding 8h chưa mô hình hóa (giống bot OKX) — cần cộng vào khi đánh giá
   P&L dài hạn.
5. Không có paper mode nội bộ: so sánh paper-vs-real bằng bot OKX đang chạy
   trên máy Muse.

## Mô hình an toàn

- `mode`: `dry_run` (mặc định) | `live`. Đổi mode phải restart.
- **dry_run**: không cần key, **không gọi bất kỳ API xác thực nào** (kể cả
  read-only), chỉ log "lệnh sẽ đặt". Dùng để kiểm tra tín hiệu/chiến thuật.
- **live**: đọc key từ biến môi trường `BINANCE_API_KEY` /
  `BINANCE_API_SECRET`. Không hardcode, không ghi vào file, không log.
  Thiếu 1 trong 2 → từ chối khởi động (fail closed).
- Code **không chứa bất kỳ endpoint rút tiền nào** (test tự quét).
- Key trên Binance phải: **chỉ quyền Trade Futures, TẮT quyền Withdraw,
  whitelist IP của VPS**. Kiểm tra lại trên trang API Management trước khi live.
- SL/TP do process canh (không đặt stop sẵn trên sàn): nếu process chết,
  vị thế không có stop cho tới khi watchdog/systemd dựng lại. Chưa nên
  scale size lớn khi chưa có stop dự phòng trên sàn.

## Chạy thử (dry-run)

```bash
pip install -r requirements.txt
cp config.example.json config.json
python3 build_universe.py        # tao universe.json (can mang, public API)
python3 test_binance.py          # smoke test offline, khong can key
python3 binance_bot.py           # chay dry-run: chi log, khong dat lenh that
```

## Lên live (cần Cường duyệt từng bước)

1. Trên Binance: tạo API key **chỉ Trade Futures**, **tắt Withdraw**,
   whitelist IP VPS.
2. Trên VPS: `export BINANCE_API_KEY=... BINANCE_API_SECRET=...`
   (hoặc cho vào EnvironmentFile của systemd service, chmod 600).
3. `cp config.example.json config.json`, sửa `"mode": "live"`.
   (Khuyên bật `"use_testnet": true` trước với key testnet để kiểm tra kỹ
   thuật, rồi mới tắt.)
4. Chạy `python3 binance_bot.py`, theo dõi 30–60 phút đầu, đối chiếu app Binance.
5. Size test nhỏ trước (giảm `order_margin_usdt`), Cường gật đầu mới để size chuẩn.

Mọi thay đổi code đều push lên repo git (luật đứng).
