#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
umask 077
if [[ -f .env ]]; then set -a; source .env; set +a; fi
[[ -x .venv/bin/python ]] || { echo '缺少 .venv，请先执行 uv sync --locked'; exit 1; }
./redis/service.sh start || echo "Redis 未就绪，应用回源数据库。" >&2
exec .venv/bin/python -m uvicorn avatar.api.app:create_app --factory \
  --host "${AVATAR_HOST:-0.0.0.0}" --port "${AVATAR_PORT:-18080}" \
  --loop asyncio --workers 1 --no-proxy-headers --no-access-log
