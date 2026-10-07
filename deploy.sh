#!/usr/bin/env bash
# Giu tuong thich: ./deploy.sh [lenh] [tuy chon]  ==  git up [lenh] [tuy chon]
# Logic nam o deploy/deploy.py (pm2). Xem deploy/README.md.
#
# Ban cu goi "sudo systemctl restart muse-*": sau khi chuyen sang pm2 lenh do
# se BAT LAI service systemd da tat -> bot chay trung 2 ban (live_trader khong
# co lock -> co the mua trung). Vi vay khong con goi systemctl o day.
ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$ROOT" || exit 1
exec python3 "$ROOT/deploy/deploy.py" "$@"
