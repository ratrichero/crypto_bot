# db/ — PostgreSQL layer cho crypto-bots

Luu tru lich su lenh cua bot OKX va radar meme vao PostgreSQL de dashboard
va phan tich query nhanh. Bot goc van chay JSONL nhu cu — day chi la ban
sao phuc vu doc/hien thi, **khong anh huong bot dang chay**.

## Cai dat

```bash
sudo apt install postgresql
sudo -u postgres psql -c "CREATE DATABASE cryptobots;"
sudo -u postgres psql -c "CREATE USER muse WITH PASSWORD '...'; GRANT ALL ON DATABASE cryptobots TO muse;"
psql "$DATABASE_URL" -f schema.sql
pip install "psycopg[binary]"
```

`DATABASE_URL` vi du:
`postgres://muse:...@localhost:5432/cryptobots`

## Import lan dau

```bash
DATABASE_URL=... python3 migrate_jsonl.py \
  --trading-bot ~/workspace/trading-bot \
  --meme-radar  ~/workspace/meme-radar
```
Idempotent (ON CONFLICT DO NOTHING) — chay lai an toan.

## Dong bo lien tuc

```bash
DATABASE_URL=... nohup python3 sync_jsonl.py \
  --trading-bot ~/workspace/trading-bot \
  --meme-radar  ~/workspace/meme-radar > sync.log 2>&1 &
```
Moi 60s doc cac dong JSONL moi (theo byte-offset luu o
`~/.cryptobots_sync_state.json`) va upsert. Chi doc, khong sua file goc.

## Bang

- `okx_trades` — lenh dong cua bot OKX (fee = 0.05% x 2 x notional)
- `radar_trades` — lenh paper radar, `plan` = scalp | holder
- `wallets` — danh sach vi theo doi
- `equity_snapshots` — (du tru, chua dung)
