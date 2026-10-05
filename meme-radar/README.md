# meme-radar — Solana smart-money copy radar (paper)

Theo dõi ví smart money trên Solana qua Helius (free tier), phát hiện lệnh mua
real-time qua websocket `logsSubscribe` (+ poll RPC 15s fallback), paper-track
chiến thuật copy.

## Luồng chạy

```
radar.py → ws logsSubscribe từng ví + poll getSignaturesForAddress (15s)
  → ingest tín hiệu buy (≥$300 và ≥0.3 SOL) → signals.jsonl
  → cluster (≥2 ví mua cùng token / 30p) + whale (≥$2.000) → alerts.jsonl
  → paper copy theo mcap (<$50k → $50, ≥$50k → $100) → exit engine
```

## Hai plan paper

**Scalp** (`paper_trades.jsonl`): TP thang (+50% chốt 1/3, +100% chốt thêm 1/3,
phần còn lại trailing -30%), SL -25%, smart exit khi ≥2 ví xả cùng token/30p,
time stop 480 phút — hết giờ mà lãi ≥20% thì chốt 1/2, giữ 1/2 đu sóng.

**Holder** (`paper_trades_holder.jsonl`): cho ví style holder (ôm lâu, trung bình giá).
SL -50%, trailing -40% (kích hoạt khi đỉnh lãi ≥20%), time stop 24h,
scale-in +50% khi ví mua thêm (tối đa 2 lần), ví gốc xả thì chốt 1/2 theo.
Danh sách ví holder trong `config.json → holder.wallets`.

## Mở rộng đàn ví

- `discover_cobuyers.py`: từ token thắng đậm của ví tốt → tìm ví mua cùng trước đó.
- `screen_new_wallets2.py`: sàng lọc ứng viên (loại bot spam: >2000 tx mà success <10%;
  chấm FIFO: realized >+2 SOL, winrate ≥50%, ≥10 vòng).
- Kết quả `ACCEPT` → thêm vào `wallets.json`, radar tự subscribe lại.

## File chính

| File | Vai trò |
|---|---|
| `radar.py` | Vòng lặp chính: ingest, cluster, paper, exit engine |
| `sources/helius.py` / `sources/ws_feed.py` | Đọc tx qua RPC / websocket real-time |
| `report.py` | Báo cáo paper + xếp hạng ví (có mục HOLDER) |

## Config

Key Helius để trong `.helius_key` (chmod 600) hoặc env `HELIUS_API_KEY` — không commit.
Watchdog cron 5 phút giữ tiến trình sống; kill switch: file `STOP`.
