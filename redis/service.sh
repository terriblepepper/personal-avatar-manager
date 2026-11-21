#!/usr/bin/env bash
# Manage only the local socket owned by this checkout.
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
native="$(command -v redis-server || true)"
if [[ -n "$native" && -r redis.pid ]] && read -r pid <redis.pid &&
   [[ "$pid" =~ ^[1-9][0-9]*$ && "$(readlink "/proc/$pid/cwd" || true)" == "$PWD" &&
      "$(readlink "/proc/$pid/exe" || true)" == "$(readlink -f "$native")" ]]; then
  if [[ "$action" == stop ]]; then
    kill -TERM "$pid"
    for attempt in {1..30}; do [[ -S redis.sock ]] || break; sleep 0.1; done
  fi
  exit 0
fi

if command -v docker >/dev/null; then
  key="$(printf '%s' "$PWD" | sha256sum)"
  name="personal-avatar-redis-${key:0:16}"
  owner="$(timeout 3 docker container inspect --format '{{ index .Config.Labels "personal-avatar.cache-dir" }}' "$name" 2>/dev/null || true)"
  if [[ "$owner" == "$PWD" ]]; then
    if [[ "$action" == stop ]]; then
      timeout 9 docker stop -t 5 "$name" >/dev/null || warn '容器停止失败'
    else
      timeout 5 docker start "$name" >/dev/null || warn '容器启动失败'
    fi
    exit 0
  fi
fi
[[ "$action" == start ]] || exit 0
# Do not unlink a live socket managed by another service.
if [[ -S redis.sock ]] && python3 -c 'import socket; s = socket.socket(socket.AF_UNIX); s.settimeout(0.2); s.connect("redis.sock")' 2>/dev/null; then
  warn '套接字已由其他进程监听'
  exit 0
fi

if [[ -n "$native" ]]; then
  "$native" redis.conf --daemonize yes --pidfile "$PWD/redis.pid" --logfile redis.log 8>&- || warn '本机服务启动失败'
elif command -v docker >/dev/null; then
  for candidate in redis:7-alpine acps/redis:7-alpine-2.2.0-linux-amd64; do
    if timeout 3 docker image inspect "$candidate" >/dev/null 2>&1; then
      timeout 8 docker run -d --pull=never --rm --name "$name" --label "personal-avatar.cache-dir=$PWD" \
        --network none --cap-drop ALL --security-opt no-new-privileges --read-only \
        --user "$(id -u):$(id -g)" --workdir /runtime --volume "$PWD:/runtime" \
        "$candidate" redis-server redis.conf >/dev/null || warn '容器启动失败'
      exit 0
    fi
  done
  warn '未找到本地 Redis 7 镜像'
else
  warn '未安装 redis-server 或 Docker'
fi
