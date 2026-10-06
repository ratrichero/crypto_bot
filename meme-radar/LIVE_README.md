# Live Trader — "đàn quạ" bản tiền thật

Module `live_trader.py` biến tín hiệu của radar (`signals.jsonl`) thành lệnh
swap THẬT trên Solana qua Jupiter. Chạy ĐỘC LẬP với `radar.py` (paper) —
không sửa, không restart radar.

## Kiến trúc

```
radar.py (paper, chay san 24/7)
   | ghi signals.jsonl (buy) + alerts.jsonl (sell_cluster)
   v
live_trader.py -- tail file, khong import radar
   |--- BUY:  Jupiter quote SOL->token -> swap -> sign (solders) -> gui qua Helius RPC
   |--- SELL: Jupiter quote token->SOL -> swap -> sign -> gui (tung leg)
   |--- Gia:  Jupiter quote token->USDC (fallback DexScreener)
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
| hết 480 phút, lãi ≥20% | chốt 1/2, giữ 1/2 trailing |
| hết 480 phút, lãi <20% | bán hết |

Thứ tự ưu tiên khi nhiều điều kiện cùng đúng trong 1 poll:
trailing → SL → smart exit → time stop (giống paper).

## Mô hình an toàn

- **Mặc định `dry_run`**: chỉ log ý định swap, không gửi transaction,
  không cần key. Dùng để kiểm tra logic trước.
- **`live` fail-closed**: thiếu file `.solana_key`, key sai định dạng,
  hoặc pubkey không khớp `wallet_address` → chương trình TỰ DỪNG.
- **Kill switch**: tạo file `STOP` trong thư mục này → vòng lặp dừng
  nhẹ nhàng. ⚠️ **STOP không tự đóng vị thế đang mở** — phải xử lý tay.
- **Daily stop**: ngừng MỞ MỚI khi lỗ thực tế trong ngày (UTC) <
  `-daily_stop_pct` × portfolio đầu ngày. Vị thế cũ vẫn được quản lý exit.
- **Fee buffer**: luôn giữ tối thiểu `fee_buffer_sol` SOL cho phí.
- **Price impact guard**: bỏ qua lệnh nếu impact vượt `max_price_impact_pct`.
- **Slippage**: giới hạn bởi `slippage_bps` ngay trong quote Jupiter
  (on-chain guard `otherAmountThreshold`).
- **Retry**: quote/swap thử lại tối đa `max_swap_retries` lần với
  blockhash mới. Lệnh bán luôn tính theo SỐ DƯ THỰC trên ví nên không
  bao giờ bán trùng 2 lần.
- Private key KHÔNG BAO GIỜ được log.

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
- [ ] Hiểu rõ: STOP không đóng vị thế; smart contract/pool rủi ro là
      của mình; meme có thể về 0 trong vài phút
- [ ] Đổi `"mode": "live"` trong `config.live.json` (cần restart)

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
- `test_live_trader.py` — test suite offline (mock)
- `live_positions.json`, `live_state.json`, `live_trades.jsonl`,
  `live_trader.log` — runtime state (tự tạo, không commit)
