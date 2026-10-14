#!/bin/sh
# Invoke from any directory; do not source the env file as shell code.
set -eu
REMOTE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_DIR=$(CDPATH= cd -- "$REMOTE_DIR/../.." && pwd -P)
RELEASE_DIR=$REPO_DIR
REMOTE_ENV_FILE=${REMOTE_ENV_FILE:-$REMOTE_DIR/.env}
REMOTE_CONTOUR=${REMOTE_CONTOUR:-TEST}
# PROD uses an explicit wrapper, never infer contour from secret env contents.
PROD_COMPOSE_FILE=
if [ "$REMOTE_CONTOUR" = PROD ]; then
  PROD_COMPOSE_FILE=$REMOTE_DIR/compose.production.yml
fi
OPS_DIR=$RELEASE_DIR/operations
OPS_MARKER=$OPS_DIR/.compose-ops-installed
OPS_OVERLAY=
OPS_ANCHOR=$OPS_DIR/admission_ops.py
OPS_RELEASE_NAME=${REPO_DIR##*/}
OPS_REQUIRED=0
if [ -e "$OPS_ANCHOR" ] || [ -L "$OPS_ANCHOR" ]; then
  OPS_REQUIRED=1
fi
case "$OPS_RELEASE_NAME" in
  vps-20261009-p3-????????????) OPS_REQUIRED=1 ;;
esac
if [ "$OPS_REQUIRED" = 1 ] && { [ ! -f "$OPS_ANCHOR" ] || [ -L "$OPS_ANCHOR" ]; }; then
  echo "Installed operations anchor is missing or invalid." >&2
  exit 1
fi
if [ -e "$OPS_MARKER" ] || [ -L "$OPS_MARKER" ]; then
  [ -f "$OPS_MARKER" ] && [ ! -L "$OPS_MARKER" ] || { echo "Installed Compose marker is invalid." >&2; exit 1; }
OPS_INFO=$(python3 - "$OPS_MARKER" "$REMOTE_CONTOUR" "${REMOTE_OPERATION_MODE:-}" <<'PY'
import hashlib
import json
import os
import stat
import sys

marker, contour, requested = sys.argv[1:]
info = os.stat(marker, follow_symlinks=False)
if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
    raise SystemExit("Installed Compose marker permissions are invalid.")
anchor = os.path.join(os.path.dirname(marker), "admission_ops.py")
anchor_info = os.stat(anchor, follow_symlinks=False)
if not stat.S_ISREG(anchor_info.st_mode) or anchor_info.st_uid != os.geteuid() or stat.S_IMODE(anchor_info.st_mode) != 0o600:
    raise SystemExit("Installed operations anchor permissions are invalid.")
with open(anchor, "rb") as stream:
    anchor_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
with open(marker, encoding="utf-8") as stream:
    data = json.load(stream)
mode = data.get("mode")
if data.get("schema") != 1 or mode not in {"candidate", "fallback", "original"} or contour not in {"TEST", "PROD"}:
    raise SystemExit("Installed Compose marker is invalid.")
if data.get("operations_sha256") != anchor_sha256:
    raise SystemExit("Installed operations anchor hash changed.")
if requested and requested != mode:
    raise SystemExit("Requested Compose mode does not match the installed mode.")
record = data.get("files", {}).get(mode, {}).get(contour)
if not isinstance(record, dict):
    raise SystemExit("Installed Compose config is missing.")
for key in ("file", "sha256", "images_file", "images_sha256"):
    if not isinstance(record.get(key), str):
        raise SystemExit("Installed Compose config record is invalid.")
if any("/" in record[key] or ".." in record[key] for key in ("file", "images_file")):
    raise SystemExit("Installed Compose config path is invalid.")
for key in ("file", "images_file"):
    path = os.path.join(os.path.dirname(marker), record[key])
    file_info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.geteuid() or stat.S_IMODE(file_info.st_mode) != 0o600:
        raise SystemExit("Installed Compose config permissions are invalid.")
print("\t".join((mode, record["file"], record["sha256"], record["images_file"], record["images_sha256"])))
PY
  ) || { echo "Installed Compose config is invalid." >&2; exit 1; }
  IFS="$(printf '\t')" read -r OPS_MODE OPS_FILE OPS_SHA OPS_IMAGES OPS_IMAGES_SHA <<EOF
$OPS_INFO
EOF
  [ -f "$OPS_DIR/$OPS_FILE" ] && [ ! -L "$OPS_DIR/$OPS_FILE" ] || { echo "Installed Compose overlay is missing." >&2; exit 1; }
  [ "$(sha256sum "$OPS_DIR/$OPS_FILE" | awk '{print $1}')" = "$OPS_SHA" ] || { echo "Installed Compose overlay hash changed." >&2; exit 1; }
  [ -f "$OPS_DIR/$OPS_IMAGES" ] && [ ! -L "$OPS_DIR/$OPS_IMAGES" ] || { echo "Installed Compose image pins are missing." >&2; exit 1; }
  [ "$(sha256sum "$OPS_DIR/$OPS_IMAGES" | awk '{print $1}')" = "$OPS_IMAGES_SHA" ] || { echo "Installed Compose image pins changed." >&2; exit 1; }
  while IFS="$(printf '\t')" read -r image expected_id; do
    [ -n "$image" ] && [ -n "$expected_id" ] || { echo "Installed Compose image pin is malformed." >&2; exit 1; }
    [ "$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null)" = "$expected_id" ] || { echo "Installed Compose image identity changed." >&2; exit 1; }
  done < "$OPS_DIR/$OPS_IMAGES"
  OPS_OVERLAY=$OPS_DIR/$OPS_FILE
elif [ "$OPS_REQUIRED" = 1 ]; then
  echo "Installed Compose marker is missing." >&2
  exit 1
fi
if [ -n "$OPS_OVERLAY" ]; then
  if [ -n "$PROD_COMPOSE_FILE" ]; then
    exec docker compose --project-directory "$REPO_DIR" --env-file "$REMOTE_ENV_FILE" \
      -f "$REPO_DIR/compose.yml" -f "$REMOTE_DIR/compose.remote.yml" -f "$PROD_COMPOSE_FILE" \
      -f "$OPS_OVERLAY" "$@"
  fi
  exec docker compose --project-directory "$REPO_DIR" --env-file "$REMOTE_ENV_FILE" \
    -f "$REPO_DIR/compose.yml" -f "$REMOTE_DIR/compose.remote.yml" -f "$OPS_OVERLAY" "$@"
fi
if [ -n "$PROD_COMPOSE_FILE" ]; then
  exec docker compose --project-directory "$REPO_DIR" --env-file "$REMOTE_ENV_FILE" \
    -f "$REPO_DIR/compose.yml" -f "$REMOTE_DIR/compose.remote.yml" -f "$PROD_COMPOSE_FILE" "$@"
fi
exec docker compose --project-directory "$REPO_DIR" \
  --env-file "$REMOTE_ENV_FILE" -f "$REPO_DIR/compose.yml" -f "$REMOTE_DIR/compose.remote.yml" "$@"
