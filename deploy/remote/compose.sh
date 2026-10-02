#!/bin/sh
# Invoke from any directory; do not source the env file as shell code.
set -eu
REMOTE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$REMOTE_DIR/../.." && pwd)
REMOTE_ENV_FILE=${REMOTE_ENV_FILE:-$REMOTE_DIR/.env}
# PROD uses an explicit wrapper, never infer contour from secret env contents.
if [ "${REMOTE_CONTOUR:-TEST}" = PROD ]; then
  set -- -f "$REMOTE_DIR/compose.production.yml" "$@"
fi
exec docker compose --project-directory "$REPO_DIR" \
  --env-file "$REMOTE_ENV_FILE" \
  -f "$REPO_DIR/compose.yml" -f "$REMOTE_DIR/compose.remote.yml" "$@"
