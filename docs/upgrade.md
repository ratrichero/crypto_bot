# Nâng cấp nhánh `arena/1b7a22f9-crypto-bot` — thay đổi và lưu ý vận hành

Tài liệu tổng hợp mọi thay đổi trên nhánh `arena/1b7a22f9-crypto-bot` kể từ
`cf8d9e9` (main), lý do, cách deploy và các rủi ro còn lại. Phạm vi: **Binance
live** (`binance-bot/`), **radar meme live** (`meme-radar/live_trader.py`,
`radar.py`), dashboard và DB. Bot OKX (`trading-bot/`) không đổi.

Quy ước: mỗi lỗi một commit, mỗi commit có test. Với lỗi logic, test đã được
chạy ngược với code cũ để chứng minh test bắt được lỗi. Không tạo pull request;
deploy thử nghiệm trực tiếp từ nhánh này.

> **Trạng thái kiểm thử:** toàn bộ test offline pass (bảng ở mục 8). **Chưa
> chạy trên Binance testnet hay mainnet.** Lên live vẫn theo luật repo: Cường
> duyệt từng bước.

---

## 1. Deploy nhanh (checklist)

1. `git pull` nhánh `arena/1b7a22f9-crypto-bot` trên VPS.
2. **DB:** chạy lại `psql cryptobots < db/schema.sql` (dùng `ADD COLUMN IF NOT
   EXISTS`, chạy lại an toàn). `sync_jsonl.py` và bot cũng tự `ALTER` nếu thiếu
   cột; ALTER lỗi thì fallback sang câu INSERT cũ, không mất trade.
3. **Binance config:** không bắt buộc sửa `config.json`. Các key mới đều có
   default trong code (mục 6). Muốn chỉnh thì xem `binance-bot/config.example.json`.
4. **Live trader config:** không bắt buộc sửa `config.live.json`. Key mới
   `pending_buy_expire_seconds` (default 180).
5. Restart theo thứ tự: `sync_jsonl` → `binance_bot` → `live_trader` → dashboard.
6. **Pending CLAUDIA đang chặn live trader:** không cần xoá tay. Ở lần
   reconcile đầu tiên sau restart:
   - ví không có CLAUDIA → pending tự gỡ (đã quá 180s), entry mở lại;
   - ví có CLAUDIA → bot nhận lại vị thế (recover) rồi mới gỡ.
   Kiểm tra log: `RESOLVE pending BUY CLAUDIA ...` hoặc `RECOVER position CLAUDIA ...`.
7. Theo dõi 30–60 phút đầu: không có `CRITICAL` mới, dashboard ô HALT đúng
   trạng thái, PnL trade mới có `fee_entry`/`fee_exit`.

---

## 2. Radar meme live (`meme-radar/`)

### 2.1 Lỗi đã sửa

| Commit | Lỗi | Sửa |
|---|---|---|
| `b4098d2` | Khi PAUSE, exit ladder và smart exit không chạy; leg bán lỗi vẫn bật cờ TP | PAUSE vẫn chạy exit/smart exit, chỉ ngừng mua mới; leg lỗi thì hoàn tác cờ |
| `d52c0a3` | Bán từng phần tính theo lượng mua ban đầu, nhưng swap bán theo số dư hiện tại → bán sai khối lượng | Quy đổi `frac` theo số dư đang có trên ví |
| `be92c20` | Guard price impact coi `priceImpactPct` của Jupiter là %, thực tế là phân số → guard gần như không bao giờ chặn | So sánh đúng đơn vị |
| `dfd0511` | Mua đuổi signal cũ (backlog sau restart, retry kéo dài) | Bỏ signal cũ hơn `max_signal_age_seconds` = 120s |
| `6fad65c` | Helius API key lộ trong log/state | Che key ở mọi log/lỗi/state |
| `8407535` | Token airdrop/dust chiếm slot `max_positions` → bot âm thầm ngừng mua | Chỉ tính token bot thật sự mua |
| `83a7164` | Leg bán "không chắc kết quả" có thể bị bán trùng | Chặn leg từng phần cho tới khi xác minh số dư; leg bán sạch vẫn được phép |
| `62aefb3` | Ví hết token lúc bán → ghi lỗ giả | Không ghi P&L giả |
| `8b9ed65` | Reconcile đóng vị thế mất token mà không ghi `live_trades` | Ghi trade record (P&L phần còn lại = không xác định) |
| `82f1508` | Giá fallback DexScreener lấy sai pair | Chọn đúng pair |
| `5f795d8` | Lệnh mua chậm (~100s) chặn exit của các vị thế khác | Chạy exit giữa các lần mua |
| `9c05276` | Test biên 120s không ổn định | Đồng hồ cố định trong test |
| `6b3fca8` | File `STOP` của radar paper cũng dừng live trader | Kill switch riêng `STOP_LIVE`; `PAUSE` = chỉ ngừng mở mới |
| `69ea3d1` | Nguồn `sells.jsonl` được khai báo nhưng không ai ghi, không ai đọc | Gỡ. **Không phải lỗi chức năng:** smart exit thật đi qua `alerts.jsonl`, loại `sell_cluster` |
| `ae6f3a4` | `radar.py ds_token` lấy `rows[0]` của DexScreener (có thể là pool rác/sai quote) | `pick_pair` chọn đúng cặp |
| `a005c49` | **Live trader BLOCKED: "pending BUY chua reconcile"** (VPS 2026-10-07 04:12:28) | Mục 2.2 |

### 2.2 Lỗi "pending BUY chua reconcile" — chẩn đoán và cách sửa

**Chẩn đoán của Muse chưa đúng ở điểm mấu chốt.** `pending_buys` không nhầm
signal với lệnh của bot. Nó **luôn là ý định mua của chính bot**, được ghi
trong `_attempt_signal` ngay trước khi gọi swap ("durable intent before a
network side effect"), để chống trường hợp bot crash giữa lúc gửi tx và lúc
lưu state. Con số $982.8 là `amount_usd` của **signal** (số tiền ví
`Ar2Y6o1QmrRA` đã mua), được lưu kèm để có thể recover. Nó không phải size
lệnh của bot.

**Nguyên nhân thật:** pending chỉ được gỡ khi token **về ví** (qua position
hoặc recover). Nếu lệnh mua của bot không land (send timeout, tx rớt, blockhash
hết hạn), không đường nào gỡ được pending → block entry vĩnh viễn. Code cũ
trong test tái hiện đúng `block_reason = "pending BUY chua reconcile"`.

**So sánh các phương án:**

- *Option A (xoá tay):* lỗi lặp lại.
- *Option B như Muse mô tả (chỉ ghi pending khi đã có tx signature):* mất lớp
  bảo vệ crash. Bot chết sau khi gửi tx nhưng trước khi lưu → lần sau không biết
  mình đã mua → token mồ côi hoặc mua lặp.
- *Option C (tự xoá sau 5 phút):* nguy hiểm nếu không kiểm tra ví và tx.

**Đã làm (đúng tinh thần Option B, giữ được bảo vệ crash):**

1. Signature của tx Solana là chữ ký đầu tiên, biết ngay khi ký.
   `Swapper._sign_and_send(on_signed=...)` gọi hook **trước** `sendTransaction`;
   `_attempt_signal` ghi `tx`, `sent_at`, `status="buy_sent"` vào pending và
   `save()` ngay. Từ giờ mỗi pending gắn với tx thật của bot.
2. `reconcile_onchain` → `_resolve_pending_buys` đối chiếu theo on-chain:

   | Tình huống | Xử lý |
   |---|---|
   | Ví có token | Recover vị thế (dùng signal lưu trong pending nếu `signals.jsonl` không còn) → gỡ |
   | Tx của bot `failed` on-chain | Gỡ ngay |
   | Tx `processed/confirmed/finalized` mà ví **không** có token | **Giữ block** + log `CRITICAL` một lần, `status="landed_no_balance"` → cần kiểm tra tay |
   | Tx không tồn tại, hoặc chưa có tx (pending cũ / chưa kịp ký), và đã quá `pending_buy_expire_seconds` (180s, dài hơn hạn blockhash ~60–90s) | Gỡ, đánh dấu signal `processed` (không mua lại) |
   | Chưa quá hạn | Chờ vòng reconcile sau (60s) |

3. Lỗi đọc status RPC → giữ pending (fail-closed), thử lại vòng sau.

### 2.3 Lưu ý vận hành live trader

- `STOP_LIVE` = dừng hẳn live trader (vị thế vẫn mở, đóng tay). `PAUSE` = chỉ
  ngừng mở mới, exit vẫn chạy. File `STOP` chỉ dừng radar paper.
- Log cần theo dõi: `RESOLVE pending BUY`, `CRITICAL pending BUY ... landed`,
  `RECOVER position`, `CRITICAL unmanaged token`.
- Pending ở trạng thái `landed_no_balance` không tự gỡ: kiểm tra tx trên
  Solscan, rồi xoá tay khỏi `live_state.json` → `pending_buys`.

---

## 3. Dashboard

| Commit | Lỗi | Sửa |
|---|---|---|
| `6441184` | OKX: phí đóng lệnh bị trừ 2 lần | Net = `pnl - fee/2` (OKX `pnl` đã trừ phí đóng) |
| `f53afe8` | Ô ví SOL đọc sai ví | Dùng đúng ví live trader `DxYkrsJA6YdS1cqJ9ocPCYRBacd7Xan3DeYWZva89dLd` |
| `22c77fc` | Ô HALT radar live không phản ánh trạng thái thật | Đọc `entry_blocked`/`block_reason`/`block_since` của live trader |

---

## 4. Binance live — vòng đời lệnh (API sàn là nguồn sự thật)

Mục tiêu: từ lúc sinh lệnh tới lúc đóng, mọi dữ liệu (qty, giá, phí, trạng
thái SL/TP) đều đồng bộ từ sàn. Không còn các lỗi đã biết: thiếu TP/SL, không
dọn lệnh dư, không recheck.

### 4.1 Đợt 1 — SL/TP trên sàn, lệnh mồ côi, sàn tự đóng (commit 1–11)

| Commit | Nội dung |
|---|---|
| `7f576d9` | `close()` đối chiếu tổng các lot cùng chiều (grid nhiều lot không còn halt giả) |
| `73fd99d` | `detect_exchange_closed` xác nhận 2 lần liên tiếp, bỏ qua lot mở < 60s |
| `7f859ca` | Giá đóng thật đúng leg / thời điểm / khối lượng (`_exchange_exit_from_trades`) |
| `0c3f6e1` | Huỷ SL/TP còn treo của lot đã đóng ngoài sàn |
| `9716bb5` | Reason SL/TP theo fill thật (cooldown scalp đúng) |
| `5880fb9` | Theo dõi vòng đời algo qua WS `ALGO_UPDATE` + REST; sàn tự khớp TP/SL → ghi PnL thật, huỷ chân còn lại |
| `d0379c9` | Hết đua giữa bot và TP/SL trên sàn (`GuardAlreadyFilled`/`GuardInFlight`); restart không xoá âm thầm lot đã khớp |
| `c393c36` | Mỗi lot luôn có đủ 1 SL + 1 TP (chỉ đặt chân thiếu); thiếu SL quá deadline → đóng |
| `447e86e` | Dọn lệnh điều kiện mồ côi theo tham chiếu lot (`b<SYM><L/S><n>`), không theo symbol |
| `11be5d0` | Startup: lot thiếu guard → đặt lại thay vì halt vĩnh viễn |
| `eb9b241` | Docs/config |
| `125ebdd`, `509c08b`, `d3aa7ad` | (Muse, đã merge ở `df9ec81`) detect 10s, phát hiện đóng bởi TP/SL, giá khớp thật |

### 4.2 Đợt 2 — audit vòng đời (B1–B13)

| # | Commit | Lỗi cũ | Sửa |
|---|---|---|---|
| B1 | `573e370` | `close()` tính lại qty qua nhân/chia float → ~16% lần đóng hụt 1 step, để lại bụi không TP/SL → mismatch → halt | Đóng đúng qty của lot; lot cuối của leg quét bụi ≤ 2 step |
| B2 | `c4a1a17` | PnL dùng phí ước tính, chưa trừ phí mở | `pnl` = gộp − phí đóng thật − phí mở thật (lấy `commission` từ userTrades); lệnh đóng nhiều lot chia phí theo qty; phí BNB / lỗi đọc → `fee_estimated=true` |
| B3 | `c5c0f22` | Lot mở > 7 ngày hoặc symbol nhiều trade → không tìm thấy fill đóng thật | Truy vấn đúng cửa sổ 7 ngày của userTrades |
| B4 | `c83fb9c` | DB không có cột phí/nguồn giá | Thêm `pnl_gross, fee_entry, fee_exit, fee_estimated, estimated, exit_source` |
| B5 | `303ec90` | Khi reconcile hold, SL/TP thiếu không được đặt lại → lot trần | `protect_during_hold` vẫn đặt chân thiếu (lệnh chỉ đóng, luôn giảm rủi ro), không ép đóng |
| B6 | `20a11c5` | Algo người dùng đặt tay → halt; halt lúc startup không bao giờ recheck | Chỉ quản lý algo có client id của bot; halt từ startup recheck mỗi `startup_hold_recheck_seconds` (60s), tự gỡ khi khớp |
| B7 | `d194aff` | Đóng tay một phần leg grid → halt "mismatch" | `_detect_partial_leg_reduction`: ghi nhận lot theo LIFO khi phần giảm khớp đúng qty, xác nhận 2 lần quét, giá từ fill đóng thật (`exit_source=exchange_detect_partial`) |
| B8 | `29ecee3` | Daily stop không chạy khi bot đang halt vì lý do khác; lot đóng lỗi không thử lại | Kích hoạt 1 lần/ngày (`state["daily_stop_day"]`) cả khi đang halt, giữ halt_reason cũ, vòng sau thử lại lot còn sót |
| B9 | `d61dd32` | Grid basket stop chỉ thử đóng 1 lần; lot lỗi bị bỏ vĩnh viễn | Cờ `grid["basket_stopping"]`: thử lại tới khi hết lot |
| B10 | `4db2763` | Lệnh mở đã khớp nhưng response/WS/`fetch_order` thiếu giá → halt, **không ghi lot, vị thế trên sàn không SL/TP** | Fallback cuối: giá từ userTrades của chính order; phí BNB không còn làm mất giá |
| B11 | `0cf5df5` | Lệnh mở timeout mơ hồ → halt "ambiguous", không lưu gì; nếu sàn đã khớp → vị thế trần, halt vĩnh viễn | Lưu ý định vào `state["ambiguous_orders"]`; `resolve_ambiguous_orders` (30s, chạy cả khi hold) tra theo `clientOrderId`: khớp → nhận lot + SL/TP; huỷ/không tồn tại sau 300s → bỏ; hết danh sách → gỡ halt |
| B12 | `8c2bd8f` | **Nghiêm trọng:** `close()` huỷ guard rồi lệnh market lỗi → lot vẫn giữ algo id cũ, báo "armed" trong khi sàn **không có SL/TP**; halt vĩnh viễn | Huỷ chắc chắn → xoá id ngay → `retry_protection` đặt lại; halt "close action" tự gỡ khi sàn khớp state và mọi lot đủ guard |
| B13 | `3ee1d79` | Sàn đã nhận lệnh mở nhưng mọi nguồn giá lỗi → vị thế trần | Đưa vào `ambiguous_orders` như B11 |
| — | `33392b9` | Docs | README Binance, config example, comment schema |

### 4.3 Thứ tự main loop (sau nâng cấp)

```
sync_exchange_protection → detect_exchange_closed (gồm partial)
→ [live] resolve_ambiguous_orders → [live] recheck_startup_holds
→ reconcile_positions → enforce_daily_stop
→ nếu reconcile hold: chỉ protect_during_hold
  ngược lại: manage_grid_risk → update_positions → retry_protection + drain
             → cleanup_orphan_orders
→ mở grid → slow path (klines/tín hiệu)
```

### 4.4 Halt và cách phục hồi

| halt_reason (prefix) | Tự phục hồi khi |
|---|---|
| `exchange position reconciliation mismatch` | Reconcile thấy sàn khớp state |
| `unmanaged exchange position`, `exchange protection reconciliation ...` (lúc startup) | Recheck 60s thấy đã khớp (B6) |
| `close action requires reconciliation` | Sàn khớp state **và** mọi lot đủ SL/TP (B12) |
| `ambiguous market order ...`, `order fill reconciliation required` | Danh sách `ambiguous_orders` rỗng (B11/B13) |
| `daily stop` | Sang ngày mới |
| Lệch không khớp được lot nào (vd đóng tay một khối lượng lẻ) | **Không tự gỡ** (chủ ý) → xử lý tay |

### 4.5 Ngữ nghĩa PnL/phí và DB

- `pnl` = `pnl_gross` − `fee_exit` − `fee_entry`. Phí thật từ userTrades; ước
  tính thì `fee_estimated=true`.
- `exit_source`: `bot` | `exchange_algo` | `exchange_detect` | `exchange_detect_partial`.
- **Record cũ (trước `c4a1a17`):** `pnl` chưa trừ phí mở, nên cao hơn thực tế
  khoảng 0.05% notional. Cột mới để NULL. Khi so sánh hiệu suất trước/sau nâng
  cấp cần lưu ý điểm này.
- `binance_trades.id` là PRIMARY KEY, INSERT dùng `ON CONFLICT (id)` → không ghi trùng.

### 4.6 State key mới (`binance-bot/state.json`)

`daily_stop_day`, `ambiguous_orders`, `grids[*].basket_stopping`. Trên lot:
`fee_entry_estimated`, `protection_status`, `protection_retry_at`,
`protection_deadline`. Tất cả tương thích ngược: state cũ nạp bình thường.

---

## 5. Đánh giá các điểm Muse đề xuất

### 5.1 "Chưa xử lý case bot restart giữa lúc TP vừa khớp" — **đã xử lý**

Đã có test `test_restart_after_offline_tp_books_pnl` (từ `d0379c9`/`5880fb9`).
Ngoài ra đã kiểm thêm các biến thể:

| Kịch bản | Kết quả |
|---|---|
| TP khớp lúc bot tắt → restart | Lot không bị xoá âm thầm; `sync_exchange_protection` đọc trạng thái algo qua REST (WS bị lỡ), ghi trade reason `TP` với giá khớp thật, huỷ SL còn lại, reconcile tự hết halt |
| Restart đúng lúc TP đang `TRIGGERED` (lệnh market đang chạy) | Startup không halt; `close()` gặp `GuardInFlight` thì hoãn, không gửi lệnh đối ứng; khi TP xong → ghi TP @giá thật, không còn guard thừa |
| Bot ghi trade TP rồi crash **trước khi lưu state** | Lot được ghi nhận lại sau restart với **cùng `id` và cùng lệnh đóng**. Equity không bị cộng đôi (state đã rollback). DB bỏ bản trùng (`ON CONFLICT (id)`), dashboard đúng. **Còn lại:** `trades.jsonl` có 1 dòng audit trùng → mức nhẹ, chưa sửa |
| Bot đang `close()` (lệnh market đã gửi) thì crash | Restart: leg trên sàn giảm/hết → `detect_exchange_closed` ghi theo fill đóng thật (2 lần quét xác nhận) |

### 5.2 "Config mới đã tune phù hợp rate limit chưa" — **đủ an toàn với default**

Governor nội bộ (`binance_safety.RequestGovernor`) giới hạn **1.200
weight/phút**, bằng 50% hạn mức IP của Binance USDⓈ-M (2.400). Mọi lời gọi
private (`_private_call`) và public (`binance_client._get`) đều đi qua governor
với weight khai báo. Ước tính tải ổn định, trường hợp xấu (11 symbol có lot =
grid 7 + scalp 4, universe 30):

| Tác vụ | Chu kỳ (key config) | Weight/lần | Weight/phút |
|---|---|---|---|
| Klines 5m + 15m × 30 symbol | `candle_refresh_seconds` 60 | 2 | 120 |
| Equity (`fetch_balance`) | `equity_refresh_seconds` 5 | 5 | 60 |
| `sync_exchange_protection` (openAlgoOrders theo symbol) | `protection_sync_seconds` 10 | 1 × S | ≤ 66 |
| `cleanup_orphan_orders` (openAlgoOrders mọi symbol) | `orphan_cleanup_seconds` 60 | 40 | 40 |
| `detect_exchange_closed` (`fetch_positions`) | `exchange_close_check_seconds` 10 | 5 | 30 |
| `reconcile_positions` | `reconcile_interval_seconds` 60 | 5 | 5 |
| `resolve_ambiguous_orders` (chỉ khi có) | `ambiguous_order_check_seconds` 30 | 1 / lệnh | ~2 |
| **Tổng ổn định** | | | **≈ 320** (≈ 27% governor, ≈ 13% hạn mức Binance) |
| `recheck_startup_holds` (chỉ khi đang halt từ startup) | `startup_hold_recheck_seconds` 60 | 80–120 (open orders 40 + open algo 40 + cleanup 40) | +80–120 → vẫn < 450 |

Phát sinh theo sự kiện: mỗi lệnh mở/đóng thêm userTrades (5), verify
`fetch_positions` (5), 2 algo order. Giới hạn order của Binance (1.200/phút,
300/10s) cách rất xa.

**Kết luận:** không cần tune. Nếu mở rộng thì chú ý: universe tăng lên 100
symbol → klines ≈ 400/phút, vẫn dưới governor. `equity_refresh_seconds`
có thể tăng lên 10 nếu muốn giảm 30 weight/phút. Không nên giảm
`orphan_cleanup_seconds` dưới 30 (40 weight mỗi lần).

---

## 6. Key config mới

**`binance-bot/config.json`** (đều có default trong code):

| Key | Default | Ý nghĩa |
|---|---|---|
| `exchange_protection` | false (VPS đang bật `true`) | SL/TP Algo Order trên sàn |
| `protection_sync_seconds` | 10 | Chu kỳ đồng bộ trạng thái SL/TP với sàn |
| `protection_grace_seconds` | 15 | Chờ sàn khớp guard trước khi bot tự market-close |
| `protection_sl_deadline_seconds` | 120 | Thiếu SL quá hạn → đóng lot |
| `orphan_cleanup_seconds` | 60 | Chu kỳ dọn algo mồ côi |
| `startup_hold_recheck_seconds` | 60 | Recheck halt phát hiện lúc startup |
| `exchange_close_check_seconds` | 10 | Chu kỳ phát hiện đóng ngoài bot |
| `exchange_close_min_age_seconds` | 60 | Bỏ qua lot mới mở |
| `ambiguous_order_check_seconds` | 30 | Chu kỳ tra lệnh mở mơ hồ |
| `ambiguous_order_expire_seconds` | 300 | Bỏ lệnh mơ hồ không tồn tại trên sàn |

**`meme-radar/config.live.json`:**

| Key | Default | Ý nghĩa |
|---|---|---|
| `max_signal_age_seconds` | 120 | Bỏ signal cũ |
| `pending_buy_expire_seconds` | 180 | Gỡ intent BUY không land |

---

## 7. Rủi ro còn lại và việc nên làm tiếp

1. **Chưa test trên testnet.** Ưu tiên test với Binance testnet: timeout lệnh
   mở (B11), lệnh đóng lỗi sau khi huỷ guard (B12), đóng tay một phần leg grid
   (B7), restart khi TP vừa khớp.
2. Lệch vị thế không khớp được với lot nào (vd đóng tay khối lượng lẻ) vẫn halt
   chờ xử lý tay. Đây là chủ ý.
3. `trades.jsonl` của Binance có thể có dòng trùng `id` nếu crash giữa lúc ghi
   trade và lúc lưu state. DB/dashboard không ảnh hưởng.
4. Live trader: pending `landed_no_balance` (tx land mà ví không có token) vẫn
   block, cần kiểm tra tay. Đây là chủ ý, tránh mua chồng khi không rõ token đi đâu.
5. Phí Binance trả bằng BNB → PnL dùng phí ước tính (`fee_estimated=true`).

---

## 8. Test

```bash
# Binance
cd binance-bot
for t in test_binance.py test_backtest.py test_protection.py; do python $t; done
rm -f config.json universe.json        # file do test sinh ra, không commit
# DB (Postgres thật qua pgserver)
python db/test_binance_trades_pg.py
# Radar meme / dashboard
cd meme-radar && python test_live_trader.py && python test_dexscreener.py; rm -f live_trader.log
cd dashboard && python test_dashboard.py
```

| Bộ test | Kết quả |
|---|---|
| `binance-bot/test_binance.py` | 97 pass |
| `binance-bot/test_backtest.py` | 17 pass |
| `binance-bot/test_protection.py` (harness sàn giả lập) | 136 pass |
| `db/test_binance_trades_pg.py` | 7 pass |
| `meme-radar/test_live_trader.py` | 145 pass |
| `meme-radar/test_dexscreener.py` | 14 pass |
| `dashboard/test_dashboard.py` | 15 pass |

Các dòng `Traceback ...` / `FAIL attempt=1` trong output test là log có chủ
đích của kịch bản lỗi, không phải test hỏng. Chỉ coi là đạt khi dòng cuối là
`N passed, 0 failed` / `N pass, 0 fail`.
