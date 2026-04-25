#!/usr/bin/env bash
# rotate-hy2-cert.sh — generate a fresh Hysteria2 TLS cert + key and push it
# through render.py so every client picks up the new PEM pin.
#
# Why rotate at all: hy2.crt is self-signed and clients pin it exactly
# (render.py inlines the PEM into each hy2 outbound under tls.certificate).
# The pin is the trust anchor — a compromise of hy2.key means an attacker
# can present any cert to clients. Periodic rotation shrinks the compromise
# window.
#
# Why a flag day (no grace): sing-box's TLS layer has no "accept either of
# two certs" window. The moment the server presents the new cert, clients
# still running the old pin fail the handshake. Clients pull updated
# profiles on a poll interval (Windows installer task + mobile
# auto_update_interval), so the practical outage window is one poll cycle
# for hy2 specifically — Reality, ShadowTLS, and WS-CDN are unaffected and
# urltest keeps traffic moving on one of those during the gap.
#
# What it does:
#   1. Generate a 2-year ECDSA P-256 cert with CN+SAN matching
#      $HY2_SNI (defaults to cloud.example.com — set via env or repo .env
#      to match defaults.hysteria2.sni in profiles.yaml).
#   2. Snapshot the old key + cert to .bak-<UTC-ts> (mode 600/644).
#   3. Atomic-swap the new files into place.
#   4. Run `./render.py -y` from singbox-profiles/ which re-inlines the
#      new PEM into every client's hy2 outbound AND triggers safe-restart
#      on the server so it picks up the new cert/key mount.
#   5. Notify on success or failure if NOTIFY is set.
#
# Configuration (env / repo .env):
#   SINGBOX_SERVER_DIR    Directory holding hy2.crt + hy2.key. Defaults to
#                         this script's own dir.
#   SINGBOX_PROFILES_DIR  Directory holding render.py. Defaults to
#                         ../singbox-profiles relative to this script.
#   HY2_SNI               Cover hostname baked into CN+SAN. Defaults to
#                         cloud.example.com — should match
#                         defaults.hysteria2.sni in profiles.yaml.
#   NOTIFY                Optional path to a notification script invoked
#                         on success/failure with one argument.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SINGBOX_SERVER_DIR="${SINGBOX_SERVER_DIR:-${SCRIPT_DIR}}"
SINGBOX_PROFILES_DIR="${SINGBOX_PROFILES_DIR:-$(cd "${SINGBOX_SERVER_DIR}/../singbox-profiles" && pwd)}"
HY2_SNI="${HY2_SNI:-cloud.example.com}"
NOTIFY="${NOTIFY:-}"

CERT="${SINGBOX_SERVER_DIR}/hy2.crt"
KEY="${SINGBOX_SERVER_DIR}/hy2.key"
TS=$(date -u +%Y%m%dT%H%M%SZ)
BAK_CERT="${CERT}.bak-${TS}"
BAK_KEY="${KEY}.bak-${TS}"
# Keep the 3 most recent cert/key backup pairs.
RETAIN=3
SUBJ="/CN=${HY2_SNI}"
# 2-year validity: balances "frequent enough that a leaked key ages out"
# against "not so frequent that operators ignore the rotation prompts".
DAYS=730

notify() {
  if [[ -n "${NOTIFY}" && -x "${NOTIFY}" ]]; then
    "${NOTIFY}" "$1" || true
  else
    echo "$1" >&2
  fi
}

# Pre-flight: the manifest + current state must render cleanly. Rotating
# into a broken manifest produces a config we can't roll back through
# render.py.
if ! "${SINGBOX_PROFILES_DIR}/render.py" --validate >/dev/null 2>&1; then
  notify "🚨 hy2 cert rotation **ABORTED**: pre-flight \`render.py --validate\` failed — fix the manifest first"
  echo "pre-flight validate failed — aborting" >&2
  exit 1
fi

# Generate into TEMP so a failed openssl doesn't corrupt the live files.
TMPDIR_ROT=$(mktemp -d)
trap 'rm -rf "${TMPDIR_ROT}"' EXIT
NEW_CERT="${TMPDIR_ROT}/hy2.crt"
NEW_KEY="${TMPDIR_ROT}/hy2.key"

# ECDSA P-256 (prime256v1). ECDSA is smaller on the wire than RSA for the
# same security margin, which matters for hy2's QUIC where every handshake
# eats bandwidth.
#
# -addext "subjectAltName=DNS:..." is NOT cosmetic: Go's crypto/tls (used
# by sing-box, including iOS SFI) removed the CN hostname fallback in
# Go 1.15. A leaf cert with only CN and no SAN fails verifyHostname even
# when the cert is pinned — the pin check and hostname check are separate
# stages, and the latter reads SAN exclusively. Symptom when missing: hy2
# handshake silently dies, urltest never records a latency sample, clients
# see "no speed" on hy2 while the other inbounds work. See docs/hazards.md.
if ! openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
     -keyout "${NEW_KEY}" -out "${NEW_CERT}" \
     -days "${DAYS}" -nodes -subj "${SUBJ}" \
     -addext "subjectAltName=DNS:${HY2_SNI}" >/dev/null 2>&1; then
  notify "🚨 hy2 cert rotation **ABORTED**: openssl failed — live cert untouched"
  echo "openssl failed" >&2
  exit 1
fi

# Sanity: new cert is readable + matches the key (guards against a
# silently-broken openssl output). Hash both public keys and compare.
cert_spki=$(openssl x509 -in "${NEW_CERT}" -pubkey -noout 2>/dev/null | openssl ec -pubin -outform DER 2>/dev/null | sha256sum | awk '{print $1}')
key_spki=$(openssl ec -in "${NEW_KEY}" -pubout 2>/dev/null | openssl ec -pubin -outform DER 2>/dev/null | sha256sum | awk '{print $1}')
if [[ -z "${cert_spki}" || "${cert_spki}" != "${key_spki}" ]]; then
  notify "🚨 hy2 cert rotation **ABORTED**: new cert/key pair doesn't match"
  echo "cert/key mismatch" >&2
  exit 1
fi

# Backup live files (keep same owner/mode via cp -p).
cp -p "${CERT}" "${BAK_CERT}"
cp -p "${KEY}"  "${BAK_KEY}"

# Trim old cert/key backup pairs beyond RETAIN. Cert and key names are
# trimmed independently — they always rotate in lockstep so their most-
# recent-N sets line up by timestamp, but decoupling the trim keeps the
# logic trivial and tolerates out-of-band key-only or cert-only ops.
ls -1t "${CERT}".bak-* 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f
ls -1t "${KEY}".bak-*  2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f

# Atomic swap: write to the real path via mv (same FS, so atomic). chmod
# BEFORE the mv so the installed files are correct even in the instant
# between the two moves. cert 644 (public) / key 600 (private).
chmod 644 "${NEW_CERT}"
chmod 600 "${NEW_KEY}"
# Match owner to the live files — singbox-server runs as the bind-mount
# owner (PUID:PGID in compose.yaml) and must be able to read both.
live_uid=$(stat -c '%u' "${CERT}")
live_gid=$(stat -c '%g' "${CERT}")
chown "${live_uid}:${live_gid}" "${NEW_CERT}" "${NEW_KEY}" 2>/dev/null || true
mv "${NEW_CERT}" "${CERT}"
mv "${NEW_KEY}"  "${KEY}"

# Push through render.py. This re-inlines the new PEM into every client
# hy2 outbound AND triggers safe-restart.sh on the server. On render
# failure the .bak files are still on disk for manual rollback; we do NOT
# auto-roll back because a failed render could leave clients on the new
# pin already if render succeeded partway.
if ! "${SINGBOX_PROFILES_DIR}/render.py" -y; then
  notify "🚨 hy2 cert rotation: **files swapped but render.py failed**. Manual rollback: \`mv ${BAK_CERT} ${CERT} && mv ${BAK_KEY} ${KEY} && cd ${SINGBOX_PROFILES_DIR} && ./render.py -y\`"
  echo "render.py failed — manual rollback required" >&2
  exit 1
fi

expiry=$(openssl x509 -in "${CERT}" -noout -enddate | cut -d= -f2)
fingerprint=$(openssl x509 -in "${CERT}" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':')
notify "🔐 hy2 cert rotated: new cert valid until **${expiry}**, SHA256 \`${fingerprint:0:24}…\`. Clients pull new pin on next poll; hy2 outage window = one poll cycle (other inbounds unaffected, urltest carries traffic)."
echo "rotation complete"
echo "  new cert valid until: ${expiry}"
echo "  sha256 fingerprint:   ${fingerprint}"
echo "  backup:               ${BAK_CERT} + ${BAK_KEY}"
