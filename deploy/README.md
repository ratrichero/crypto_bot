# Deploy tự động + quản lý app bằng pm2

```bash
git up              # kéo code mới → cài thư viện nếu cần → build nếu cần → restart app có thay đổi
git up status       # app nào đang chạy commit nào, có cần restart không
git up --dry-run    # chỉ xem sẽ làm gì (fetch nhưng không pull/cài/restart)
git up --yes        # đồng ý luôn restart app tiền thật (Binance, Live Trader)
git up doctor       # kiểm tra môi trường, chỉ đọc
git up setup        # lần đầu: alias, pm2-logrotate, chuyển app từ systemd sang pm2
./deploy.sh ...     # tương đương git up ...
```

Tuỳ chọn khác: `--only muse-dashboard,muse-radar`, `--no-restart`,
`--restart --only <app>` (ép restart), `--force-deps` (chạy lại pip install).

## `git up` làm gì

| Bước | Kiểm tra | Bỏ qua khi |
|---|---|---|
| 1. Kéo code | `git fetch` rồi chỉ **fast-forward** nhánh đang checkout (`DEPLOY_BRANCH`) | không có commit mới |
| 2. Thư viện | băm file requirements của từng python/venv, so với lần cài trước; `pip install -r` rồi so `pip freeze` trước/sau | requirements không đổi |
| 3. Build web | thư mục có `package.json` đổi → `npm ci` + `npm run build` | không có (dashboard Streamlit chạy thẳng `app.py`) |
| 4. Restart | app đang chạy commit nào (giờ start trong pm2 đối chiếu `git reflog`) → diff tới HEAD ∩ các file Python app import (tính bằng AST) | các thay đổi không đụng tới app |
| 5. Kiểm tra | cú pháp Python trước khi restart; sau restart theo dõi 15–45s: online, không crash | — |
| 6. Lưu | `pm2 save`, ghi `.deploy/history.log` | — |

Lý do restart in ra cụ thể, ví dụ `CAN RESTART - code doi: binance-bot/live_binance.py`.
Lỗi "dashboard đang chạy code cũ" do pull tay mà quên restart cũng được phát hiện:
lần `git up` sau thấy app chạy commit cũ hơn HEAD nên restart nó.

**An toàn:**
- Working tree có file đã sửa → dừng. Nếu local có commit chưa push hoặc lệch
  nhánh → dừng, không bao giờ merge/rebase/reset.
- App tiền thật (`confirm: true` trong `apps.json`) phải trả lời y/N, hoặc
  chạy `git up --yes`. Lý do: luật repo yêu cầu lên live Binance cần Cường duyệt.
  Chạy không có terminal (cron) và không có `--yes` thì bỏ qua và in lệnh để chạy lại.
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

`deploy/deploy.env` chứa giá trị mặc định và được commit. Trên VPS, ghi đè bằng
`deploy/deploy.local.env` (không commit), cùng cú pháp:

```bash
DEPLOY_BRANCH=arena/1b7a22f9-crypto-bot
PYTHON=.venv/bin/python                  # riêng 1 app: PYTHON_MUSE_BINANCE=...
ENV_FILE=.env                            # riêng 1 app: ENV_FILE_MUSE_LIVE_TRADER=meme-radar/.env
DASHBOARD_PORT=8501
DASHBOARD_ARGS=--server.baseUrlPath x    # nếu unit systemd cũ có thêm tham số
APPS=muse-dashboard muse-radar muse-live-trader muse-binance
```

Định nghĩa app (thư mục, entry, thư mục import, `kill_timeout`, có cần xác nhận
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
  cấu hình. Nếu khác, ghi vào `deploy.local.env`.
- **Env (tên)**: các biến unit systemd đang cấp. Biến nào chưa có trong
  `ENV_FILE` thì `doctor`/`setup` báo `XX` và **không chuyển** app đó. Thêm biến
  vào `.env` (chmod 600) trước khi chuyển.
- **process ngoài pm2 / crontab**: watchdog cũ phải xoá, tránh chạy trùng.

```bash
python3 deploy/deploy.py setup
```

Lần lượt cho từng app (dashboard → radar → live trader → binance), sau khi bạn
trả lời y: `sudo systemctl disable --now muse-x`, kiểm tra không còn process cũ,
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
- Trạng thái deploy nằm ở `.deploy/` (có trong .gitignore): lock, mốc cài pip, hash
  cấu hình pm2 của từng app, `history.log`.
