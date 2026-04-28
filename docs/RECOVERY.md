# RECOVERY.md

**If you're reading this on Google Drive, something went very wrong.**
This is the self-contained runbook to rebuild the household's Docker
stack from scratch using only what's in this `Backups/` folder. It
assumes the original docker-host is gone and you're starting with a
fresh Linux VM.

Last revised 2026-04-24. File lives at `gdrive:Backups/RECOVERY.md`.

---

## Prerequisites

1. **A fresh Linux VM** (Ubuntu 24+ arm64 or amd64). Oracle Cloud
   Always Free ARM (4 OCPU / 24 GB / 50 GB boot) is the original
   setup; anything Docker-capable works.
2. **Docker + Docker Compose v2** installed. `curl -fsSL
   https://get.docker.com | sh` is the quick path.
3. **rclone** authorised to the same Google Drive account that holds
   these backups. The config lives at `~/.config/rclone/rclone.conf`
   and defines a remote called `gdrive:`. If you don't have it, run
   `rclone config` and walk through the Google Drive OAuth flow with
   a web browser; name the remote `gdrive`.
4. **The age private key** (`AGE-SECRET-KEY-...`). You need one of:
   - The paper copy (if the operator printed one).
   - A copy on any machine with gdrive access (`gdrive:Backups/KEYS/backup.key`).
   - The line starting `AGE-SECRET-KEY-` — anywhere you stashed it.

   Without the private key, every daily archive is undecryptable.

5. **sudo** on the VM (most steps need it).

---

## What's in this backup

```
gdrive:Backups/
├── docker-stack/
│   └── docker-stack-YYYY-MM-DD.tar.gz.age   (daily archive, 15d retention)
├── singbox-server/
│   ├── latest/                              (newest sing-box secrets, never pruned)
│   └── YYYY-MM-DD/                          (15d dated history)
├── claude-memory/                           (operator notes)
├── KEYS/
│   └── backup.key                           (age private key)
└── RECOVERY.md                              (this file)
```

The daily archive contains:
- All compose files (`/opt/docker/aio/` + category dirs `security/`, `egress/`, `monitoring/`, `util/`, `misc/`; plus `clearway/` (which now houses `docs/`), `scripts/`, top-level).
- Root `.env` (renamed `docker.env` in the archive).
- Host-side cron scripts (`/home/ubuntu/apps/`).
- Sing-box server configs + TLS keys.
- Selected SQLite databases (*arr libraries, Grafana, nzbdav, etc.).
- Consistent `pg_dump` snapshots of Authelia / Comet / Zilean.
- `crontab -l` output.

---

## Step-by-step recovery

### 1. Install dependencies

```bash
sudo apt update && sudo apt install -y age rclone
# Docker — skip if already installed:
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
# Log out and back in so the docker group membership takes effect.
```

### 2. Authorise rclone to Google Drive (if not already)

```bash
rclone config
# → n (new remote)
# → name: gdrive
# → type: 13 (drive)
# → accept most defaults; authorise in browser
rclone lsf gdrive:Backups/    # should list docker-stack/ KEYS/ etc.
```

### 3. Retrieve the age private key

```bash
mkdir -p ~/.config/age && chmod 700 ~/.config/age
rclone copyto gdrive:Backups/KEYS/backup.key ~/.config/age/backup.key
chmod 600 ~/.config/age/backup.key
age-keygen -y ~/.config/age/backup.key    # should print the public key — sanity check
```

If gdrive's copy is gone (worst case), reconstruct the file manually
from the paper copy:

```bash
cat > ~/.config/age/backup.key <<'EOF'
# created: ...
# public key: age1...
AGE-SECRET-KEY-1...
EOF
chmod 600 ~/.config/age/backup.key
```

The only line that matters for decrypt is the `AGE-SECRET-KEY-...` one.

### 4. Download and decrypt the latest archive

```bash
latest=$(rclone lsf gdrive:Backups/docker-stack/ --include 'docker-stack-*.tar.gz.age' | sort | tail -1)
echo "Using: ${latest}"

rclone copy "gdrive:Backups/docker-stack/${latest}" /tmp/
age -d -i ~/.config/age/backup.key "/tmp/${latest}" > /tmp/archive.tar.gz
```

If `age -d` fails with a decrypt error, the key doesn't match — go
back to step 3 and check the key content.

### 5. Extract into place

```bash
# Extract into scratch first so we can inspect before moving to /opt/docker.
mkdir -p /tmp/rx && cd /tmp/rx
sudo tar -xzpf /tmp/archive.tar.gz
ls docker-stack-backup-*/    # should show aio/ security/ egress/ monitoring/ util/ misc/ clearway/ docker.env data/ home-ubuntu-apps/ scripts/ etc. (docs/ lives inside clearway/)

# Stage into /opt/docker
sudo mkdir -p /opt/docker
sudo cp -a docker-stack-backup-*/aio        /opt/docker/
for cat in security egress monitoring util misc; do
  [ -d "docker-stack-backup-"*/${cat} ] && sudo cp -a docker-stack-backup-*/${cat} /opt/docker/
done
sudo cp -a docker-stack-backup-*/clearway   /opt/docker/   # docs/ travels inside clearway/
sudo cp -a docker-stack-backup-*/scripts    /opt/docker/
sudo cp -a docker-stack-backup-*/docker.env /opt/docker/.env
for f in README.md DONE.md Taskfile.yml PROFILES.md compose.yaml .gitignore; do
  [ -f docker-stack-backup-*/$f ] && sudo cp -a docker-stack-backup-*/$f /opt/docker/
done

# Host-side cron scripts (symlinks into /opt/docker/scripts/ — copy as-is)
cp -a docker-stack-backup-*/home-ubuntu-apps/. ~/apps/
```

### 6. Restore SQLite databases

The archive has per-app SQLite files under
`data/<app>/<file>`. Copy them into place **before** starting the
containers (so they're there on first boot):

```bash
cd /tmp/rx/docker-stack-backup-*/data/

# The layout is: data/<app>/<files>. Mirror to /opt/docker/data/<app>/ with
# correct perms. Only the SQLite-backed apps have entries here; other
# apps are rebuildable (stremthru, xrdb, prowlarr).
sudo mkdir -p /opt/docker/data
sudo cp -a . /opt/docker/data/

# Ownership: most apps expect PUID=1001, but the tarball preserves
# whatever owner was on the source. Verify:
sudo chown -R 1001:1001 /opt/docker/data/{monitoring,radarr,sonarr,aiostreams,aiometadata,aiomanager,nzbdav,beszel,decypharr,uptime-kuma,xrdb,vnstat} 2>/dev/null || true
```

### 7. Bring up Postgres sidecars and restore each dump

Postgres data directories weren't shipped (live pgdata is unsafe to
tar). Instead, the archive has `pg_dump -Fc` snapshots.

```bash
cd /opt/docker

# Start the 3 Postgres containers WITHOUT their parent apps (so the
# dumps can load into empty DBs without the app writing in parallel):
docker compose --profile authelia up -d authelia_postgres
docker compose --profile stremio-addons up -d comet_postgres
docker compose --profile indexing up -d zilean_postgres

# Wait for each to be healthy
for c in authelia_postgres comet_postgres zilean_postgres; do
  until [ "$(docker inspect $c -f '{{.State.Health.Status}}')" = "healthy" ]; do sleep 3; done
  echo "$c: healthy"
done

# Restore each dump
cd /tmp/rx/docker-stack-backup-*/data/pg/
for spec in authelia_postgres:authelia:authelia comet_postgres:comet:comet zilean_postgres:zilean:zilean; do
  IFS=':' read -r container user db <<<"$spec"
  echo "Restoring ${container}..."
  docker exec -i "${container}" pg_restore -U "${user}" -d "${db}" -c < "${container}-${db}.dump"
done
```

### 8. Bring up the rest of the stack

```bash
cd /opt/docker
./scripts/up.sh     # layer-by-layer bring-up (security → clearway/egress → debrid+indexing → arr/media-server → stremio-addons)
```

Wait a couple of minutes, then:

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep -v healthy
# Should be a short list. If everything shows `(healthy)`, you're mostly done.
```

### 9. Restore cron

```bash
crontab /tmp/rx/docker-stack-backup-*/crontab.txt
crontab -l | head    # sanity
```

### 10. Restore sing-box server secrets (bonus path)

If you only need sing-box profiles back (not the whole stack), you
can skip steps 5–9 and pull directly from the sing-box-specific
backup:

```bash
rclone copy gdrive:Backups/singbox-server/latest/ /opt/docker/apps/singbox-profiles/
cd /opt/docker/apps/singbox-profiles && ./render.py -y
```

This reconstitutes `.secrets.yaml`, `profiles.yaml`, Hysteria2 cert + key,
and the pending-rotation state file.

### 11. Smoke test

```bash
# Traefik healthy + serving?
curl -sI https://traefik.dot0.one | head -1

# Authelia responding?
curl -sI https://auth.dot0.one/api/health

# Sing-box clients can fetch their config?
curl -sI https://0.dot0.one/p/<any-user-secret>/singbox-mobile.json

# Any container unhealthy?
docker ps --format '{{.Names}}\t{{.Status}}' | grep -v 'Up.*healthy'
```

If all green, the rebuild is done.

---

## If decryption fails

Symptoms: `age: error: parsing the file: no identity matched any of
the recipients`.

Causes, in order of likelihood:
1. Wrong `backup.key` — confirm it's the one whose public key matches
   `age1...` in the `# public key:` line of the key file. Check
   against the embedded `AGE_RECIPIENT=...` in
   `scripts/backup-streaming-stack.sh` once you've extracted the
   tarball (chicken-and-egg — you'll need the old .age file already
   decrypted OR a git clone of `ericguichard/oracle-docker`).
2. The key was rotated and only newer archives will decrypt. Try a
   more recent archive; oldest archives may be encrypted to a
   superseded pubkey.
3. The .age file itself is corrupt. `rclone` usually catches this via
   hash mismatches, but if it happened, try the prior day.

Nuclear option: if you have GitHub access, `git clone
git@github.com:ericguichard/oracle-docker.git /opt/docker` brings
back everything except the `.env` secrets (CF token, Authelia keys,
Grafana password) and the pg-dump / SQLite runtime state. You'd then
need to regenerate those secrets and start fresh on the databases.

---

## Verifying this runbook actually works

The monthly restore drill
(`/opt/docker/scripts/restore-drill.sh`, cron day 3 at 09:15 UTC)
exercises most of this path on the live host without overwriting
anything — download, decrypt, extract, spot-check file presence +
pg_restore validity. If the most recent Discord message from
`restore-drill` was a ✅, the tarball is known-good.

If it was ⚠️ or ❌, read the specific issue list before trusting the
archive.

---

## Post-recovery: verify the backup still works on the new host

Once the stack is running again:

```bash
# Run one backup cycle manually to confirm credentials + cron are OK
/home/ubuntu/apps/backup-postgres-dumps.sh
/home/ubuntu/apps/backup-streaming-stack.sh
# Check for the new archive
rclone lsf gdrive:Backups/docker-stack/ | sort | tail
```

If you see the new archive landed, cron will pick up from here on.

---

## Files you'll want to keep safe for next time

- **Paper copy of `AGE-SECRET-KEY-...`** — the line the whole recovery
  depends on.
- **rclone remote config** (`~/.config/rclone/rclone.conf`) — not
  strictly required (you can re-authorise), but saves the OAuth
  dance.
- **Your Oracle Cloud account recovery details** — if the VM image
  is gone and you also lost access to OCI console, the block volume
  snapshot of swap etc. won't help you.
