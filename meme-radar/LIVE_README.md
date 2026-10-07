# Live Trader — "đàn quạ" bản tiền thật

Module `live_trader.py` biến tín hiệu của radar (`signals.jsonl`) thành lệnh
swap THẬT trên Solana qua Jupiter. Chạy ĐỘC LẬP với `radar.py` (paper) —
không sửa, không restart radar.

## Kiến trúc

```
radar.py (paper, chay san 24/7)
   | ghi signals.jsonl (buy, kem wallet_price_usd + liquidity_usd)
   |   + alerts.jsonl (sell_cluster, wallet_sell)
   v
live_trader.py -- tail file, khong import radar
   |--- KIEM TRA: mint (authority/Token-2022) -> quote mua -> chong mua duoi
   |              -> quote thu ban lai (lo khu hoi / honeypot)
   |--- BUY:  Jupiter quote SOL->token -> swap -> sign (solders) -> gui qua Helius RPC
   |--- SELL: Jupiter quote token->SOL -> swap -> sign -> gui (tung leg,
   |          lenh thoat bat buoc that bai -> nang bac slippage/phi)
   |--- Gia:  Jupiter Price API v3 (1 request/moi vi the), thieu -> quote
   |          token->USDC (fallback DexScreener)
   |--- RENT: ban sach -> CloseAccount token account rong -> rent ve vi
   v
live_positions.json (vi the mo) + live_trades.jsonl (lenh da dong)
```

**Seam tích hợp:** tail `signals.jsonl`/`alerts.jsonl` theo byte-offset
(lưu trong `live_state.json`). Lần chạy đầu bắt đầu từ CUỐI file —
không đánh theo tín hiệu cũ. Zero thay đổi vào `radar.py`.

## Exit ladder (mirror y hệt paper)

| Điều kiện | Hành động |
|---|---|
| +50% (TP1) | bán 1/3 |
| +100% (TP2) | bán thêm 1/3 |
| sau TP2, rớt -30% từ đỉnh | trailing, bán hết |
| -25% | SL, bán hết |
| ≥2 ví tracked xả cùng token/30p | smart exit, bán hết |
| ví nguồn đã bán cộng dồn ≥50% lượng đang giữ | copy exit, bán hết |
| hết 480 phút, lãi ≥20% | chốt 1/2, giữ 1/2 trailing |
| hết 480 phút, lãi <20% | bán hết |

Thứ tự ưu tiên khi nhiều điều kiện cùng đúng trong 1 poll:
trailing → SL → smart exit → copy exit → time stop (giống paper).

## Mô hình an toàn

- **Mặc định `dry_run`**: chỉ log ý định swap, không gửi transaction,
  không cần key. Dùng để kiểm tra logic trước.
- **`live` fail-closed**: thiếu file `.solana_key`, key sai định dạng,
  hoặc pubkey không khớp `wallet_address` → chương trình TỰ DỪNG.
- **Kill switch**: tạo file `STOP_LIVE` trong thư mục này → vòng lặp dừng
  nhẹ nhàng. ⚠️ **STOP_LIVE không tự đóng vị thế đang mở** — phải xử lý tay.
  File `STOP` là kill switch của `radar.py` (paper): live trader chỉ log
  cảnh báo, KHÔNG dừng (trước đây dùng chung → tắt radar là tắt luôn live).
- **PAUSE**: tạo file `PAUSE` trong thư mục này → ngừng MỞ MỚI nhưng vẫn
  reconcile ví, nhận smart exit (sell_cluster) và chạy đủ exit ladder
  (TP/SL/trailing/time stop). Tín hiệu đến trong lúc PAUSE bị đánh dấu
  `skipped_paused` — xoá PAUSE xong bot KHÔNG mua đón tín hiệu cũ.
- **Leg bán thất bại** (no route, lỗi RPC…): hoàn tác `remaining` và cờ của
  điều kiện đó (TP1/TP2/TIME/TIME_KEEP) để poll sau thử lại, không bị kẹt.
- **Daily stop**: ngừng MỞ MỚI khi lỗ thực tế trong ngày (UTC) <
  `-daily_stop_pct` × portfolio đầu ngày. Vị thế cũ vẫn được quản lý exit.
- **Fee buffer**: luôn giữ tối thiểu `fee_buffer_sol` SOL cho phí.
- **Price impact guard**: bỏ qua lệnh nếu impact vượt `max_price_impact_pct`.
- **Slippage**: giới hạn bởi `slippage_bps` ngay trong quote Jupiter
  (on-chain guard `otherAmountThreshold`).
- **Retry**: quote/swap thử lại tối đa `max_swap_retries` lần với
  blockhash mới. Lệnh bán luôn tính theo SỐ DƯ THỰC trên ví nên không
  bao giờ bán trùng 2 lần.
- Private key KHÔNG BAO GIỜ được log. Jupiter API key cũng bị che.
- **Lọc token trước khi mua** (fail closed, xem mục Nâng cấp 10/2026).
- **Thoát khẩn cấp**: SL/TRAIL/SMART_EXIT/COPY_EXIT/TIME bán thất bại chắc
  chắn → vòng sau thử lại ngay với bậc `exit_escalation` kế tiếp.

## Cách chạy

```bash
cd ~/workspace/meme-radar
cp config.live.example.json config.live.json   # sua neu can
.venv/bin/python live_trader.py                 # dry_run mac dinh
```

## Dry-run trước

1. Chạy `dry_run` ít nhất vài giờ, xem `live_trader.log`: tín hiệu có
   được bắt không, exit ladder có chạy đúng không (`live_trades.jsonl`
   bản mô phỏng).
2. Kiểm tra `live_positions.json`/`live_state.json` persist đúng sau
   restart (Ctrl+C rồi chạy lại).

## Go-live checklist (tiền thật)

- [ ] `.solana_key` tồn tại, chmod 600, chứa base58 secret key của ví
      `DxYkrsJA6YdS1cqJ9ocPCYRBacd7Xan3DeYWZva89dLd`
- [ ] Chạy thử với `trade_size_usd` nhỏ ($10) và `max_positions` nhỏ
- [ ] Ví có đủ SOL: tiền trade + `fee_buffer_sol` + phí tx (~0.0001 SOL/lệnh)
- [ ] Đã chạy dry-run và đối chiếu logic exit với paper
- [ ] Hiểu rõ: STOP_LIVE không đóng vị thế; smart contract/pool rủi ro là
      của mình; meme có thể về 0 trong vài phút
- [ ] Đổi `"mode": "live"` trong `config.live.json` (cần restart)

## Nâng cấp 10/2026 (meme radar 1→6 + paper khả thi)

| # | Tính năng | Config (`config.live.json`) |
|---|---|---|
| 1 | Thu hồi rent token account (~0.00204 SOL/token) sau khi bán sạch; rent lần mua đầu được tách khỏi giá vào (`rent_lamports`) | `reclaim_rent`, `reclaim_max_per_loop` |
| 2 | Lọc token: freeze/mint authority, Token-2022 (phí chuyển > 0, transfer hook, permanent delegate, non-transferable, mặc định frozen, pausable); quote thử bán lại: không có route → `skipped_no_sell_route`, lỗ khứ hồi > 6% → `skipped_round_trip` | `token_safety`, `reject_*`, `max_round_trip_loss_pct` |
| 3 | Chống mua đuổi: giá mình > giá ví nguồn khớp (tính từ tx) quá 20% → `skipped_chase` | `max_entry_premium_pct` |
| 4 | Thoát khẩn cấp nâng bậc slippage/impact/phí ưu tiên | `exit_escalation`, `sell_slippage_bps`, `sell_max_price_impact_pct` |
| 5 | Giá batch qua Jupiter Price API v3 (≤50 token/request) | `price_batch`, `price_fallback_seconds` |
| 6 | Copy exit theo ví nguồn (alert `wallet_sell` của radar) | `copy_exit`, `copy_exit_min_sold_frac` |

- Bị từ chối trước khi gửi tx → signal đánh dấu `skipped_<lý do>`, KHÔNG
  retry, không chặn entry; đếm trong `live_state.json` → `entry_rejects`.
  Lỗi đọc RPC/Jupiter (không phải bằng chứng token xấu) → retry như cũ.
- Mỗi trade trong `live_trades.jsonl` có thêm: `signal_ts`,
  `wallet_price_usd`, `entry_premium_pct`, `round_trip_loss_pct`,
  `liquidity_usd`, `rent_lamports`, `exit_tier_used`.
- **Jupiter API key**: `lite-api.jup.ag` đang bị giảm rate và sẽ khai tử.
  Tạo key tại developers.jup.ag → `export JUPITER_API_KEY=...` (hoặc file
  `.jupiter_key`, chmod 600). Có key → bot tự dùng `https://api.jup.ag`.
  Không key mà đổi sang `api.jup.ag` → giới hạn 0.5 req/s, đặt
  `jupiter_min_interval_seconds: 2`.
- **Radar phải chạy bản mới** để có `wallet_sell`, `wallet_price_usd`,
  `liquidity_usd`. Radar cũ: copy exit và chống mua đuổi tự bỏ qua (không lỗi).
- **Dọn account rỗng tồn từ trước**: `python close_empty_accounts.py` (chỉ
  liệt kê) → kiểm tra → `python close_empty_accounts.py --yes`.
- **VPS**: `config.live.json` không bị ghi đè khi pull — các key mới lấy
  mặc định từ `DEFAULTS` trong code. Giá đã gom thành 1 request nên có thể
  hạ `price_poll_seconds` xuống 5 và `loop_seconds` xuống 3 (cần API key,
  hoặc giữ nguyên nếu chạy keyless).

**Paper radar (đề xuất Musev)** — `config.json` của radar:
`min_liquidity_mult` 20 (liquidity < size×20 → `skipped_low_liquidity`
trong `paper_entries.jsonl`), slippage `min(size/liq×50, 15)%` trừ vào giá
vào và ra (`final_ret_adj`), `min_exit_volume_mult` 5 (volume 5 phút <
leg×5 → `exit_constrained`, chờ tối đa `max_exit_waits` 3 vòng rồi thoát
với slippage 15% → `exit_failed_liquidity`). TP/SL vẫn kích hoạt theo giá
DexScreener thô (đồng bộ live). `python report.py --sensitivity` phát lại
lệnh cũ ở các mức thanh khoản giả định.

## Khác biệt so với paper (có chủ ý)

1. **Size flat $10/lệnh** thay vì tiers theo mcap ($50/$100) — đơn giản
   và an toàn cho giai đoạn test live. Đổi qua `trade_size_usd`.
2. **Giá quyết định từ Jupiter quote** (thay vì DexScreener cache 60s)
   — sát giá khớp thực hơn; DexScreener làm fallback.
3. **P&L thực đo bằng chênh lệch số dư SOL** trước/sau swap (ground truth
   ví), không phải giá quote. Báo cáo có thể lệch nhẹ so với paper do
   phí và trượt giá thật.
4. **Không có plan holder** trong module này — chỉ mirror plan scalp.
   Tín hiệu từ ví holder vẫn mở vị thế theo luật scalp (ghi rõ wallet).
5. Không snapshot P&L ở +5/+15/+60/+240p như paper (chỉ phục vụ báo cáo).

## File liên quan

- `live_trader.py` — module chính
- `config.live.example.json` → copy thành `config.live.json`
- `test_live_trader.py`, `test_live_upgrade.py` — test suite offline (mock)
- `close_empty_accounts.py` — dọn token account rỗng (mặc định chỉ liệt kê)
- `feasibility.py`, `test_feasibility.py` — kiểm tra khả thi paper
- `live_positions.json`, `live_state.json`, `live_trades.jsonl`,
  `live_trader.log` — runtime state (tự tạo, không commit)

## Doi chieu P&L voi vi (reconcile_wallet.py)

Dashboard hien `realized_usd` bot ghi = (SOL nhan khi ban − SOL chi khi mua) × gia
SOL, **da tru phi mang** (base + priority) cua ca tx mua va ban; rent token account
tach rieng. App vi (Phantom/GMGN/...) thuong tinh **truoc phi mang** va co the
tinh rent/gia SOL khac -> lech vai cent moi lenh la binh thuong.

Doi chieu tung lenh voi tx that (chi doc, khong can private key):

```bash
.venv/bin/python meme-radar/reconcile_wallet.py              # 24h
.venv/bin/python meme-radar/reconcile_wallet.py --hours 48 --scan --detail
```

- `Bot` = dashboard, `Chain` = tinh lai tu tx that (cung cach bot), `TruocPhi` =
  Chain + phi mang (gan voi so app vi). `Bot` ≠ `Chain` -> co `BOT_LECH_CHAIN`.
- `--scan`: tx cua vi ma bot khong ghi (tx loi van mat phi, thu hoi rent, mua/ban
  tay...).

Tu 10/2026 bot ghi P&L tu **chinh tx** (`getTransaction`: so du truoc/sau + phi)
thay vi `getBalance` sau confirm (node RPC lag co the tra so du cu -> tien ban ve
ghi $0 / chi phi mua sai). Khong doc duoc tx sau 3 lan -> dung so du nhu cu (log
"dung so du"). Moi lenh/leg co `fee_usd` (phi mang) va `acct` (tx|balance); ban ghi
dong lenh co `fee_usd` + `fee_known` -> dashboard hien cot "Phi mang" va "Truoc phi".
Tat: `"tx_accounting": false` trong config.live.json.

## Equity ví live (live_equity_snap.py, app pm2 muse-live-equity)

Mỗi 15s ghi `equity_snapshots(system='radar_live')` = SOL trong ví × giá SOL + token
của vị thế đang mở (giá Jupiter; thiếu giá → giá vào). Tab Live Radar vẽ "Equity ví
6h qua" giống tab Binance LIVE. Chỉ đọc (Helius getBalance + Jupiter price), không
cần private key. `git up` tự start app này lần đầu; log:
`pm2 logs muse-live-equity`.
