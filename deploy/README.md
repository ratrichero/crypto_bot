# Deploy tự động + quản lý app bằng pm2

```bash
git up              # kéo code mới → cài thư viện nếu cần → build nếu cần → restart app có thay đổi
git up status       # app nào đang chạy commit nào, có cần restart không
git up --dry-run    # chỉ xem sẽ làm gì (fetch nhưng không pull/cài/restart)
git up --branch X   # đổi sang deploy nhánh X, lần sau git up tự theo X
git up --force      # làm lại mọi bước (pip, migrate, build) + restart mọi app
git up doctor       # kiểm tra môi trường, chỉ đọc
git up setup        # lần đầu: alias, pm2-logrotate, chuyển app từ systemd sang pm2
./deploy.sh ...     # tương đương git up ...
```

**Không hỏi y/N**: mọi bước tự chạy, kể cả restart app tiền thật (Binance, Live
Trader), đổi nhánh và `setup`. Chạy từ cron cũng vậy. `--yes` vẫn được chấp nhận
(cho lệnh cũ) nhưng không còn tác dụng. Muốn xem trước thì dùng `--dry-run`.

Tuỳ chọn khác: `--only muse-dashboard,muse-radar`, `--no-restart`,
`--restart --only <app>` (ép restart), `--force-deps` (chỉ chạy lại pip install).

## `git up` làm gì

| Bước | Kiểm tra | Bỏ qua khi |
|---|---|---|
| 1. Kéo code | `git fetch` rồi chỉ **fast-forward** nhánh đang checkout | không có commit mới |
| 2. Thư viện | băm file requirements của từng python/venv, so với lần cài trước; `pip install -r` rồi so `pip freeze` trước/sau | requirements không đổi |
| 3. Migrate DB | `db/schema.sql` (idempotent) đổi, hoặc DB đổi → chạy trong **1 transaction** (lock_timeout 15s, statement_timeout 300s) | file và DB không đổi |
| 4. Build web | thư mục có `package.json` đổi → `npm ci` + `npm run build` | không có (dashboard Streamlit chạy thẳng `app.py`) |
| 5. Restart | app đang chạy commit nào (giờ start trong pm2 đối chiếu `git reflog`) → diff tới HEAD ∩ các file Python app import (tính bằng AST); **hoặc** thư viện/build của app đổi **sau** giờ app start | các thay đổi không đụng tới app |
| 6. Kiểm tra | cú pháp Python trước khi restart; sau restart theo dõi 15–45s: online, không crash | — |
| 7. Lưu | `pm2 save`, ghi `.deploy/history.log`, in dòng tổng kết | — |

Lý do restart in ra cụ thể, ví dụ `CAN RESTART - code doi: binance-bot/live_binance.py`.
Lỗi "dashboard đang chạy code cũ" do pull tay mà quên restart cũng được phát hiện:
lần `git up` sau thấy app chạy commit cũ hơn HEAD nên restart nó.

Tương tự với thư viện: mỗi lần `pip freeze` đổi, mốc thời gian được ghi vào
`.deploy/changed_at.json`. App nào start **trước** mốc đó thì vẫn bị đánh dấu cần
restart (`thu vien Python doi luc 07/10 14:05, sau khi app start`), kể cả khi
lần trước restart bị bỏ (`--no-restart`, `--only`, lỗi cú pháp). Lần `git up`
sau sẽ restart nó.

Dòng cuối luôn tóm tắt kết quả:

```
OK xong trong 12s - code:6a25585 thu-vien:1 migrate:applied build:0 restart:2/2
XX xong trong 4s - code:- thu-vien:0 migrate:error build:0 restart:0/0 (CO LOI)
```

### Migrate DB

Cấu hình trong `deploy.env`: `MIGRATE_SQL=db/schema.sql`, `MIGRATE_DB_ENV=DATABASE_URL`
(tên biến chứa URL), `MIGRATE_ENV_FILE` (để trống = `ENV_FILE`), `MIGRATE_PYTHON`
(để trống = `PYTHON`, cần có `psycopg`). URL lấy từ môi trường hoặc file env,
được truyền qua biến môi trường chứ không qua tham số dòng lệnh. Nếu thông báo lỗi
có chứa URL thì URL được thay bằng `***`.

- Lỗi → rollback toàn bộ, **không restart app nào** (code mới có thể cần bảng
  mới), mã thoát 1. Sửa SQL rồi chạy lại `git up`; migrate chạy lại vì dấu chưa ghi.
- Không dùng DB: đặt `MIGRATE_SQL=` (rỗng) trong `deploy/deploy.env`.
- Thêm bảng mới: viết vào `db/schema.sql` theo kiểu idempotent (`CREATE TABLE IF
  NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).

### Đổi nhánh deploy: `git up --branch X`

```bash
git up --branch main            # đổi luôn, không hỏi
git up --branch main --dry-run  # chỉ xem chênh bao nhiêu commit
```

- Nhánh phải có trên remote và **đã có `deploy/deploy.py`**. Nếu không có,
  lệnh từ chối, vì chuyển sang sẽ mất `git up` và cấu hình pm2. Hiện `main`
  chưa có `deploy/`, phải merge trước.
- Working tree phải sạch. Lệnh chỉ `checkout` + fast-forward. Nhánh local lệch
  với remote (diverged) thì dừng. Commit chưa push của nhánh cũ vẫn nằm trên nhánh cũ.
- Cho xem `A (sha) -> B (sha): +N commit chỉ có ở nhánh mới, -M commit chỉ có ở
  nhánh cũ`. -M > 0 nghĩa là code sẽ **mất** các commit đó (có thể là lùi bản).
- Sau khi checkout, chạy bình thường: cài thư viện, migrate, chỉ restart app có
  file khác nhau giữa hai nhánh. Script deploy khác nhau thì tự chạy lại bằng bản
  của nhánh mới. Không ghi file cấu hình nào: `git up` luôn deploy **nhánh đang
  checkout**, nên các lần sau tự theo X.
- Lần đầu: VPS đang chạy `deploy.py` cũ chưa có `--branch` → chạy `git up` một lần
  để lấy bản mới.

### `--force`

Bỏ qua mọi dấu "đã làm": chạy lại `pip install`, migrate, build, và restart
**mọi** app đang chạy (lý do `--force/--restart`), kể cả app tiền thật. App
đang `stopped` vẫn không tự start. Dùng khi nghi ngờ trạng thái
`.deploy/` sai, hoặc sau khi sửa tay môi trường.

**An toàn:**
- Working tree có file đã sửa → dừng. Nếu local có commit chưa push hoặc lệch
  nhánh → dừng, không bao giờ merge/rebase/reset.
- App tiền thật (`live: true` trong `apps.json`) được restart tự động như app khác,
  không hỏi y/N. Trước khi restart, log có nhãn `[tien that]` và tóm tắt state
  của bot (số lot đang mở, có đang halt không, nếu đọc được).
- Có lỗi cú pháp Python → không restart, bot cũ vẫn chạy.
- App đang `stopped` (do bị `pm2 stop`, do file `STOP`, hoặc do safety
  circuit) → **không tự start**.
- Nếu chính script deploy có trong commit mới, `git up` tự chạy lại bằng bản mới
  sau khi pull.

## pm2

Tên app giữ nguyên tên service systemd cũ: `muse-dashboard`, `muse-radar`,
`muse-live-trader`, `muse-binance`.

- Dừng sạch: pm2 gửi SIGINT. Bot làm nốt vòng lặp hoặc swap đang chạy, lưu
  state rồi thoát. `kill_timeout` là 60s cho Binance, 180s cho Live Trader.
  Gửi SIGINT lần nữa thì bot dừng ngay.
- Thoát mã 0 (file `STOP`, safety circuit 429/418, khởi động lỗi) hoặc mã 78
  (thiếu python/.env) → pm2 **không restart**. Nếu không chặn, pm2 sẽ khởi động
  lại liên tục và spam API Binance. Crash thật (mã khác) → restart với thời
  gian chờ tăng dần, tối đa 15s.
- Secret: `deploy/run-app.sh` nạp `.env` lúc start (giống `EnvironmentFile`
  của systemd). Secret **không** nằm trong `~/.pm2/dump.pm2`; sửa `.env` rồi
  restart là app nhận giá trị mới.
- Log: `pm2 logs muse-binance --lines 100`, file ở `~/.pm2/logs/`, xoay vòng
  bằng pm2-logrotate (20M × 10). Bot vẫn ghi `bot.log` như cũ.
- Tab Monitor của dashboard tự nhận biết pm2 hay systemd (ghi đè bằng env
  `PROCESS_MANAGER=pm2|systemd`; đường dẫn pm2 qua env `PM2_BIN`).

Lệnh pm2 hay dùng: `pm2 ls`, `pm2 logs <app>`, `pm2 stop <app>`, `pm2 start <app>`.
Muốn restart, ưu tiên `git up --restart --only <app>` (có kiểm tra cú pháp
và health check).

## Cấu hình

Chỉ có **một** file cấu hình là `deploy/deploy.env`, được commit trong repo.
Muốn đổi thì sửa file này rồi push. Không còn `deploy.local.env`: nếu VPS còn
file đó thì `git up` bỏ qua và in nhắc xoá. Các giá trị chính:

```bash
PYTHON=.venv/bin/python                  # riêng 1 app: PYTHON_MUSE_BINANCE=...
ENV_FILE=.env                            # riêng 1 app: ENV_FILE_MUSE_LIVE_TRADER=meme-radar/.env
DASHBOARD_PORT=8501
DASHBOARD_ARGS=--server.baseUrlPath x    # nếu unit systemd cũ có thêm tham số
APPS=muse-dashboard muse-radar muse-live-trader muse-binance
```

Định nghĩa app (thư mục, entry, thư mục import, `kill_timeout`, có phải app tiền thật
không) nằm trong `deploy/apps.json`. `deploy.py` và `ecosystem.config.js` cùng đọc file này.

## Chuyển từ systemd sang pm2 (làm 1 lần trên VPS)

```bash
cd /home/ubuntu/muse_bot
git pull --ff-only                      # lấy deploy/ lần đầu
node -v && pm2 -v || sudo npm install -g pm2    # cần nodejs >= 16
python3 deploy/deploy.py doctor
```

Đọc kết quả `doctor`:
- **ExecStart** của từng unit: python, tham số và port dashboard phải khớp với
  cấu hình trong `deploy/deploy.env`. Nếu khác, sửa `deploy.env` trong repo rồi push.
  Python khác nhau → `XX ... sua PYTHON_<APP>` (chặn `setup`). Hiện tại `meme-radar`
  dùng `.venv` riêng (`PYTHON_MUSE_RADAR`, `PYTHON_MUSE_LIVE_TRADER`).
- **EnvironmentFile** của unit phải trùng với `ENV_FILE` mà pm2 sẽ nạp. Nếu khác
  → `XX ... sua ENV_FILE_<APP>`. Unit không cấp biến nào mà pm2 lại nạp file →
  app tiền thật bị chặn, vì biến trong file sẽ đè lên `.env` riêng mà bot tự đọc
  (ví dụ `SOLANA_PRIVATE_KEY`). Đặt `ENV_FILE_<APP>=-` nếu muốn không nạp file nào.
- **systemd DANG LOI** (activating/auto-restart): bot hiện không chạy. Xem
  `journalctl -u <app> -n 50` trước khi chuyển.
- **Env (tên)**: các biến unit systemd đang cấp. Biến nào chưa có trong
  `ENV_FILE` thì `doctor`/`setup` báo `XX` và **không chuyển** app đó. Thêm biến
  vào `.env` (chmod 600) trước khi chuyển.
- **process ngoài pm2 / crontab**: watchdog cũ phải xoá, tránh chạy trùng.

```bash
python3 deploy/deploy.py setup
```

Lần lượt cho từng app (dashboard → radar → live trader → binance), **không hỏi**:
`sudo systemctl disable --now muse-x`, kiểm tra không còn process cũ,
`pm2 start`, rồi health check. Mỗi app ngừng vài giây. Cuối cùng chạy `pm2 save`,
`pm2 startup` (tự chạy lại sau reboot) và cài alias `git up`.

**Sau khi chuyển: KHÔNG dùng `systemctl start/restart muse-*` nữa.** Lệnh đó
bật lại bản systemd chạy song song với pm2. Binance có lock, nhưng Live Trader
không có lock nên có thể mua trùng.

Quay lại systemd: `pm2 delete muse-x && pm2 save && sudo systemctl enable --now muse-x`.

## Ghi chú

- Không chạy test của bot trên VPS: test ghi đè `runtime_config.cache.json`
  và các file runtime khác. Riêng `python3 deploy/test_deploy.py` chạy được,
  vì chỉ dùng repo tạm.
- Alias `git up` lưu trong `.git/config` của repo này (`!exec python3 deploy/deploy.py`).
- Trạng thái deploy nằm ở `.deploy/` (có trong .gitignore): lock, dấu pip/migrate/build,
  `changed_at.json` (mốc đổi thư viện/build), hash cấu hình pm2 của từng app, `history.log`.
