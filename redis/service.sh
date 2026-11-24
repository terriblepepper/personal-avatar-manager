#!/usr/bin/env bash
# Manage only the Unix socket and Redis instance owned by this checkout.
set -eu
umask 077
action="${1:-}"
case "$action" in
  start) [[ "${AVATAR_REDIS_SOCKET:-}" == redis/redis.sock ]] || exit 0 ;;
  stop) ;;
  *) echo '用法：redis/service.sh start|stop' >&2; exit 2 ;;
esac
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
exec 8>redis.lock
warn() { echo "Redis：$*；应用可回源数据库。" >&2; }
flock -w 3 8 || { warn "等待服务锁超时"; exit 0; }

key="$(printf '%s' "$PWD" | sha256sum)"
unit="personal-avatar-redis-${key:0:16}.service"
if command -v systemd-run >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  if systemctl --user is-active --quiet "$unit"; then
    if [[ "$action" == stop ]]; then systemctl --user stop "$unit"; fi
    exit 0
  fi
else
  unit=''
fi

native="$(command -v redis-server || true)"
if [[ -z "$native" && -x bin/redis-server ]]; then native="$PWD/bin/redis-server"; fi
if [[ -n "$native" && -r redis.pid ]] && read -r pid <redis.pid &&
   [[ "$pid" =~ ^[1-9][0-9]*$ && "$(readlink "/proc/$pid/cwd" || true)" == "$PWD" &&
      "$(readlink "/proc/$pid/exe" || true)" == "$(readlink -f "$native")" ]]; then
  if [[ "$action" == stop ]]; then
    kill -TERM "$pid"
    for attempt in {1..30}; do [[ -S redis.sock ]] || break; sleep 0.1; done
  fi
  exit 0
fi

[[ "$action" == start ]] || exit 0
[[ -n "$native" ]] || { warn '请安装原生 redis-server 并配置 PATH'; exit 0; }
# An operator-managed listener on the same socket takes precedence.
if [[ -S redis.sock ]] && python3 -c 'import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(.2); s.connect("redis.sock")' 2>/dev/null; then
  warn '套接字已由其他进程监听'
  exit 0
fi
if [[ -d lib ]]; then export LD_LIBRARY_PATH="$PWD/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; fi
if [[ -n "$unit" ]]; then
  systemd-run --user --unit="$unit" --collect --property=Type=exec --property=Restart=on-failure \
    --property="WorkingDirectory=$PWD" --property=UMask=0077 \
    --setenv="LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}" "$native" "$PWD/redis.conf" --daemonize no >/dev/null
else
  "$native" redis.conf --daemonize yes --pidfile "$PWD/redis.pid" --logfile redis.log 8>&-
fi
