# binance-bot — Binance USDT-M futures trader (scalp + grid)

Port của `trading-bot/` (OKX) sang Binance USDT-M futures. **Ưu tiên Binance
vì volume lớn hơn**; code OKX giữ nguyên không đụng.

## Kiến trúc

```
binance_bot.py      Vong lap chinh: routed WS gia real-time -> SL/TP + grid
                    trigger (~0.5s); nen/regime/scalp tren slow loop. Kill
                    switch: file STOP; single-instance lock theo IP.
live_binance.py     Engine dat lenh that qua ccxt (binanceusdm), che do
                    data_only | dry_run | live. Private calls co governor,
                    circuit breaker va cooldown theo symbol/action; client id,
                    private event fill, aggregate LONG/SHORT reconciliation.
binance_ws.py       WS public routed /market/stream, miniTicker + markPrice,
                    1 connection, reconnect exponential backoff, khong storm.
binance_user_ws.py  WS private routed /private/ws/<listenKey>, keepalive 30m,
                    reconnect 24h/event; nhan ORDER_TRADE_UPDATE/ACCOUNT_UPDATE.
binance_client.py   REST public shared requests.Session + request metrics,
                    rate limiter, status/API-code logging; fallback dung
                    ticker price weight thap.
binance_safety.py   Governor theo public-IP scope/endpoint va circuit state
                    persistent; 429/418/-1003 khong retry.
build_universe.py   Top USDT-M perp theo quote volume 24h -> universe.json.
strategy.py         Tin hieu scalp + regime (copy y het tu trading-bot/).
indicators.py       EMA/RSI/ADX/ATR thuan python (copy y het tu trading-bot/).
config.example.json Mau config day du, co ghi chu tung tham so.
backtest.py         Historical simulator + public data/funding downloader +
                    rolling train/test walk-forward.
test_backtest.py    Regression tests for closed candles, fills, costs and OOS.
test_binance.py     Smoke test offline.
```

`strategy.py` và `indicators.py` là bản copy verbatim từ `trading-bot/` để
module này tự chứa (deploy 1 thư mục lên VPS là chạy, không phụ thuộc chéo).
Nếu sửa logic chiến thuật, sửa cả hai nơi.

## Luật chiến thuật (nền tảng từ bot OKX, có lớp Binance risk guard)

- **Regime**: ADX(14) trên nến 15m đã đóng. `>=25` trong 2 nến xác nhận
  → trending → scalp; `<=20` trong 2 nến → ranging → grid; vùng 20–25 giữ
  regime cũ để tránh flip quanh ngưỡng.
- **Scalp**: trend filter EMA(20) trên 15m; entry khi nến 5m đóng cửa phá
  đỉnh/đáy N nến gần nhất + lọc RSI (long RSI<65, short RSI>35).
  TP 1%, SL 0.4% (net R:R ~1.8 sau phí). Tối đa 4 vị thế scalp;
  sau SL nghỉ 30 phút.
- **Grid hai chiều**: 5 tầng mỗi bên quanh anchor; step =
  `clamp(step_mult * ATR(14)/giá, 0.4%, 0.8%)`; TP = 1 step. Grid có
  basket stop mặc định 2% mark-to-market equity; khi giá lệch anchor quá
  6 step thì freeze level mới cho tới khi basket cũ flat, không xóa mapping
  position đang sống; mỗi cycle tối đa 1 level mới. Tối đa 7 lệnh grid.
- **Size hiện tại**: mỗi lệnh 100 USDT margin ×10 = 1000 USDT notional;
  tối đa 10 vị thế. Position sizing theo risk/ATR vẫn là bước P1 tiếp theo.
- **Daily stop**: lỗ mark-to-market ≥10% equity đầu ngày → đóng hết và nghỉ
  hết ngày. Mức 10% là cấu hình thử nghiệm ban đầu, chưa phải mức tối ưu cuối.
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
3. **Đóng lệnh Hedge Mode** dùng market chiều ngược lại + `positionSide`
   tương ứng; không gửi `reduceOnly` (Binance từ chối cặp tham số này trong
   Hedge Mode). Mỗi lệnh có `newClientOrderId`; nếu request timeout bot query
   lại theo client id một lần thay vì blind retry.
4. **Phí**: taker Binance USDT-M 0.05% (`fee_rate = 0.0005`), giống OKX.
   Funding và slippage vẫn cần đưa vào backtest/P&L đầy đủ trước khi tối ưu
   size dài hạn.
5. Không có paper mode nội bộ: so sánh paper-vs-real bằng bot OKX đang chạy
   trên máy Muse.

## Mô hình an toàn

- `mode`: `data_only` | `dry_run` (mặc định) | `live`. Đổi mode phải restart.
- **data_only**: chỉ đọc market data/candles và chạy regime/tín hiệu; không
  tạo vị thế paper, không gọi API xác thực. Đây là mode đầu tiên cần chạy sau
  khi IP hết ban.
- **dry_run**: không cần key, **không gọi bất kỳ API xác thực nào** (kể cả
  read-only), chỉ log "lệnh sẽ đặt". Dùng sau data-only để kiểm tra wiring
  của chiến lược mà chưa đặt lệnh.
- **live**: đọc key từ biến môi trường `BINANCE_API_KEY` /
  `BINANCE_API_SECRET`. Không hardcode, không ghi vào file, không log.
  Thiếu 1 trong 2 → từ chối khởi động (fail closed).
- Code **không chứa bất kỳ endpoint rút tiền nào** (test tự quét).
- Key trên Binance phải: **chỉ quyền Trade Futures, TẮT quyền Withdraw,
  whitelist IP của VPS**. Kiểm tra lại trên trang API Management trước khi live.
- Private user-data WS được mở trong `live`: listenKey keepalive định kỳ,
  reconnect chủ động trước lifetime 24 giờ, nhận `ORDER_TRADE_UPDATE` để lấy
  fill trước khi fallback REST và nhận `ACCOUNT_UPDATE` để đánh dấu thay đổi
  account. Nếu private WS gặp lỗi safety circuit, bot dừng fail-closed.
- Fast risk loop dùng Mark Price để cập nhật `mark_equity`, unrealized PnL,
  daily drawdown và grid basket loss. Daily stop không còn chỉ dựa trên
  realized balance. Reconciliation cũng halt nếu khoảng cách tới liquidation
  price nhỏ hơn `min_liquidation_buffer_pct` (mặc định 5%).
- Mặc định `exchange_protection=false`: SL/TP do process canh. Sau khi
  testnet/mock validation đạt, có thể bật `exchange_protection=true` để tạo
  Algo Order `STOP_MARKET`/`TAKE_PROFIT_MARKET` theo từng position; bot hủy
  algo còn lại trước khi market-close và halt entry nếu tạo protection thất
  bại. Không bật flag này trên mainnet khi chưa kiểm tra payload Hedge Mode.
- Startup luôn đối chiếu open normal orders và open Algo Orders theo các
  symbol bot quản lý; nếu protection bật còn kiểm tra
  symbol/positionSide/side/type/quantity của từng guard. Mismatch hoặc không
  đọc được đều halt để xử lý thủ công. Nếu lệnh MARKET timeout/partial hoặc không lấy
  được fill price, bot không dùng giá tham chiếu và không ghi position giả.
- Reconciliation nhóm quantity local theo `(symbol, LONG)` và
  `(symbol, SHORT)` để đối chiếu với aggregate Binance. Nếu drift hoặc có
  position không quản lý, bot fail-closed cả entry/auto-close để tránh gửi
  lệnh ngược chiều tạo position mới; cần đối chiếu thủ công trước khi resume.

## Rate-limit / IP-ban guard

- Mọi REST public dùng một `requests.Session` và governor chung theo
  `BINANCE_IP_SCOPE` + endpoint. Bot giữ một `bot.lock` để không có hai bản
  cùng host cùng đốt request budget.
- Log mỗi request có `request_id`, endpoint, HTTP status, Binance API code,
  latency, `X-MBX-USED-WEIGHT-*`, order-count headers và `Retry-After` (body
  được redact). Signed CCXT requests dùng `adjustForTimeDifference` và
  `recv_window_ms` (mặc định 5000 ms).
- HTTP **429**, **418**, hoặc API code **-1003** mở circuit persistent trong
  `binance_circuit.json`, dừng mọi request/trading và **không retry**. Không
  restart bot liên tục; kiểm tra mọi process dùng chung public IP trước.
- Lỗi order/private theo từng symbol/action có exponential cooldown
  (mặc định 30 giây → tối đa 15 phút), nên không lặp lại mỗi 0,5 giây.
- Khi chạy Hedge Mode, lệnh close dùng `positionSide` + chiều ngược lại và
  **không gửi `reduceOnly`**; Binance từ chối `reduceOnly` khi đã gửi
  `positionSide=LONG/SHORT`.
- Universe dùng raw id như `BTCUSDT`, còn CCXT được map sang unified id như
  `BTC/USDT:USDT`; quantity market lấy theo `MARKET_LOT_SIZE`, không lấy mù
  theo `quantityPrecision`.
- Nếu `bot.log` có `SAFETY STOP`, hãy lấy dòng `BINANCE HTTP/CCXT` ngay trước
  đó để biết status/code thực tế; không xoá circuit state để ép chạy lại.
  Lệnh xem nhanh: `grep -E 'BINANCE (HTTP|CCXT)|SAFETY STOP|CIRCUIT' bot.log`.

## P1: backtest / walk-forward (offline, trước Testnet/live)

`backtest.py` dùng Python standard library, không import exchange client và
không đọc API key. Dữ liệu giá là nến 5m đã đóng; 15m được aggregate từ 5m
và nến đang hình thành bị loại khỏi regime/EMA/RSI/breakout signal. Có thể tải
dữ liệu public Binance USD-M rồi chạy đánh giá:

```bash
# Không cần key; chỉ gọi public market-data endpoint
python3 backtest.py download --symbol BTCUSDT --days 90 \
  --output /tmp/btcusdt-5m.jsonl
python3 backtest.py download-funding --symbol BTCUSDT --days 90 \
  --output /tmp/btcusdt-funding.jsonl

# OOS là tiêu chí chính; không ghi params tối ưu vào config/live config
python3 backtest.py walk-forward --data /tmp/btcusdt-5m.jsonl \
  --funding-data /tmp/btcusdt-funding.jsonl --symbol BTCUSDT \
  --train-days 30 --test-days 7 --step-days 7 \
  --json-out /tmp/btcusdt-walk-forward.json

# Một config cố định, hữu ích để kiểm tra baseline/cost model
python3 backtest.py single --data /tmp/btcusdt-5m.jsonl \
  --funding-data /tmp/btcusdt-funding.jsonl --symbol BTCUSDT \
  --json-out /tmp/btcusdt-single.json
python3 test_backtest.py
```

Walk-forward chỉ search ba tham số exit/grid nhỏ (`scalp.tp_pct`,
`scalp.sl_pct`, `grid.step_mult`); leverage, margin, exposure cap và risk
limits giữ nguyên theo config. Mỗi fold tách train và test theo thời gian,
parameter chỉ chọn trên train, còn kết quả OOS từng window nằm trong
`folds_detail`. Report gồm return/P&L, max drawdown, profit factor, win rate,
trade count, expectancy, taker fees, slippage, funding, exposure, daily-stop,
grid-basket-stop và ambiguous intrabar events. Khi không truyền
`--funding-data`, report ghi rõ
`funding_status=not_supplied` và không giả định funding bằng 0.

OHLC không cho biết thứ tự chính xác khi cùng nến chạm TP và SL. Simulator
chọn intrabar path bảo thủ (`bullish: open → low → high → close`, `bearish:
open → high → low → close`) và ưu tiên SL trong trường hợp không xác định.
OOS synthetic là kiểm tra model/code, không phải bằng chứng strategy có lợi
nhuận; chỉ xem xét Testnet sau khi OOS trên dữ liệu thật và cost/funding
đầy đủ được duyệt.

## Chạy thử (data-only → dry-run)

```bash
pip install -r requirements.txt
cp config.example.json config.json
python3 test_binance.py          # smoke test offline, khong can key
python3 build_universe.py        # tao universe.json (public API, 1 lan)
# lan dau: sua mode thanh data_only, sau do chay mot minh bot
python3 binance_bot.py           # chi public data/candles, khong mo vi the
# sau khi data_only on dinh: sua mode thanh dry_run va restart
python3 binance_bot.py           # mo phong/log lenh, van khong dat lenh that
```

## Lên live (cần Cường duyệt từng bước)

Chỉ chuyển sang `live` sau khi `data_only` và `dry_run` đã chạy ổn định,
không có `429`, `418`, `-1003`, WS reconnect storm hoặc lỗi payload. Tham
chiếu tài liệu chính thức Binance về [USDⓈ-M REST](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info),
[user-data stream](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams),
[ORDER_TRADE_UPDATE](https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Order-Update)
và [New Algo Order](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order).

1. Trên Binance: tạo API key **chỉ Trade Futures**, **tắt Withdraw**,
   whitelist IP VPS.
2. Trên VPS: `export BINANCE_API_KEY=... BINANCE_API_SECRET=...`
   (hoặc cho vào EnvironmentFile của systemd service, chmod 600).
3. `cp config.example.json config.json`, sửa `"mode": "live"`.
   (Bắt buộc bật `"use_testnet": true` trước với key testnet để kiểm tra:
   listenKey/keepalive, client id timeout recovery, `ORDER_TRADE_UPDATE`,
   aggregate LONG/SHORT và payload Algo Order. `exchange_protection` vẫn để
   false cho tới khi test xong.)
4. Chạy testnet, xác nhận một open/close nhỏ và restart/reconnect không tạo
   lệnh trùng; kiểm tra `client_order_id`, event order, position aggregate.
5. Chỉ sau validation trên mới chuyển endpoint mainnet; theo dõi 30–60 phút
   đầu và đối chiếu app Binance.
6. Size test nhỏ trước (giảm `order_margin_usdt`), Cường gật đầu mới để size chuẩn.

Mọi thay đổi code đều push lên repo git (luật đứng).
