# Grid v2 — thiết kế đề xuất (chờ duyệt trước khi code)

Trạng thái: **ĐỀ XUẤT**. Phần đã làm xong: mục 0 (commit `8b95587`).
Phạm vi: `binance-bot/` (grid), `dashboard/`, `db/`, `backtest.py`.

Ưu tiên của anh: **tìm đúng coin/thời điểm đang thực sự đi ngang** để vào lệnh;
mỗi lệnh ăn **$10–15 đã trừ phí**; mọi tham số chỉnh được trên dashboard mà
không cần restart.

---

## 0. Đã sửa: basket stop không cắt khi lỗ $40–50 (`8b95587`)

Nguyên nhân đã xác nhận trong code:

1. `manage_grid_risk` và `update_positions` nằm trong nhánh `not reconcile_hold`.
   Halt kiểu `exchange position…`, `unmanaged…`, `position…` tắt luôn cơ chế cắt lỗ.
2. Symbol có cờ `risk_halted` bị bỏ qua đánh giá, dù vẫn còn lot.
3. `SYMBOLS` chỉ lấy từ `universe.json` (build lại theo volume 24h). Coin rớt
   khỏi top sau restart thì lot không còn giá mark, mất cả basket lẫn SL local.

Giả thuyết "lot không tag grid": không đúng, mọi đường mở grid đều gán tag.

Đã sửa:
- `run_risk_controls()` chạy **mỗi vòng, bất kể halt**: basket stop, trần lỗ
  tổng grid, SL/TP local; khi hold thì đặt lại SL/TP thiếu. Halt chỉ chặn mở
  lệnh mới.
- `risk_halted` chỉ chặn mở lệnh; lot còn tồn tại vẫn được đánh giá.
- **Trần lỗ tổng grid** `risk.grid_total_max_loss_pct` = 10% (mặc định).
- Symbol có lot đang mở luôn được theo dõi (manage-only, không mở mới).

---

## 1. Kinh tế một lệnh — cách đạt $10–15

Phí VIP0: maker 0,02%, taker 0,05%; trượt giá ước 0,01%.

| Lot (notional) | TP | Net hiện tại (market vào + TP market) | Net khi vào bằng LIMIT + TP market |
|---|---|---|---|
| $1.000 | 1,0% | $8,80 | $9,20 |
| $1.000 | 1,2% | $10,80 | **$11,20** |
| $1.000 | 1,5% | $13,80 | **$14,20** |
| $1.500 | 0,8% | $10,20 | **$10,80** |
| $1.500 | 1,0% | $13,20 | **$13,80** |
| $2.000 | 0,6% | $9,60 | **$10,40** |
| $2.000 | 0,8% | $13,60 | **$14,40** |

Hai đòn bẩy: **TP rộng hơn** (ít lần khớp hơn, cần coin có biên range đủ rộng)
hoặc **lot lớn hơn** (rủi ro mỗi tầng lớn hơn, basket phải tính lại). Cả hai đều
là tham số trên dashboard; nên chọn bằng backtest (mục 6), không chọn cảm tính.

Lưu ý quan trọng: **tỷ lệ TP/SL tự nó không tạo lợi thế.** Nếu giá đi ngẫu
nhiên, TP 1% / SL 3,5% có xác suất chạm TP trước ≈ 77,8%, nên kỳ vọng ≈ 0 trước
phí; TP 1% / SL 2% có xác suất ≈ 66,7%, cũng ≈ 0. Lợi nhuận chỉ đến từ việc
**vào đúng coin đang dao động hồi quy trong biên** (mục 4) và giảm phí (mục 3).

---

## 2. Hedge mode — đánh 2 chiều có nên dùng?

**Hiện tại grid đã giao dịch 2 đầu biên**: long ở các tầng dưới anchor, short ở
các tầng trên anchor. Long và short gần như không cùng tồn tại vì TP = 1 step:
long tầng 1 chốt lời ngay dưới anchor, trước khi giá tới tầng short đầu tiên.
Hedge mode lúc này chỉ là điều kiện kỹ thuật.

**Đề xuất: "range grid" 2 chiều theo vị trí trong biên.** Có dùng hedge thật,
nhưng không mở 2 chiều cùng một giá:
- Biên lấy từ scanner (mục 4): `range_low`, `range_high`, `mid`.
- Nửa dưới biên chỉ đặt **long**; nửa trên chỉ đặt **short**.
- TP = `tp_pct` (vd 1%), có thể lớn hơn khoảng cách tầng. Khi giá quét từ đáy
  lên đỉnh, long chưa chốt hết thì short đã mở, nên hai chiều cùng tồn tại. Đây
  là lúc hedge mode thực sự được dùng, và là đúng ý "ăn cả 2 đầu" khi đi ngang.
- **Không** mở long + short cùng lúc ở cùng một giá giữa biên: hai chiều triệt
  tiêu nhau, trả phí gấp đôi, không có lợi thế.
- Thoát khi biên vỡ: giá đóng nến 1h ra ngoài biên quá `break_buffer`, hoặc ADX
  1h vượt ngưỡng trend → huỷ lệnh chờ; chiều đang ngược trend thì cắt theo
  basket hoặc SL biên (mục 5).

---

## 3. Lệnh vào LIMIT, TP MARKET

Theo đúng thiết kế anh từng dùng:

**Lệnh vào: LIMIT đặt trước, có quản lý slot.**
- Lệnh limit post-only (GTX) tại các tầng của các symbol được scanner chọn.
  Maker 0,02% thay cho taker 0,05%; giá vào đúng tầng, không trượt.
- `grid.max_symbols` (vd 3): số symbol được chạy grid cùng lúc.
  `grid.max_lots_per_symbol`, `grid.max_total_lots`.
- **Đạt max thì huỷ mọi lệnh chờ còn lại**; khi có slot trống (lot đóng hoặc
  symbol bị loại) mới được đặt mới.
- Lệnh chờ có `entry_ttl` và bị huỷ khi biên vỡ / symbol rời top scanner.
- **Khớp một phần:** nguồn sự thật là `executedQty` trên sàn (ORDER_TRADE_UPDATE
  + userTrades). Khi lệnh khớp một phần rồi bị huỷ, hoặc hết TTL, phần đã khớp
  được ghi thành lot với **đúng qty đó**, đặt SL/TP cho đúng qty đó. Phần dưới
  minQty thì đóng market. Hạ tầng hiện có (client id, reconcile, userTrades,
  guard theo lot) đã đủ để làm việc này.

**TP: giữ MARKET.** TP hiện là algo `TAKE_PROFIT_MARKET` trên sàn: chạm giá là
khớp hết. Lập luận không chuyển sang TP limit:
- Tiết kiệm chỉ 0,03% (~$0,30/lot $1.000), tức 2–3% của mục tiêu $10–15.
- TP limit có rủi ro khớp một phần hoặc giá chạm rồi quay đầu mà không khớp, lot
  vẫn mở; phải thêm logic theo dõi và huỷ/đặt lại TP, tăng bề mặt lỗi.
- Có thể xem xét lại sau khi chạy ổn định, nếu số liệu cho thấy phí TP đáng kể.

---

## 4. Scanner đi ngang — trọng tâm

Chạy mỗi `scanner.rescan_minutes` (vd 15 phút) trên universe rộng hơn (top
50–80 theo volume, loại coin mới niêm yết < 30 ngày). Dùng nến **1h** để xác
định biên và **15m** để xác nhận:

| Chỉ báo | Ý nghĩa | Ngưỡng gợi ý (config) |
|---|---|---|
| ADX(14) 1h | Không có trend | < 20 |
| ADX(14) 15m | Không có trend ngắn hạn | < 22 |
| Độ rộng Bollinger (20, 2) 1h | Biên đủ rộng để ăn TP, không quá rộng | 2×TP + phí ≤ BBW ≤ `bbw_max` |
| Percentile BBW ~20 ngày | Không đang nở mạnh (breakout) | ≤ 80% (đã nới từ 50% khi code G2) |
| Range 24–48h (high/low) | Biên ổn định | `range_min` … `range_max` |
| Số lần cắt đường giữa trong lookback | Thật sự dao động qua lại | ≥ `min_mid_crosses` (vd 4) |
| Choppiness Index (cửa sổ 48h) / Efficiency Ratio | Đi ngang / không hiệu quả theo hướng | CHOP ≥ 45 và ER ≤ 0,35 (CHOP tính trên 48h, không dùng 14 nến) |
| Vị trí giá trong biên | Tránh vào khi đang ở sát mép | 15–85% |

- **Điểm tổng hợp** → chọn top `grid.max_symbols`. Dashboard hiển thị bảng xếp
  hạng (điểm, từng chỉ báo, lý do loại).
- **Chế độ quan sát trước:** scanner chạy, ghi log và hiển thị nhưng chưa điều
  khiển lệnh, để đối chiếu với thực tế 1–2 tuần.
- **Giảm vị thế khi chuyển trend:** symbol mất điểm (ADX 1h > `trend_exit_adx`
  hoặc biên vỡ) → huỷ lệnh chờ, không mở mới. Tuỳ chọn `derisk_on_trend`: đóng
  sớm các lot ngược trend nếu lỗ > `derisk_loss_pct`.
- Weight API: 80 symbol × klines 1h (limit 200, weight 2) / 15 phút ≈ 11
  weight/phút. Không đáng kể.

---

## 5. Rủi ro

| Lớp | Tham số | Ghi chú |
|---|---|---|
| Basket theo symbol | `risk.grid_basket_max_loss_pct` | Đã luôn chạy (mục 0) |
| Trần lỗ tổng grid | `risk.grid_total_max_loss_pct` = 10% | **Mới, đã có** |
| Daily stop | `risk.daily_max_loss_pct` (VPS đang 20%) | Có sẵn |
| **SL biên trên sàn** | `grid.boundary_sl_buffer` | **Mới:** SL mọi lot của symbol đặt tại `range_low − buffer` (long) / `range_high + buffer` (short). Khoản lỗ tối đa biết trước, kể cả khi bot chết |
| Đòn bẩy / exposure | `leverage`, `risk.max_notional_mult` | Có sẵn |

Basket stop và trần tổng là do bot tự canh; SL biên nằm trên sàn. Cả hai cùng tồn tại.

---

## 6. Config runtime: DB + cache, không cần restart

**Phân loại:**

| Loại | Ví dụ | Lưu ở | Đổi thì |
|---|---|---|---|
| Bí mật / hạ tầng | API key, `DATABASE_URL`, `mode`, `use_testnet`, `hedge_mode`, `exchange_protection` | env / `config.json` | Restart (cố ý) |
| Tham số chiến lược & rủi ro | lot, leverage, số tầng, step/TP, basket, trần tổng, daily, `max_symbols`, ngưỡng scanner | **DB** | Áp dụng nóng |

**Cơ chế:**
- Bảng `bot_config_versions(version serial, bot text, config jsonb, author,
  note, created_at)` lưu mọi phiên bản (audit, rollback được), và
  `bot_config_applied(bot, version, applied_at, error)` do bot ghi lại.
- Bot lúc khởi động: nạp version mới nhất → validate → cache trong RAM. DB lỗi
  thì dùng bản last-known-good `runtime_config.cache.json`; không có nữa thì
  dùng `config.json`.
- Code luôn đọc từ cache (`RUNTIME.get("grid.levels")`).
- Dashboard: nút **"Lưu & áp dụng"** ghi version mới và gửi `NOTIFY bot_config`.
  Bot kiểm tra version mỗi 10s (1 truy vấn nhỏ, không phụ thuộc NOTIFY bị lỡ).
  Version mới thì validate rồi đổi cache nguyên khối, và ghi `applied` +
  version. Dashboard hiển thị "bot đang chạy version X" để xác nhận.
- **Validate 2 lớp** (dashboard và bot), có biên an toàn, ví dụ leverage 1–20,
  basket 0,5–10%, trần tổng ≤ daily. Sai thì bot giữ bản cũ và ghi lỗi.
- **Ngữ nghĩa áp dụng:** tham số rủi ro áp dụng ngay; lot/leverage cho lệnh mới;
  hình học lưới (tầng, step, TP) từ lần rebuild kế tiếp, **không sửa lot đang mở**.
- **Bảo mật:** dashboard hiện **không có xác thực**. Trang config bắt buộc có
  mật khẩu admin (hash trong env), ghi `author` vào mỗi version.

---

## 7. Kiểm chứng (được phép tuỳ chỉnh backtest)

1. **Kiểm chứng scanner:** với dữ liệu lịch sử nhiều symbol, tại mỗi thời điểm
   scanner nói "đi ngang", mô phỏng grid trong N giờ sau và so với khi scanner
   nói "không". Scanner chỉ có giá trị nếu nhóm "đi ngang" lời rõ hơn.
2. **Backtest grid v2:** vào limit (khớp khi nến chạm tầng), TP market, SL biên,
   basket, trần tổng. Walk-forward cho các bộ tham số (lot, TP, tầng, ngưỡng scanner).
3. **Dry-run trên VPS** với scanner + config DB, sau đó **testnet**, cuối cùng
   live với lot nhỏ; Cường duyệt từng bước.

Sandbox không tải được dữ liệu Binance, nên backtest chạy trên VPS; em cung cấp
lệnh và mẫu báo cáo.

---

## 8. Lộ trình đề xuất

| Giai đoạn | Nội dung | Ảnh hưởng lệnh thật |
|---|---|---|
| G0 | Sửa basket + trần tổng 10% | ✅ đã xong (`8b95587`) |
| G1 | Config runtime DB + cache + trang dashboard (đăng nhập, user lưu DB) | ✅ xong (`869aba8` + dashboard) — xem `docs/upgrade.md` mục 9 |
| G2 | Scanner đi ngang, **chế độ quan sát** + bảng xếp hạng trên dashboard | ✅ xong — ngưỡng CHOP/percentile đã chỉnh, xem `docs/upgrade.md` mục 9.3 |
| G3 | Backtest: kiểm chứng scanner + grid v2 | Không |
| G4 | Range grid 2 chiều theo scanner, `max_symbols`, SL biên, giảm vị thế khi trend | Có (dry-run → testnet) |
| G5 | Lệnh vào LIMIT post-only + quản lý slot + xử lý khớp một phần | Có (testnet bắt buộc) |
