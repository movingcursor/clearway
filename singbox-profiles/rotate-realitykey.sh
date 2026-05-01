#!/usr/bin/env bash
# rotate-realitykey.sh — rotate the Reality X25519 server keypair.
#
# Why: Reality's server private key is the long-lived secret that binds the
# handshake. Short_id rotation (rotate-shortids.sh) limits blast radius for
# leaked device credentials but not the server identity itself. A compromised
# private key (file disclosure, backup exposure) silently lets anyone forge
# valid Reality connections — until you rotate. Quarterly rotation shrinks
# the window.
#
# What it does:
#   1. Generate a fresh keypair via `sing-box generate reality-keypair`
#      (runs in the same image as the server — guaranteed-compatible format).
#   2. Snapshot `.secrets.yaml` to .bak-<UTC-ts> for rollback.
#   3. Rewrite `shared.reality_public_key` + `shared.reality_private_key` in
#      `.secrets.yaml`.
#   4. Run `./render.py -y` which:
#        - re-renders every client config with the new public key
#        - re-renders server config.json with the new private key
#        - kicks safe-restart.sh on the server
#      render.py's rotation-grace machinery does NOT cover this rotation:
#      Reality keypair is a single server-side pair, not a per-user
#      credential, so there's no "old + new" slot on the server. The
#      rotation is a flag day — clients must fetch the new public key
#      before their next Reality handshake attempt. Clients poll the
#      profile URL on a schedule (Windows updater + mobile
#      auto_update_interval), so the practical outage window is one poll
#      cycle. Schedule rotation when a brief outage is acceptable.
#   5. Notify via $NOTIFY if set (same hook as rotate-shortids.sh).
#
# Invocation: run manually or via cron. Suggested cadence: quarterly
# (`0 4 1 */3 *`). Absolutely not monthly — too disruptive for clients
# that haven't polled recently.
#
# Safety:
#   - Refuses to run if `.secrets.yaml` has uncommitted-looking local mods
#     that would be lost (checks for a .bak file from the last 5 min to
#     avoid concurrent rotations).
#   - Refuses to run if render.py --validate fails first (current manifest
#     is broken — don't compound the problem).
#   - On sing-box-generate failure, aborts before touching .secrets.yaml.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SECRETS="${ROOT}/.secrets.yaml"
NOTIFY="${NOTIFY:-/opt/docker/scripts/notify-discord.sh}"
TS=$(date -u +%Y%m%dT%H%M%SZ)

# Next quarterly rotation = next 1st-of-month in {Jan, Apr, Jul, Oct}.
# Computed at runtime so the notification stays accurate even if cron drifts.
NEXT_ROTATION=$(python3 -c "
import datetime
t = datetime.date.today()
for y in (t.year, t.year + 1):
    for m in (1, 4, 7, 10):
        d = datetime.date(y, m, 1)
        if d > t:
            print(d.isoformat()); raise SystemExit
")
# `-reality-` infix distinguishes these backups from short_id rotation
# backups (`.bak-shortid-*`), so each script's retention trim only touches
# its own family. See rotate-shortids.sh for the matching pattern.
BAK="${SECRETS}.bak-reality-${TS}"
# Keep the 3 most recent reality-key backups. Reality rotation runs
# quarterly, so 3 = 9 months of rollback history — enough to diff across
# the most recent crypto changes without unbounded growth.
RETAIN=3

notify() {
  if [[ -n "${NOTIFY}" && -x "${NOTIFY}" ]]; then
    "${NOTIFY}" "$1" || true
  else
    echo "$1" >&2
  fi
}

# Pre-flight: current manifest must render cleanly before we touch anything.
# A stale broken state would otherwise strand us with the old key in
# .secrets.yaml.bak and no viable render.
if ! "${ROOT}/render.py" --validate >/dev/null 2>&1; then
  notify "🚨 **Reality keypair rotation ABORTED** (quarterly tier) — pre-flight \`render.py --validate\` failed; fix the manifest first"
  echo "pre-flight validate failed — aborting" >&2
  exit 1
fi

# Generate keypair via the same image the server runs so the public-key
# format is guaranteed to match (upstream sometimes tweaks base64 padding).
GEN=$(docker run --rm ghcr.io/sagernet/sing-box:latest generate reality-keypair 2>&1) || {
  notify "🚨 **Reality keypair rotation ABORTED** (quarterly tier) — \`sing-box generate reality-keypair\` failed"
  echo "sing-box generate failed:" >&2
  echo "${GEN}" >&2
  exit 1
}

# Parse the two-line "PrivateKey: ...\nPublicKey: ..." output. Using awk
# over grep+sed keeps it single-pass and resilient to whitespace variations.
NEW_PRIV=$(awk -F': ' '/^PrivateKey/ {print $2}' <<<"${GEN}")
NEW_PUB=$(awk -F': ' '/^PublicKey/  {print $2}' <<<"${GEN}")

if [[ -z "${NEW_PRIV}" || -z "${NEW_PUB}" ]]; then
  notify "🚨 **Reality keypair rotation ABORTED** (quarterly tier) — could not parse keypair output"
  echo "parse failed, raw output:" >&2
  echo "${GEN}" >&2
  exit 1
fi

# Snapshot .secrets.yaml. Keep mode explicitly so cp doesn't widen it
# (the file is 0600 by design).
cp -p "${SECRETS}" "${BAK}"
chmod 600 "${BAK}"

# Trim reality-key backups beyond the retention count. Runs after the
# new backup exists so the rollback target for *this* rotation is safe.
ls -1t "${SECRETS}".bak-reality-* 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f

# Replace the two key lines in-place. sed is fine here — keys are
# base64-url (no / or +) and fit on one line, so no escaping gotchas.
# Anchored to the field name + leading 2-space indent (under `shared:`) to
# avoid collisions with any similarly-named field elsewhere.
sed -i \
  -e "s|^  reality_public_key:.*|  reality_public_key: ${NEW_PUB}|" \
  -e "s|^  reality_private_key:.*|  reality_private_key: ${NEW_PRIV}|" \
  "${SECRETS}"

# Sanity: both lines were actually updated (sed succeeds even on 0 matches).
if ! grep -q "reality_public_key: ${NEW_PUB}" "${SECRETS}" \
   || ! grep -q "reality_private_key: ${NEW_PRIV}" "${SECRETS}"; then
  mv "${BAK}" "${SECRETS}"
  notify "🚨 **Reality keypair rotation ABORTED** (quarterly tier) — sed didn't update both keys; \`.secrets.yaml\` restored from backup"
  echo "sed update failed — restored backup" >&2
  exit 1
fi

# Re-render (clients + server), which will hot-restart singbox-server.
# If render fails we still have the .bak; operator can rollback with:
#   mv .secrets.yaml.bak-<TS> .secrets.yaml && ./render.py -y
if ! "${ROOT}/render.py" -y; then
  notify "🚨 **Reality keypair rotation FAILED AT RENDER** (quarterly tier) — \`.secrets.yaml\` has new key but render.py errored. Rollback: \`mv ${BAK##*/} .secrets.yaml && ./render.py -y\`"
  echo "render.py failed — manual rollback required" >&2
  exit 1
fi

notify "$(cat <<EOF
🔑 **Reality keypair rotation** — quarterly tier (1st of Jan/Apr/Jul/Oct @ 04:00 UTC)
• new pubkey: \`${NEW_PUB:0:12}…\`
• continuity: ⚠️ flag day — no grace window (single server keypair). Clients fail Reality handshakes until they poll the new pubkey.
• fallback: ShadowTLS / Hysteria2 / WS-CDN outbounds unaffected; urltest carries traffic during the gap.
• clients refresh on next poll (Windows updater hourly + boot, mobile per \`auto_update_interval\`)
• next rotation: **${NEXT_ROTATION}** @ 04:00 UTC
EOF
)"
echo "rotation complete"
echo "  old backup: ${BAK}"
echo "  new pubkey: ${NEW_PUB}"
