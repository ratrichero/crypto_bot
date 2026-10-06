#!/bin/bash
# Day JSONL lich su lenh paper OKX tu may nay len VPS de sync vao Neon.
# (Radar da chuyen sang chay tren VPS — file radar doc truc tiep, khong day nua.)
# Chay moi 2 phut qua cron. Khong co secret trong script.
set -u
P="${http_proxy:-$HTTP_PROXY}"; P="${P#http://}"; P="${P#https://}"
U="${P%%:*}"; R="${P#*:}"; PW="${R%%@*}"; H="${R##*@}"
HOST="ubuntu@13.229.182.177"
DST="/home/ubuntu/muse_bot/paper-jsonl"

scp -i /home/hatch/.ssh/cuong_vps -o BatchMode=yes -o StrictHostKeyChecking=no \
    -o ConnectTimeout=20 \
    -o ProxyCommand="nc -X connect -x $H -P $U:$PW %h %p" \
    /home/hatch/workspace/trading-bot/trades.jsonl \
    "$HOST:$DST/trading-bot/trades.jsonl" || echo "FAIL okx trades.jsonl"
