#!/bin/bash
# Deploy script tu dong tu repo - chay tren VPS
# Su dung: ./deploy.sh [branch]
# Mac dinh: arena/1b7a22f9-crypto-bot

set -e

BRANCH="${1:-arena/1b7a22f9-crypto-bot}"
REPO_DIR="/home/ubuntu/muse_bot"
cd "$REPO_DIR" || exit 1

echo "=== Deploy branch: $BRANCH ==="

# 1. Kiem tra va keo code moi
echo "[1/4] Kiem tra code moi..."
git fetch origin "$BRANCH" 2>&1 | tail -1
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "  -> Khong co code moi, bo qua pull"
    NEED_RESTART=false
else
    echo "  -> Co code moi, dang pull..."
    git checkout "$BRANCH" 2>&1 | tail -1
    git pull origin "$BRANCH" 2>&1 | tail -2
    NEED_RESTART=true
fi

# 2. Kiem tra thu vien Python
echo "[2/4] Kiem tra thu vien..."
VENV="/home/ubuntu/muse_bot/.venv"
# So sanh requirements (neu co file)
if [ -f "$REPO_DIR/binance-bot/requirements.txt" ]; then
    # Kiem tra nhanh: thu import cac module chinh
    MISSING=""
    for mod in ccxt psycopg streamlit; do
        if ! "$VENV/bin/python" -c "import $mod" 2>/dev/null; then
            MISSING="$MISSING $mod"
        fi
    done
    if [ -n "$MISSING" ]; then
        echo "  -> Cai them:$MISSING"
        "$VENV/bin/pip" install -q $MISSING
    else
        echo "  -> Du thu vien, bo qua"
    fi
else
    echo "  -> Khong co requirements.txt, bo qua"
fi

# 3. Kiem tra build web (dashboard khong can build)
echo "[3/4] Kiem tra build..."
echo "  -> Dashboard Streamlit khong can build, bo qua"

# 4. Restart app neu co code moi
echo "[4/4] Restart services..."
if [ "$NEED_RESTART" = true ]; then
    echo "  -> Restart muse-binance, muse-live-trader, muse-dashboard..."
    sudo systemctl restart muse-binance muse-live-trader muse-dashboard
    sleep 5
    for svc in muse-binance muse-live-trader muse-dashboard; do
        STATUS=$(systemctl is-active "$svc" 2>&1)
        echo "  -> $svc: $STATUS"
    done
else
    echo "  -> Khong co thay doi, khong restart"
fi

echo "=== Deploy xong ==="
