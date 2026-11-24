#!/usr/bin/env bash
# Run one KRaft broker on the local host, with an isolated project volume.
set -eu
umask 077
case "${1:-}" in
  start|stop|status) action="$1" ;;
  *) echo '用法：kafka/service.sh start|stop|status' >&2; exit 2 ;;
esac
if [[ "$action" == start && "${AVATAR_KAFKA_MANAGED:-0}" != 1 ]]; then exit 0; fi
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
key="$(printf '%s' "$PWD" | sha256sum)"
name="personal-avatar-kafka-${key:0:16}"
exec 8>kafka.lock
flock -w 5 8
command -v docker >/dev/null || { echo 'Kafka 需要 Docker' >&2; exit 1; }

owner="$(docker container inspect --format '{{ index .Config.Labels "personal-avatar.kafka-dir" }}' "$name" 2>/dev/null || true)"
if [[ -n "$owner" && "$owner" != "$PWD" ]]; then
  echo 'Kafka 容器归属不匹配' >&2
  exit 1
fi
if [[ "$action" == stop ]]; then
  if [[ "$owner" == "$PWD" ]]; then docker stop -t 15 "$name" >/dev/null; fi
  exit 0
fi
if [[ "$action" == status ]]; then
  [[ "$owner" == "$PWD" ]] && docker inspect --format '{{.State.Status}}' "$name"
  exit
fi
port="${AVATAR_KAFKA_PORT:-19092}"
controller="${AVATAR_KAFKA_CONTROLLER_PORT:-19093}"
for value in "$port" "$controller"; do
  if [[ ! "$value" =~ ^[1-9][0-9]{3,4}$ ]] || (( value < 1024 || value > 65535 )); then
    echo 'Kafka 端口必须在 1024 到 65535 之间' >&2
    exit 2
  fi
done
[[ "$port" != "$controller" ]] || { echo 'Kafka 两个端口不能相同' >&2; exit 2; }
if [[ "${AVATAR_KAFKA_BOOTSTRAP:-}" != "127.0.0.1:$port" ]]; then
  echo '本地 Kafka bootstrap 必须匹配 127.0.0.1 和 broker 端口' >&2
  exit 2
fi

image="${AVATAR_KAFKA_IMAGE:-apache/kafka:4.1.2@sha256:5cc2a2fd93fa2687b44015eee04fb2c3edd9e526bd64bf8bec5ff1e268772e0e}"
config="$(printf '%s\n' "$image" "$port" "$controller" "$(sha256sum broker.env)" | sha256sum | cut -d ' ' -f 1)"
if [[ "$owner" == "$PWD" ]]; then
  existing="$(docker container inspect --format '{{ index .Config.Labels "personal-avatar.kafka-config" }}' "$name")"
  if [[ "$existing" != "$config" ]]; then
    echo 'Kafka 的端口、镜像或配置已改变，请停止并移除旧容器，保留 kafka/data 后再启动。' >&2
    exit 1
  fi
  docker start "$name" >/dev/null
  exit 0
fi
mkdir -p data logs
if [[ ! -s data/cluster.id ]]; then
  python3 -c 'import base64, uuid; print(base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("="))' >data/cluster.id
fi
docker run -d --pull=never --name "$name" --label "personal-avatar.kafka-dir=$PWD" \
  --label "personal-avatar.kafka-config=$config" \
  --network host --cap-drop ALL --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --volume "$PWD:/runtime" --log-opt max-size=10m --log-opt max-file=3 \
  --env-file broker.env -e "CLUSTER_ID=$(cat data/cluster.id)" \
  -e "KAFKA_LISTENERS=PLAINTEXT://127.0.0.1:$port,CONTROLLER://127.0.0.1:$controller" \
  -e "KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://127.0.0.1:$port" \
  -e "KAFKA_CONTROLLER_QUORUM_VOTERS=1@127.0.0.1:$controller" \
  "$image" >/dev/null
