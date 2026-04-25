#!/usr/bin/env bash
# safe-restart.sh — validate config, then reconcile singbox-server.
#
# Wrapper around `docker compose up -d singbox-server` that first runs
# `sing-box check` against the config.json that's about to be mounted. A JSON
# typo, missing key, or schema regression would otherwise only surface when
# the container starts, by which point sing-box has already died — and if the
# old container was running with the old config, recreating it replaces a
# working process with a broken one. Running check in a throwaway container
# lets us fail fast with the old container still serving traffic.
#
# Uses `up -d` (not `restart`) so compose.yaml changes — cap set, env vars,
# volume mounts — get applied on next invocation. `restart` only bounces
# the process with the existing container config; operational rollouts of
# compose edits would silently no-op with the old shape still live.
#
# Configuration via env vars (set or sourced from the repo .env):
#   SINGBOX_SERVER_DIR  Directory holding config.json + hy2.crt + hy2.key + this
#                       script's compose.yaml. Defaults to the script's own dir.
#   COMPOSE_FILE        Override the compose file used. Defaults to
#                       ${SINGBOX_SERVER_DIR}/compose.yaml.
#   COMPOSE_ENV_FILE    Optional --env-file passed to docker compose. If unset
#                       and a sibling .env exists, that's used.
#   NOTIFY              Optional path to a notification script invoked on
#                       failure with one argument: the error summary.
#                       Receives a single string. Unset = print to stderr.
#
# Exit codes:
#   0  config valid, restart issued
#   1  config invalid, restart skipped
#   2  something else went wrong (docker missing, compose error)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SINGBOX_SERVER_DIR="${SINGBOX_SERVER_DIR:-${SCRIPT_DIR}}"
CONFIG="${SINGBOX_SERVER_DIR}/config.json"
COMPOSE_FILE="${COMPOSE_FILE:-${SINGBOX_SERVER_DIR}/compose.yaml}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${SINGBOX_SERVER_DIR}/.env}"
NOTIFY="${NOTIFY:-}"
IMAGE="ghcr.io/sagernet/sing-box:latest"
DRY_RUN=0

[[ "${1:-}" == "--no-restart" ]] && DRY_RUN=1

notify() {
  if [[ -n "${NOTIFY}" && -x "${NOTIFY}" ]]; then
    "${NOTIFY}" "$1"
  else
    echo "$1" >&2
  fi
}

# 1. Config file must exist and be non-empty — catches a truncated edit
#    that would otherwise make sing-box crash immediately on start.
if [[ ! -s "${CONFIG}" ]]; then
  notify "🚫 singbox-server safe-restart: \`${CONFIG}\` missing or empty, aborting"
  exit 1
fi

# 2. Run sing-box check inside a throwaway container. We deliberately use the
#    same image the service runs (":latest" pin follows the running stack).
#    --rm so this leaves no container behind; -v ro because check reads only.
#    The hy2 cert/key mounts match compose.yaml — `check` validates that the
#    paths referenced by config.json are readable (not just that the JSON is
#    well-formed), so missing mounts would produce a false negative.
check_output=$(docker run --rm \
  -v "${CONFIG}:/etc/sing-box/config.json:ro" \
  -v "${SINGBOX_SERVER_DIR}/hy2.crt:/etc/sing-box/hy2.crt:ro" \
  -v "${SINGBOX_SERVER_DIR}/hy2.key:/etc/sing-box/hy2.key:ro" \
  "${IMAGE}" check -c /etc/sing-box/config.json 2>&1)
check_rc=$?

if [[ ${check_rc} -ne 0 ]]; then
  # Trim to a manageable length — most notification channels limit message
  # size, and sing-box parse errors are often short but can include multi-line
  # JSON context. Take the first 400 chars and backtick-wrap for readability.
  snippet=$(echo "${check_output}" | tr '\n' ' ' | cut -c1-400)
  notify "🚫 singbox-server config invalid — restart NOT issued. Error: \`${snippet}\`"
  echo "config check FAILED:" >&2
  echo "${check_output}" >&2
  exit 1
fi

echo "config check passed"

# 3. Short-circuit if caller only wanted validation.
[[ ${DRY_RUN} -eq 1 ]] && exit 0

# 4. Config valid — reconcile via `up -d`. This recreates the container
#    if compose.yaml changed (cap set, env, mounts) and is a plain restart
#    otherwise. Profile scoping prevents compose from touching unrelated
#    services on the same project. --env-file is explicit because
#    compose.yaml references ${PUID}/${PGID}/${SINGBOX_SERVER_DIR} —
#    without it, a missing env expansion would materialise as blank
#    values and the container would recreate with `user: ":"` or empty
#    volume paths.
id_before=$(docker inspect singbox-server -f '{{.Id}}' 2>/dev/null || true)
compose_args=( --profile singbox-server -f "${COMPOSE_FILE}" )
[[ -f "${COMPOSE_ENV_FILE}" ]] && compose_args+=( --env-file "${COMPOSE_ENV_FILE}" )
docker compose "${compose_args[@]}" up -d singbox-server
id_after=$(docker inspect singbox-server -f '{{.Id}}' 2>/dev/null || true)

# Force-restart if `up -d` was a no-op (container ID unchanged). sing-box's
# config.json, hy2.crt, and hy2.key are *single-file bind mounts*, which pin
# the host inode at container-create time — rewriting the file on disk leaves
# the running process with the old content still mapped. `up -d` only
# recreates when compose.yaml/env changed, so cert rotations + config edits
# would silently land on disk but not in the container. `docker restart`
# re-resolves the bind-mount inode. See docs/hazards.md.
if [[ -n "${id_before}" && "${id_before}" == "${id_after}" ]]; then
  echo "up -d was a no-op; issuing docker restart to refresh bind-mount inodes"
  docker restart singbox-server
fi
