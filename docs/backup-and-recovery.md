# Backup and recovery

Last revised 2026-04-24.

## Split: what lives where

The household uses two cloud remotes. They serve deliberately different
purposes:

| Remote | Role | What's in it | Retention |
| --- | --- | --- | --- |
| `gdrive:` (Google Drive) | **Backup home.** Unlimited space, not user-facing. | Everything under `gdrive:Backups/` (docker stack, sing-box secrets, Claude memory, age key). | 15 days on daily archives + dated sing-box snapshots; `latest/` + claude-memory kept forever. |
| `onedrive:` (OneDrive) | **User-shared drop.** Regular people navigate to it. | Only `onedrive:Desktop/Configs/<user>/` — per-user VPN configs, installers, READMEs. | N/A (live synced from the server). |

Rule of thumb: if a human end-user needs to read it, OneDrive.
Otherwise, gdrive.

## Google Drive structure

```
gdrive:Backups/
├── docker-stack/
│   └── docker-stack-YYYY-MM-DD.tar.gz.age   (one per day, age-encrypted)
├── singbox-server/
│   ├── latest/                              (always the newest snapshot)
│   └── YYYY-MM-DD/                          (dated snapshots, 15d retention)
├── claude-memory/                           (markdown memory files, no pruning)
│   ├── MEMORY.md
│   └── <feedback|project|reference|user>_*.md
└── KEYS/
    └── backup.key                           (age private key, 184 B)
```

## OneDrive structure (user-shared)

```
onedrive:Desktop/Configs/
├── <user>/
│   ├── awg-mobile.conf       (AmneziaWG mobile — iOS AmneziaVPN app / Android)
│   ├── awg-pc.conf           (AmneziaWG desktop — optional, per user)
│   ├── awg-laptop.conf       (same)
│   ├── singbox-mobile.json   (sing-box mobile config — bootstrap copy, live version served from docker host)
│   ├── singbox-windows.json  (sing-box Windows config — bootstrap only)
│   ├── install-singbox.ps1   (one-shot Windows installer, baked with per-user secret)
│   ├── README.md             (per-user instructions in their language)
│   └── README-amnezia-*.md   (AmneziaVPN-specific instructions)
```

Users navigate to their own subfolder via OneDrive sharing links. The
live config is served from the docker host at
`https://0.dot0.one/p/<secret>/singbox-mobile.json`; the OneDrive copy
is a bootstrap (clients re-import from the URL at their configured
auto-update interval).

## What gets backed up

### Daily encrypted archive (`docker-stack-YYYY-MM-DD.tar.gz.age`)

Produced by `/opt/docker/scripts/backup-streaming-stack.sh` at 08:00 UTC.
Staged, tarred, age-encrypted, uploaded. Contents:

| Include | Reason |
| --- | --- |
| `/opt/docker/aio/` + category dirs | All compose files + `.env` per app + bind-mounted scripts + configs. As of 2026-04-28 services are split across `aio/` (media stack: arr, debrid, indexing, stremio-addons, media-server) and `security/` `egress/` `monitoring/` `util/` `misc/`. Backup captures all six. |
| `/opt/docker/clearway/docs/` | Operator docs (including this file). Travel with clearway/ in the tarball. (Lived at `/opt/docker/docs/` until 2026-04-28.) |
| `/opt/docker/scripts/` | Stack-wide infra scripts. Same rationale. |
| `/opt/docker/{README.md,DONE.md,Taskfile.yml,PROFILES.md,compose.yaml,.gitignore}` | Top-level repo files. Same rationale. |
| `/opt/docker/.env` | Root env — CF DNS API token, Authelia encryption key, Grafana admin pw, OCI creds. |
| Selected small SQLite DBs | *arr stack libraries, aiometadata, kuma, nzbdav, beszel, vnstat, grafana, decypharr, xrdb. See the `copy_db` calls in the script for the exact list. |
| Grafana OCI API private key (`oci_api_key.pem`) | Required by the `oci-metrics-datasource` Grafana plugin. |
| Postgres dumps (authelia / comet / zilean) | Produced 10 min earlier by `backup-postgres-dumps.sh`. Custom format (`pg_dump -Fc`). |
| `/home/ubuntu/apps/` | Host-side cron scripts. Without these a bare-metal restore can't re-establish the backup flow itself. (Most are symlinks into `/opt/docker/scripts/` covered above; the symlinks themselves are in this tree.) |
| `crontab -l` output | Re-establishes the cron schedule on a fresh host. |

Intentional skips (rebuildable on demand, too large to store daily):
`stremthru.db`, `xrdb.db`, `prowlarr.db`, `*log*.db` files, `/mnt/` media
library, docker image layers.

### Sing-box server secrets (separate job)

Produced by `/opt/docker/apps/singbox-server/backup-secrets.sh` at
03:15 UTC. Pushes `.secrets.yaml`, `profiles.yaml`, `secrets.txt`,
`.pending-rotations.yaml`, `hy2.crt/.key` to
`gdrive:Backups/singbox-server/{latest, YYYY-MM-DD}/`. 15-day retention
on dated copies (aligned with stack-wide policy 2026-04-24; `latest/`
is always the newest and is never pruned).

Why a separate job: these are the single most critical files for
regenerating every user's profile. A second independent backup path
narrows the window where a main-backup failure could lose them.

### Claude memory (separate sync)

Produced at the end of `backup-streaming-stack.sh` via `rclone sync
~/.claude/projects/-home-ubuntu/memory → gdrive:Backups/claude-memory/`.
No pruning — memory files are tiny (<50 KB total today) and are the
institutional knowledge of prior sessions.

## Schedule

```
Daily (UTC):
  03:15   sing-box secrets    → gdrive:Backups/singbox-server/
  07:50   postgres dumps      → /opt/docker/data/_backups/pg/ (host only)
  08:00   main backup         → gdrive:Backups/docker-stack/
          claude memory sync  → gdrive:Backups/claude-memory/

Monthly (UTC):
  day 3 @ 09:15  restore drill → verify latest archive, post Discord
```

## Encryption: age

All daily archives are encrypted with
[age](https://github.com/FiloSottile/age) before upload. One recipient
pubkey, one private key. OneDrive-at-rest / gdrive-at-rest encryption
is NOT sufficient — a compromised cloud account would expose every
secret bundled in the tarball.

- **Public key** (embedded in `backup-streaming-stack.sh`, safe to commit):
  `age145mtrz9wskl6z8sa3w4q9pnas89wfc4u9u2gl5artst8yc9yh55snserjf`

- **Private key** — three copies:
  1. `~/.config/age/backup.key` on the docker host (0600). Used by
     `restore-drill.sh` for the monthly check and by any interactive
     restore run from the host.
  2. `gdrive:Backups/KEYS/backup.key`. Recovery copy for when the
     host is destroyed — download with any Google account that has
     access to the Backups folder.
  3. **Paper copy.** User's physical responsibility. Last line of
     defense if both the host and the Google account are lost.

**Losing all three** means every archive becomes undecryptable
ciphertext. No recovery, no workaround.

## Rotating the age key

Only needed if the key is suspected compromised (e.g. laptop stolen
while key present). Not a routine action.

```bash
# 1. Generate new keypair
age-keygen -o /tmp/new.key
cat /tmp/new.key  # note the public key

# 2. Update backup-streaming-stack.sh AGE_RECIPIENT to the new public key
# 3. Update all three private-key copies
cp /tmp/new.key ~/.config/age/backup.key
chmod 600 ~/.config/age/backup.key
rclone copyto ~/.config/age/backup.key gdrive:Backups/KEYS/backup.key
# Print + paper copy the new key; destroy the old paper copy.

# 4. Decrypt old archives + re-encrypt with new key (optional — only if
#    you want the historical archives to stay recoverable under the
#    new key). Otherwise leave them under the old key and archive the
#    old private key somewhere offline.
```

## Recovery runbook

### Recovering a single file (most common)

```bash
# 1. Grab latest archive
rclone copy gdrive:Backups/docker-stack/docker-stack-YYYY-MM-DD.tar.gz.age /tmp/

# 2. Decrypt
age -d -i ~/.config/age/backup.key /tmp/docker-stack-YYYY-MM-DD.tar.gz.age \
    > /tmp/archive.tar.gz

# 3. Extract into scratch
mkdir /tmp/rx && cd /tmp/rx && sudo tar -xzpf /tmp/archive.tar.gz

# 4. Cherry-pick the file you wanted out of /tmp/rx/docker-stack-backup-*/...
```

### Full bare-metal rebuild

```bash
# 0. Fresh Oracle Cloud ARM VM, Ubuntu 24+, Docker installed (see
#    /opt/docker/README.md), rclone authorised to the same gdrive account.

# 1. Install age, pull the private key
sudo apt install age
mkdir -p ~/.config/age && chmod 700 ~/.config/age
rclone copyto gdrive:Backups/KEYS/backup.key ~/.config/age/backup.key
chmod 600 ~/.config/age/backup.key

# 2. Download latest archive + decrypt
cd /tmp
rclone copyto gdrive:Backups/docker-stack/$(rclone lsf gdrive:Backups/docker-stack | sort | tail -1) /tmp/archive.tar.gz.age
age -d -i ~/.config/age/backup.key /tmp/archive.tar.gz.age > /tmp/archive.tar.gz
sudo tar -xzpf /tmp/archive.tar.gz -C /tmp/

# 3. Restore compose + env
sudo mkdir -p /opt/docker
sudo cp -a /tmp/docker-stack-backup-*/apps /opt/docker/
sudo cp /tmp/docker-stack-backup-*/docker.env /opt/docker/.env

# 4. Restore SQLite databases
sudo mkdir -p /opt/docker/data/monitoring /opt/docker/data/radarr ... # etc
# Then per the data/ subtree of the extracted tarball; see backup-streaming-
# stack.sh's `copy_db` list for the exact destinations.

# 5. Bring up Postgres sidecars empty, then pg_restore each dump:
cd /opt/docker && docker compose --profile authelia up -d authelia_postgres
for spec in authelia_postgres:authelia:authelia comet_postgres:comet:comet zilean_postgres:zilean:zilean; do
  IFS=':' read -r container user db <<<"$spec"
  cd /opt/docker && docker compose up -d "$container"
  sleep 10  # wait for pg ready
  docker exec -i "$container" pg_restore -U "$user" -d "$db" -c \
    < /tmp/docker-stack-backup-*/data/pg/"${container}-${db}.dump"
done

# 6. Bring up the rest of the stack layer-by-layer
/opt/docker/scripts/up.sh

# 7. Restore cron
cat /tmp/docker-stack-backup-*/crontab.txt | crontab -

# 8. Restore host-side scripts (most are symlinks into /opt/docker/scripts/)
cp -a /tmp/docker-stack-backup-*/home-ubuntu-apps/. ~/apps/
```

### Recovering from sing-box-secrets backup (faster than full restore)

If only the sing-box profiles secrets are lost (e.g. accidental delete
of `.secrets.yaml`):

```bash
# Pull from the latest copy directly
rclone copy gdrive:Backups/singbox-server/latest/ /opt/docker/apps/singbox-profiles/
cd /opt/docker/apps/singbox-profiles && ./render.py -y
```

## Restore drill

`/opt/docker/scripts/restore-drill.sh` runs monthly on day 3 at 09:15 UTC
and performs:

1. Download latest `*.tar.gz.age` from gdrive.
2. `age -d` decrypt.
3. `tar -tzf` integrity check + full extract to `/tmp/`.
4. Spot-checks:
   - Load-bearing files present + non-zero (compose.yaml, .secrets.yaml,
     hy2.key, each pg dump, a few SQLite DBs, `oci_api_key.pem`, the
     backup scripts themselves, `crontab.txt`).
   - `pg_restore -l` succeeds on each `.dump` file (uses a throwaway
     `postgres:17-alpine` container — structural validity only, no
     actual restore).
   - `docker.env` shape-check for well-known keys present.
5. Discord notify: pass with archive size + file count, or fail with
   the specific issue list.

What this catches:
- age key drift (can't decrypt → immediate alert).
- Corrupted gdrive upload (tar fails to extract).
- Missing expected files (script layout regressions).
- Postgres dump structural corruption.
- `.env` rotation dropping a required key.

What this does NOT catch:
- Semantic regression (e.g. the config is syntactically valid but
  references a container that no longer exists). Would require a
  full apply+healthcheck test, which is too heavy for monthly.

## Open items (future improvements, not urgent)

None of these are blocking; L1 (pg dumps) + L2 (age encryption) + L6
(restore drill) cover the critical failure modes. Everything below is
"nice to have":

- **Retention ladder beyond 15 days** — daily archives currently
  prune at 15d. If longer history is wanted (e.g. "what was the
  config 3 months ago?"), add a second cron that marks e.g. the
  first-of-month archive with a name like `docker-stack-YYYY-MM-01-monthly.tar.gz.age`
  and excludes that pattern from the 15d prune. Drop-in change; not
  needed until a real use-case surfaces.
- **Second destination mirror** — currently all backups on gdrive. A
  monthly mirror to a second cloud (S3/Backblaze/OneDrive with sufficient
  quota) would survive a gdrive account loss. Adds cost.
- **Local cold copy on `/dev/sdb`** — the 46 GB unpartitioned tail on
  the OCI block volume could hold last-14-day archives locally for
  "network is down" recovery. Would need a filesystem + mount + cron
  rsync step.
- **OCI native block-volume snapshot** — filesystem-consistent whole-
  boot snapshots, free tier. Good for "nuked the VM" scenarios.
  Complements rather than replaces the tarball backup.
- **Semantic drill** — a quarterly test that actually applies the
  restored config in a scratch VM + validates each container reaches
  `healthy`. Catches config-breaks that the current spot-check drill
  misses.

## References

- `/opt/docker/scripts/README.md` — script catalog (one-liner per script).
- `/opt/docker/apps/README.md` — app-specific scripts catalog.
- `/opt/docker/clearway/docs/discord-notifications.md` — notification inventory.
- Memory: `project_backup_strategy_2026_04_24.md` — condensed version
  of this doc for future-session context pickup.
