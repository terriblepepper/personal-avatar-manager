#!/usr/bin/env bash
# Manage the user service or a project-owned local process.
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
umask 077
action="${1:---help}"
[[ $# -le 1 ]] || { echo '用法：./service.sh start|stop|install'; exit 2; }
case "$action" in
  -h|--help) echo '用法：./service.sh start|stop|install'; exit 0 ;;
  start|stop|install) ;;
  *) echo '用法：./service.sh start|stop|install' >&2; exit 2 ;;
esac
if [[ "$action" == start && -f .env ]]; then set -a; source .env; set +a; fi
service=personal-avatar.service
service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$service"
[[ "$action" == stop || -x .venv/bin/python ]] || uv sync --locked

owned_process() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]] &&
    [[ "$(readlink /proc/"$1"/cwd 2>/dev/null)" == "$PWD" ]] || return 1
  local -a process_args
  mapfile -d '' -t process_args 2>/dev/null < /proc/"$1"/cmdline || return 1
  [[ "${process_args[1]:-}" == -m && "${process_args[2]:-}" == uvicorn &&
     "${process_args[3]:-}" == avatar.api.app:create_app ]]
}

pg_home="${AVATAR_PG_HOME:-$HOME/.local/pgsql-16}"
pg_bin="$pg_home/usr/lib/postgresql/16/bin"
pg_data="$PWD/data/pgsql"
pg_log="$PWD/data/pgsql.startup.log"

pg_start() {
  [[ -x "$pg_bin/pg_ctl" && -d "$pg_data" ]] || return 0
  export LD_LIBRARY_PATH="$pg_home/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  "$pg_bin/pg_ctl" -D "$pg_data" status >/dev/null 2>&1 && return 0
  "$pg_bin/pg_ctl" -D "$pg_data" -l "$pg_log" -w start ||
    { echo 'PostgreSQL 启动失败，请检查 data/pgsql.startup.log。' >&2; exit 1; }
}

stop_dependencies() {
  ./redis/service.sh stop || echo "Redis 未正常停止，请检查项目的 redis/ 目录。" >&2
  [[ -x "$pg_bin/pg_ctl" && -d "$pg_data" ]] || return 0
  export LD_LIBRARY_PATH="$pg_home/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  "$pg_bin/pg_ctl" -D "$pg_data" status >/dev/null 2>&1 || return 0
  "$pg_bin/pg_ctl" -D "$pg_data" -m fast stop >/dev/null ||
    { echo 'PostgreSQL 停止失败，请检查 data/pgsql.startup.log。' >&2; exit 1; }
}

if [[ "$action" == install ]]; then
  if [[ "$PWD" == *'%'* || "$PWD" == *'"'* || "$PWD" == *$'\n'* ]]; then
    echo 'systemd 安装路径不能包含 %、双引号或换行。' >&2; exit 1
  fi
  mkdir -p -- "$(dirname -- "$service_file")"
  .venv/bin/python - "$PWD" "$service_file" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[2]).write_text(Path('deploy/personal-avatar.service.in').read_text().replace('@PROJECT_DIR@', sys.argv[1].replace('\\', '\\\\')))
PY
  systemctl --user daemon-reload
  systemctl --user enable "$service"
  exec ./service.sh start
fi

if [[ -f "$service_file" ]]; then
  [[ "$action" != start ]] || pg_start
  systemctl --user "$action" "$service"
  [[ "$action" == start ]] || { stop_dependencies; echo 'systemd 服务已停止。'; exit 0; }
else
  mkdir -p data
  exec 9>data/process.lock
  flock -x 9
  [[ "$action" != stop || -f data/avatar.pid ]] || { echo '系统未运行。'; stop_dependencies; exit 0; }
  pid=''
  if [[ -f data/avatar.pid ]]; then read -r pid <data/avatar.pid || true; fi
  if [[ "$action" == stop ]]; then
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || { echo 'PID 文件无效。' >&2; exit 1; }
    if kill -0 "$pid" 2>/dev/null; then
      owned_process "$pid" || { echo 'PID 与本项目进程不匹配，拒绝终止。' >&2; exit 1; }
      kill -TERM "$pid"
      for attempt in {1..20}; do
        owned_process "$pid" || break
        sleep 1
      done
      owned_process "$pid" && { echo '进程尚未退出，请检查日志。' >&2; exit 1; }
    fi
    rm -f data/avatar.pid
    stop_dependencies
    echo '系统已停止。'; exit 0
  fi
  owned_process "$pid" && { echo "系统已运行，PID=$pid"; exit 0; }
  pg_start
  nohup ./run.sh >>data/server.log 2>&1 9>&- </dev/null &
  echo "$!" >data/avatar.pid
fi
for attempt in {1..30}; do
  if curl -fsS --max-time 2 "http://127.0.0.1:${AVATAR_PORT:-18080}/api/health" >/dev/null 2>&1; then
    echo "系统已启动：${AVATAR_PUBLIC_URL:-http://127.0.0.1:${AVATAR_PORT:-18080}}"
    exit 0
  fi
  sleep 1
done
echo '启动未通过健康检查，请检查 journalctl --user -u personal-avatar 或 data/server.log。' >&2
exit 1
