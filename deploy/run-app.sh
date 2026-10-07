#!/usr/bin/env bash
# Khoi dong 1 app cho pm2:  run-app.sh <env_file|-> <cwd> <python> [args...]
#
# - Nap env_file (KEY=VALUE, '#' la comment, bo nhay bao quanh) bang parser
#   rieng: KHONG eval -> gia tri chua $, dau cach, ky tu dac biet giu nguyen.
#   Bien da co san trong moi truong duoc giu (giong _load_env_file cua bot).
#   Secret khong nam trong pm2 dump (~/.pm2/dump.pm2), sua .env roi restart
#   la app nhan gia tri moi.
# - exec python -> cung PID, SIGINT/SIGTERM cua pm2 toi thang bot (dung sach).
set -u
env_file="$1"; cwd="$2"; py="$3"; shift 3

if [ "$env_file" != "-" ] && [ -n "$env_file" ]; then
    if [ ! -r "$env_file" ]; then
        echo "run-app: khong doc duoc ENV_FILE $env_file" >&2
        exit 78
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        # bo khoang trang dau dong
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in ''|'#'*) continue ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        key="${line%%=*}"; val="${line#*=}"
        key="${key#export }"
        key="${key//[[:space:]]/}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}" ;;
            \'*\') val="${val#\'}"; val="${val%\'}" ;;
        esac
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        if [ -z "${!key+x}" ]; then
            export "$key=$val"
        fi
    done < "$env_file"
fi

cd "$cwd" || { echo "run-app: khong vao duoc $cwd" >&2; exit 78; }
if [ ! -x "$py" ]; then
    echo "run-app: khong thay python $py (sua PYTHON trong deploy/deploy.env)" >&2
    exit 78
fi
export PYTHONUNBUFFERED=1
exec "$py" "$@"
