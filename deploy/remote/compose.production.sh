#!/bin/sh
set -eu
REMOTE_CONTOUR=PROD
REMOTE_ENV_FILE=${REMOTE_ENV_FILE:-$(dirname -- "$0")/.env.production}
export REMOTE_CONTOUR REMOTE_ENV_FILE
exec sh "$(dirname -- "$0")/compose.sh" "$@"
