#!/bin/bash
# Day JSONL lich su lenh (paper OKX + radar) tu may nay len VPS de sync vao Neon.
# Chay moi 2 phut qua cron. Khong co secret trong script.
set -u
P="${http_proxy:-$HTTP_PROXY}"; P="${P#http://}"; P="${P#https://}"
U="${P%%:*}"; R="${P#*:}"; PW="${R%%@*}"; H="${R##*@}"
HOST="ubuntu@13.229.182.177"
DST="/home/ubuntu/muse_bot/paper-jsonl"

push_one() {  # $1 = local, $2 = remote
  scp -i /home/hatch/.ssh/cuong_vps -o BatchMode=yes -o StrictHostKeyChecking=no \
      -o ConnectTimeout=20 \
      -o ProxyCommand="nc -X connect -x $H -P $U:$PW %h %p" \
      "$1" "$HOST:$2" || echo "FAIL $1"
}

push_one /home/hatch/workspace/trading-bot/trades.jsonl              "$DST/trading-bot/trades.jsonl"
# /home/hatch/workspace/meme-radar/paper_trades.jsonl: doc truc tiep tren VPS, khong day
# /home/hatch/workspace/meme-radar/paper_trades_holder.jsonl: doc truc tiep tren VPS, khong day
# /home/hatch/workspace/meme-radar/wallets.json: doc truc tiep tren VPS, khong day
