#!/usr/bin/env bash
# Profile only a newly created disposable database; never use the working Compose.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$project_root"
benchmark_python="${BENCHMARK_PYTHON:-.venv/bin/python}"
if ! command -v "$benchmark_python" >/dev/null; then
  echo 'Benchmark Python is unavailable; install project dev dependencies first.' >&2
  exit 2
fi

container_name="moex-sentinel-profile-$$-${RANDOM}"
container_id="$(docker create --name "$container_name" \
  --env POSTGRES_HOST_AUTH_METHOD=trust \
  --env POSTGRES_USER=sentinel_profile --env POSTGRES_DB=sentinel_profile \
  --publish '127.0.0.1::5432' --tmpfs /var/lib/postgresql/data:rw postgres:16-alpine)"

cleanup() {
  local outcome=$?
  trap - EXIT
  docker stop --time 10 "$container_id" >/dev/null || true
  docker rm "$container_id" >/dev/null || true
  exit "$outcome"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker start "$container_id" >/dev/null
ready=false
for ((attempt = 0; attempt < 30; attempt++)); do
  if docker exec "$container_id" pg_isready -U sentinel_profile -d sentinel_profile >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo 'Disposable PostgreSQL did not become ready within 30 checks.' >&2
  exit 3
fi

address="$(docker port "$container_id" 5432/tcp)"
if [[ ! "$address" =~ ^127\.0\.0\.1:[0-9]+$ ]]; then
  echo 'Disposable PostgreSQL has an unexpected port binding.' >&2
  exit 4
fi
PG_PROFILE_DATABASE_URL="postgresql+psycopg://sentinel_profile@${address}/sentinel_profile" \
  PG_PROFILE_STORAGE_KIND=tmpfs \
  "$benchmark_python" -m develop.benchmarks.postgresql "$@"
