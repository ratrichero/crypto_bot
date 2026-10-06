#!/bin/bash
# Forward tin hieu radar paper (may local) -> VPS cho live_trader doc.
# Chi gui cac byte MOI tu lan truoc (append-only) de live_trader tail theo
# byte-offset khong bi lech. Chay moi 30s qua cron.
set -u
P="${http_proxy:-$HTTP_PROXY}"; P="${P#http://}"; P="${P#https://}"
U="${P%%:*}"; R="${P#*:}"; PW="${R%%@*}"; H="${R##*@}"
SSH="ssh -i /home/hatch/.ssh/cuong_vps -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ProxyCommand=\"nc -X connect -x $H -P $U:$PW %h %p\" ubuntu@13.229.182.177"
DST_DIR="/home/ubuntu/muse_bot/live-signals"
STATE_F="$HOME/.radar_signal_forward.state"
SIG=/home/hatch/workspace/meme-radar/signals.jsonl
ALERT=/home/hatch/workspace/meme-radar/alerts.jsonl

sig_off=0; alert_off=0
if [ -f "$STATE_F" ]; then
  sig_off=$(sed -n '1p' "$STATE_F" | grep -E '^[0-9]+$' || echo 0)
  alert_off=$(sed -n '2p' "$STATE_F" | grep -E '^[0-9]+$' || echo 0)
fi

forward() {  # $1 = local file, $2 = offset var name, $3 = remote name
  local f="$1" off="$2" rname="$3"
  [ -f "$f" ] || { echo "0"; return; }
  local size
  size=$(stat -c%s "$f")
  [ "$off" -gt "$size" ] && off=0        # file bi rotate -> gui lai tu dau
  [ "$off" -eq 0 ] && [ ! -f "$STATE_F" ] && off=$size  # lan dau: bo qua lich su cu
  if [ "$off" -lt "$size" ]; then
    tail -c +$((off + 1)) "$f" | eval $SSH "\"cat >> $DST_DIR/$rname\"" \
      || { echo "FAIL forward $rname" >&2; echo "$off"; return; }
    off=$size
  fi
  echo "$off"
}

sig_off=$(forward "$SIG" "$sig_off" "signals.jsonl")
alert_off=$(forward "$ALERT" "$alert_off" "alerts.jsonl")
printf "%s\n%s\n" "$sig_off" "$alert_off" > "$STATE_F"
