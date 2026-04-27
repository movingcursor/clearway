#!/usr/bin/env bash
# rotate-awg-params.sh — generate fresh AWG obfuscation params, update
# .secrets.yaml, re-render every per-user .conf + the awg-server config,
# restart awg-server.
#
# Why rotate: AWG's Jc/Jmin/Jmax/S1/S2/H1-H4 values are the obfuscation
# fingerprint. A censor that captures enough handshakes to fingerprint
# *this* deployment's specific param tuple can then signature-block it.
# Rotating periodically (and after a confirmed block) gives the deployment
# a fresh obfuscation surface. Suggested cadence: quarterly cron, or
# immediately after observing a regional throughput drop on AWG users.
#
# Cost: every AWG client must re-import their .conf after rotation.
# render.py emits the new .conf into srv/p/<secret>/awg.conf; the per-user
# README links to the URL so refetching is a single tap in the Amnezia VPN
# app — but it's NOT zero-friction (unlike sing-box's hourly poll, the
# Amnezia app doesn't auto-refresh imported .conf files).
#
# Configuration:
#   AWG_SERVER_DIR  Defaults to <script-dir>.
#   PROFILES_DIR    Path to singbox-profiles/. Defaults to a sibling of
#                   AWG_SERVER_DIR (the in-repo layout).
#   NOTIFY          Optional notification script.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AWG_SERVER_DIR="${AWG_SERVER_DIR:-${SCRIPT_DIR}}"
PROFILES_DIR="${PROFILES_DIR:-${AWG_SERVER_DIR}/../singbox-profiles}"
SECRETS="${PROFILES_DIR}/.secrets.yaml"
NOTIFY="${NOTIFY:-}"
DRY_RUN=0

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

notify() {
  if [[ -n "${NOTIFY}" && -x "${NOTIFY}" ]]; then
    "${NOTIFY}" "$1" || true
  else
    echo "$1" >&2
  fi
}

if [[ ! -f "${SECRETS}" ]]; then
  echo ".secrets.yaml not found at ${SECRETS}" >&2
  exit 2
fi

# Generate a fresh tuple via Python (already a render.py dependency, so
# no new tool). Range bounds match docs/quickstart.md's documented snippet
# — keep them in sync. AWG 1.0 baseline; 2.0 adds S3/S4 + I1-I5 fields
# (handled by render.py at template-time when present in the awg block).
new_params=$(python3 - <<'PY'
import secrets
print(f'  Jc: {secrets.randbelow(13)+3}')         # 3..15
print(f'  Jmin: {secrets.randbelow(50)+30}')      # 30..79
# Jmax must be > Jmin; pick from a strictly-higher range.
print(f'  Jmax: {secrets.randbelow(80)+90}')      # 90..169
print(f'  S1: {secrets.randbelow(120)+15}')       # 15..134
print(f'  S2: {secrets.randbelow(120)+15}')
hs = []
while len(hs) < 4:
    h = secrets.randbelow(2_000_000_000) + 5
    # WG's own canonical message-type values are 1..4; AWG's H1-H4 must
    # not collide with them or the handshake reverts to standard WG and
    # the obfuscation does nothing.
    if h not in hs and h not in (1, 2, 3, 4):
        hs.append(h)
for i, h in enumerate(hs, 1):
    print(f'  H{i}: {h}')
PY
)

if [[ ${DRY_RUN} -eq 1 ]]; then
  echo "Would replace awg.{Jc,Jmin,Jmax,S1,S2,H1..H4} in ${SECRETS} with:"
  echo "${new_params}"
  exit 0
fi

# Backup before rewrite. Pattern matches the .secrets.yaml.bak-* gitignore
# so the backup never lands in version control.
ts=$(date -u +%Y%m%dT%H%M%SZ)
cp "${SECRETS}" "${SECRETS}.bak-${ts}"
chmod 600 "${SECRETS}.bak-${ts}"

# In-place rewrite of just the obfuscation params, preserving every other
# line. A python3 driver is more robust than sed here (handles arbitrary
# whitespace + comments around the awg block). Reads the file, replaces
# only the matched keys inside the top-level awg: section, writes back.
python3 - "${SECRETS}" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
# Capture the awg: block (top-level; first non-indented `awg:` ... up to next
# top-level key or EOF). Replace the eight obfuscation keys inside it.
import secrets
def gen():
    yield 'Jc',   secrets.randbelow(13)+3
    yield 'Jmin', secrets.randbelow(50)+30
    yield 'Jmax', secrets.randbelow(80)+90
    yield 'S1',   secrets.randbelow(120)+15
    yield 'S2',   secrets.randbelow(120)+15
    hs = []
    while len(hs) < 4:
        h = secrets.randbelow(2_000_000_000) + 5
        if h not in hs and h not in (1, 2, 3, 4):
            hs.append(h)
    for i, h in enumerate(hs, 1):
        yield f'H{i}', h

m = re.search(r'^(awg:\s*\n(?:[ \t].*\n)*)', text, flags=re.MULTILINE)
if not m:
    sys.exit('rotate-awg-params.sh: awg block not found in .secrets.yaml')
block = m.group(1)
new_block = block
for key, val in gen():
    new_block = re.sub(
        rf'^(\s+){re.escape(key)}:\s*\S+\s*$',
        rf'\g<1>{key}: {val}',
        new_block,
        count=1,
        flags=re.MULTILINE,
    )
text = text[:m.start()] + new_block + text[m.end():]
open(path, 'w').write(text)
print('rewrote awg obfuscation params in', path)
PY

# Re-render every client + the awg-server config. -y skips the apply
# prompt; render.py's auto_yes still walks the rename detector cleanly
# (1:1 renames auto-apply, ambiguous abort).
if ! python3 "${PROFILES_DIR}/render.py" -y; then
  notify "🚫 AWG param rotation: render.py failed after rewriting .secrets.yaml. State on disk: rewritten secrets but stale .conf files. Restore from ${SECRETS}.bak-${ts} and investigate."
  exit 1
fi

# Reconcile awg-server with the new server config.
if ! "${AWG_SERVER_DIR}/safe-restart.sh"; then
  notify "🚨 AWG param rotation: safe-restart.sh failed. New configs are written but awg-server is on the old params. Roll back: \`cp ${SECRETS}.bak-${ts} ${SECRETS} && python3 ${PROFILES_DIR}/render.py -y && ${AWG_SERVER_DIR}/safe-restart.sh\`"
  exit 1
fi

notify "🔄 AWG obfuscation params rotated. AWG users must re-import their .conf from \`https://\${PROFILE_HOST}/p/<secret>/awg.conf\` — old params will fail handshake until they do."
echo "rotation complete; backup at ${SECRETS}.bak-${ts}"
