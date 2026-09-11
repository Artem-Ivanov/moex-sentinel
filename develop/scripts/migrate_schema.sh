#!/usr/bin/env bash
# Apply the supported schema migrations to the configured Compose database.
set -euo pipefail

if (( $# != 0 )); then
  echo 'Usage: bash develop/scripts/migrate_schema.sh' >&2
  exit 2
fi

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
exec docker compose --project-directory "$project_root" \
  -f "$project_root/compose.yml" --profile migrations run --rm migrations
