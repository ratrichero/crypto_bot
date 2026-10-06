#!/bin/bash
# Sync bot state files (open positions) from this machine to the VPS dashboard.
# Runs every 2 min via cron. No secrets inside - key lives in ~/.ssh.
set -u
P="${http_proxy:-$HTTP_PROXY}"; P="${P#http://}"; P="${P#https://}"
U="${P%%:*}"; R="${P#*:}"; PW="${R%%@*}"; H="${R##*@}"
SCP="scp -i /home/hatch/.ssh/cuong_vps -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o ProxyCommand=\"nc -X connect -x $H -P $U:$PW %h %p\""
eval $SCP /home/hatch/workspace/trading-bot/state.json \
  ubuntu@13.229.182.177:/home/ubuntu/muse_bot/dashboard/state/okx_state.json || echo "FAIL okx_state"
# radar_state.json: radar paper chay tren VPS, dashboard doc truc tiep, khong day
