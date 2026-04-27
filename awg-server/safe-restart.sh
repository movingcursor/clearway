#!/usr/bin/env bash
# safe-restart.sh — validate awg-server config, then reconcile the container.
#
# Wrapper around `docker compose --profile awg-server up -d awg-server` that
# first sanity-checks the rendered awg0.conf. Unlike sing-box, amneziawg-go
# doesn't ship a non-destructive `check` subcommand — wg-quick syntax is
# too forgiving for a real schema validation — so this script verifies
# structure: the file must have an [Interface] block with at minimum
# PrivateKey + ListenPort + Address, plus zero-or-more [Peer] blocks each
# with PublicKey + AllowedIPs. That catches the most common rotation
# failure (truncated render, dangling token).
#
# Uses `up -d` (not `restart`) so compose.yaml changes — cap set, env vars,
# digest pin — get applied. The bind-mount inode pinning that bites
# singbox-server (single-file mounts; see hazards.md) bites awg-server
# the same way: an in-place edit of awg0.conf without a container
# recreate leaves the running process with the old inode still mapped.
# Force-restart on no-op covers that.
#
# Configuration:
#   SINGBOX_SERVER_DIR  Repo root reference; awg-server/ is a sibling of
#                       singbox-server/. Falls back to <script-dir>/.. if
#                       unset (the in-repo layout). The compose file is
#                       resolved relative to this dir.
#   COMPOSE_FILE        Override the compose file. Defaults to the
#                       awg-server/compose.yaml next to this script.
#   COMPOSE_ENV_FILE    Optional --env-file. Falls back to a sibling .env.
#   NOTIFY              Optional notification script invoked on failure.
#
# Exit codes:
#   0  config valid, restart issued
#   1  config invalid, restart skipped
#   2  something else went wrong (docker missing, compose error)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AWG_SERVER_DIR="${AWG_SERVER_DIR:-${SCRIPT_DIR}}"
CONFIG="${AWG_SERVER_DIR}/config/awg0.conf"
COMPOSE_FILE="${COMPOSE_FILE:-${AWG_SERVER_DIR}/compose.yaml}"
# .env at the repo root one level up; matches singbox-server's pattern.
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${AWG_SERVER_DIR}/../.env}"
NOTIFY="${NOTIFY:-}"
DRY_RUN=0

[[ "${1:-}" == "--no-restart" ]] && DRY_RUN=1

notify() {
  if [[ -n "${NOTIFY}" && -x "${NOTIFY}" ]]; then
    "${NOTIFY}" "$1"
  else
    echo "$1" >&2
  fi
}

# 1. Config must exist and be non-empty.
if [[ ! -s "${CONFIG}" ]]; then
  notify "🚫 awg-server safe-restart: \`${CONFIG}\` missing or empty, aborting"
  exit 1
fi

# 2. Structural check: [Interface] section with PrivateKey + ListenPort +
#    Address. Empty-peer-set is allowed (a freshly-rendered config with no
#    AWG users yet still passes; the container starts but accepts no
#    handshakes). awk is in coreutils — no extra dependency.
awk_out=$(awk '
  /^\[Interface\]/ { in_iface=1; in_peer=0; next }
  /^\[Peer\]/      { in_iface=0; in_peer=1; peers++; have_pubkey=0; have_allowed=0; next }
  in_iface && /^[[:space:]]*PrivateKey[[:space:]]*=/  { have_priv=1 }
  in_iface && /^[[:space:]]*ListenPort[[:space:]]*=/  { have_port=1 }
  in_iface && /^[[:space:]]*Address[[:space:]]*=/     { have_addr=1 }
  in_peer  && /^[[:space:]]*PublicKey[[:space:]]*=/   { have_pubkey=1 }
  in_peer  && /^[[:space:]]*AllowedIPs[[:space:]]*=/  { have_allowed=1 }
  in_peer  && /^\[/ { if (!have_pubkey || !have_allowed) bad_peer=1 }
  END {
    if (!have_priv)  print "missing [Interface] PrivateKey"
    if (!have_port)  print "missing [Interface] ListenPort"
    if (!have_addr)  print "missing [Interface] Address"
    if (bad_peer)    print "a [Peer] block is missing PublicKey or AllowedIPs"
  }
' "${CONFIG}")

if [[ -n "${awk_out}" ]]; then
  notify "🚫 awg-server config invalid — restart NOT issued: ${awk_out}"
  echo "config check FAILED:" >&2
  echo "${awk_out}" >&2
  exit 1
fi

echo "config check passed"

# 3. Short-circuit if caller only wanted validation.
[[ ${DRY_RUN} -eq 1 ]] && exit 0

# 4. Reconcile via `up -d`. Profile-scoped so compose doesn't touch
#    singbox-server or other services. --env-file is explicit because
#    compose.yaml references ${VNIC_SECONDARY_IP} + ${SINGBOX_SERVER_DIR};
#    a missing env expansion would silently render a blank port bind.
id_before=$(docker inspect awg-server -f '{{.Id}}' 2>/dev/null || true)
compose_args=( --profile awg-server -f "${COMPOSE_FILE}" )
[[ -f "${COMPOSE_ENV_FILE}" ]] && compose_args+=( --env-file "${COMPOSE_ENV_FILE}" )
docker compose "${compose_args[@]}" up -d awg-server
id_after=$(docker inspect awg-server -f '{{.Id}}' 2>/dev/null || true)

# Force-restart if `up -d` was a no-op. Single-file bind mount on
# config/awg0.conf pins the inode at create time; an in-place rewrite
# without recreate leaves the running process with the old config still
# mapped. See docs/hazards.md.
if [[ -n "${id_before}" && "${id_before}" == "${id_after}" ]]; then
  echo "up -d was a no-op; issuing docker restart to refresh bind-mount inode"
  docker restart awg-server
fi
