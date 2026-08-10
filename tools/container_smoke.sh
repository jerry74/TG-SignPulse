#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: container_smoke.sh <immutable-image>}"
container="tg-signplus-smoke"
port="${TGSP_SMOKE_PORT:-18081}"
root="$(mktemp -d)"
data_dir="${root}/data"
restore_dir="${root}/restore"

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
  rm -rf "${root}"
}
trap cleanup EXIT

mkdir -p "${data_dir}" "${restore_dir}"
chmod 0755 "${root}" "${restore_dir}"
chown 10001:10001 "${data_dir}"

wait_healthy() {
  for _ in $(seq 1 60); do
    if [ "$(docker inspect -f '{{.State.Health.Status}}' "${container}" 2>/dev/null || true)" = "healthy" ]; then
      return 0
    fi
    sleep 1
  done
  docker logs "${container}" >&2
  return 1
}

docker run -d \
  --name "${container}" \
  --restart unless-stopped \
  --read-only \
  --tmpfs /tmp \
  --tmpfs /app/__pycache__ \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -p "127.0.0.1:${port}:8080" \
  -e APP_DATA_DIR=/data \
  -e APP_MASTER_KEY=test-master-key-that-is-long-enough-123456 \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD=smoke-admin-password \
  -e TG_API_ID=12345 \
  -e TG_API_HASH=smoke-api-hash \
  -e ENABLE_FAILURE_NOTIFIER=false \
  -v "${data_dir}:/data" \
  "${image}" >/dev/null

echo "smoke: health and isolation"
wait_healthy
curl --fail --silent "http://127.0.0.1:${port}/healthz" | grep -q '"status":"ok"'
curl --fail --silent "http://127.0.0.1:${port}/readyz" | grep -q '"status":"ready"'
curl --fail --silent "http://127.0.0.1:${port}/" | grep -q 'TG-SignPlus'
test "$(docker exec "${container}" id -u)" = "10001"
test "$(docker exec "${container}" id -g)" = "10001"
if docker exec "${container}" touch /rootfs-write-test >/dev/null 2>&1; then
  echo "read-only root filesystem check failed" >&2
  exit 1
fi
test "$(docker exec "${container}" python -c "import sqlite3; print(sqlite3.connect('/data/signplus.sqlite').execute('pragma user_version').fetchone()[0])")" = "3"

echo "smoke: online backup and isolated restore"
docker exec "${container}" python -c "import sqlite3; source=sqlite3.connect('/data/signplus.sqlite'); target=sqlite3.connect('/data/smoke-backup.sqlite'); source.backup(target); target.close(); source.close()"
docker cp "${container}:/data/smoke-backup.sqlite" "${restore_dir}/signplus.sqlite" >/dev/null
chmod 0755 "${restore_dir}"
chmod 0644 "${restore_dir}/signplus.sqlite"
test "$(docker run --rm --user 0:0 --entrypoint python -v "${restore_dir}:/restore:ro" "${image}" -c "import sqlite3; db=sqlite3.connect('file:/restore/signplus.sqlite?mode=ro&immutable=1', uri=True); print(db.execute('select count(*) from users').fetchone()[0])")" = "1"

echo "smoke: normal restart persistence"
docker restart "${container}" >/dev/null
wait_healthy
test "$(docker exec "${container}" python -c "import sqlite3; print(sqlite3.connect('/data/signplus.sqlite').execute('select count(*) from users').fetchone()[0])")" = "1"

echo "smoke: forced process interruption recovery"
restart_before="$(docker inspect -f '{{.RestartCount}}' "${container}")"
docker exec "${container}" sh -c 'read -r child _ < /proc/1/task/1/children; test -n "${child}"; kill -KILL "${child}"' >/dev/null 2>&1 || true
restart_after="${restart_before}"
for _ in $(seq 1 60); do
  restart_after="$(docker inspect -f '{{.RestartCount}}' "${container}")"
  health="$(docker inspect -f '{{.State.Health.Status}}' "${container}")"
  if [ "${restart_after}" -gt "${restart_before}" ] && [ "${health}" = "healthy" ]; then
    break
  fi
  sleep 1
done
test "${restart_after}" -gt "${restart_before}"

logs="$(docker logs "${container}" 2>&1)"
if printf '%s' "${logs}" | grep -Eqi 'test-master-key|smoke-admin-password|smoke-api-hash|BEGIN [A-Z ]*PRIVATE KEY|session_string'; then
  echo "secret-like material found in container logs" >&2
  exit 1
fi

echo "container smoke test passed"
