# Crypto Trading Bots

Hệ thống bot giao dịch crypto chạy 24/7: paper-trade trước, tiền thật sau.
Code Python, dữ liệu vận hành trên PostgreSQL, dashboard Streamlit.

## Thành phần

| Thư mục | Mô tả |
|---|---|
| `trading-bot/` | Bot paper-trade futures trên OKX: scalp 5m/15m + grid 2 chiều, top 30 coin vốn hóa |
| `meme-radar/` | Radar copy-trade meme Solana: theo dõi ví smart money qua Helius, tín hiệu real-time qua websocket |
| `dashboard/` | Dashboard Streamlit: KPIs, equity curve, P&L theo ngày, vị thế mở, xếp hạng ví |
| `db/` | Schema PostgreSQL, script migrate JSONL → Postgres, daemon đồng bộ |

## Kiến trúc dữ liệu

```
bot / radar  →  JSONL append-only (audit)  →  sync daemon  →  PostgreSQL  →  dashboard
```

- JSONL giữ vai trò audit log không sửa được (mỗi dòng 1 lệnh, append-only).
- PostgreSQL là nguồn đọc cho dashboard/báo cáo, có unique constraint chống ghi trùng.
- Daemon `db/sync_jsonl.py` tail JSONL và upsert vào Postgres mỗi phút.

## Chiến thuật (tóm tắt)

**trading-bot** — mỗi lệnh 100 USDT margin ×10 đòn bẩy, tối đa 10 vị thế, dừng ngày -20%:
- *Scalp*: breakout nến 5m/15m, lọc RSI + ADX trend, TP 1.0% / SL 0.4%.
- *Grid 2 chiều*: chỉ chạy khi ADX(14) < 22 (đi ngang); lưới 5 tầng mỗi bên, step 0.5%
  (adaptive theo ATR 0.4%–0.8%); TP 1 step, không SL từng lệnh; rebuild khi giá lệch anchor 3%.
- Optimizer mỗi sáng tự chỉnh tham số trong biên an toàn (không động đòn bẩy/size).

**meme-radar** — copy ví smart money Solana (paper):
- Plan *scalp*: tín hiệu buy ≥$300, size theo mcap ($50/$100); TP thang +50%/+100%,
  trailing -30%, SL -25%, smart exit khi ≥2 ví xả, time stop 480 phút (lãi ≥20% giữ 1/2 đu sóng).
- Plan *holder* (ví holder như Cooker.hl): SL -50%, trailing -40%, time stop 24h,
  scale-in khi ví mua thêm, ví xả thì chốt 1/2 theo.

Chi tiết xem README trong từng thư mục.

## Chạy thử

```bash
cp trading-bot/config.example.json trading-bot/config.json   # điền key nếu cần
cp meme-radar/config.example.json meme-radar/config.json
# DB
createdb cryptobots && psql cryptobots < db/schema.sql
python3 db/migrate_jsonl.py --trading-bot ~/workspace/trading-bot --meme-radar ~/workspace/meme-radar
# Dashboard
pip install -r dashboard/requirements.txt
DATABASE_URL=postgresql://user:pass@localhost/cryptobots streamlit run dashboard/app.py
```

> Không commit file `.key`, `*_state.json`, `*.jsonl`, `*.log` — đã có trong `.gitignore`.
> Đây là hệ thống thử nghiệm, không phải lời khuyên đầu tư.
