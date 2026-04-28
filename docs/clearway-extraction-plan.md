# Clearway — extraction plan

Goal: extract `/opt/docker/apps/singbox-profiles/` + `/opt/docker/apps/singbox-server/` into a single public GitHub repo under the name **Clearway**. Real household data stays out (gitignored); the public repo ships anonymized examples.

Working-name rationale and naming alternatives discussed 2026-04-24 in session.

---

## Open items

### Stage 1 — Draft scratch tree (no GitHub push yet)

- [x] Create scratch dir `/tmp/clearway-scratch/` with the target layout.
- [x] Scrub `render.py`:
  - [x] `PROFILE_HOST` → env var / config (`os.environ.get('PROFILE_HOST', 'profile.example.com')`)
  - [x] Reality `handshake_sni` default → env var with doc on choosing a plausible cover SNI (now lives entirely in `defaults.reality.handshake_sni`; `profiles.example.yaml` documents how to pick one)
  - [x] Any remaining household hostnames / IPs / usernames → config-driven (`PROXY_SERVER_IPS` → `defaults.proxy_server_ips`, `bootstrap_domains` derived from `defaults.ws_cdn.host`, `/opt/docker/...` → `SERVER_DIR` env-var-or-sibling, `vpnws.dot0.one`/`cloud.oracle.com`/`132.226.x` etc. all gone)
  - [x] Dropped `sync_onedrive` entirely (household-specific, not core)
- [x] Write `profiles.example.yaml` covering three archetypes:
  - [x] `alice` — admin, multi-country traveller, all 4 protocols, home-egress
  - [x] `bob` — single-country resident, 2 protocols, no home
  - [x] `carol` — single-country resident, Windows + mobile, shadowtls_sni override
- [x] Write `home_wg/README.md` explaining the `.conf` format without shipping any real key.
- [x] Scrub `singbox-server/compose.yaml`:
  - [x] VNIC IP bindings (`10.0.0.220`) → env vars (kept `${VNIC_SECONDARY_IP}` reference, scrubbed comments)
  - [x] Traefik hostnames → env vars (`${SINGBOX_SERVER_DIR}` for bind mounts; CDN-front hostname now `defaults.ws_cdn.host`)
- [x] Scrub `singbox-server/config.json` template — strip Oracle-specific comments, keep architecture.
- [x] Keep: `safe-restart.sh`, `rotate-hy2-cert.sh`, `bump-singbox-image.sh` (all rewritten with env-driven config + NOTIFY hook).
- [x] Drop from `singbox-server/`: `banscan.sh`, `daily-report.sh`, `config-watch.sh`, `config-drift.sh`, `prom-alerts.sh`, `backup-secrets.sh`, `check-health.sh`, `publish-version.sh` (household ops, not renderer core). Not copied to scratch.
- [x] Keep: `singbox-profiles/generate-installer.sh`, `rotate-short-ids.sh`, `rotate-reality-key.sh` (also rewritten with `${PROFILES_DIR}` + NOTIFY hook).
- [x] Tests: verify `tests/test_render.py` passes against the scrubbed `render.py` with `tests/fixtures/` — fixtures renamed `test_eric/test_resident/test_nomad` → `test_alice/test_bob/test_carol` and IPs/hostnames swapped to RFC5737 / example.com.
- [x] Regenerate golden files (4/4 pass deterministically).
- [x] Add `.gitignore` covering live state (profiles.yaml, .secrets.yaml, srv/, hy2.{crt,key}, home_wg/*.conf, etc.).

### Stage 2 — Docs

- [x] `README.md`: project intro, problem statement, quickstart link, example rendered output, golden-tests-pass pointer.
- [x] `docs/quickstart.md`: VPS prerequisites → openssl hy2 cert → edit `profiles.yaml` → `./render.py` → reverse-proxy + verify → onboarding + rotation cron.
- [x] `docs/architecture.md`: fragment composition, manifest trio (profiles.yaml/.secrets.yaml/home_wg), 2h rotation grace, SS-2022 multi-user EIH, renderer vs server split, server-template substitution, schema policy, tests.
- [x] `docs/hazards.md`: 9 hazards covering all 8 plan items plus sing-box ANSI colors. Each entry: symptom → why → fix.
  - [x] hy2 cert needs SAN (not just CN)
  - [x] ShadowTLS mobile clients break after 1 probe with pooled SNI
  - [x] smux over VLESS+WS through Cloudflare is broken
  - [x] sing-box 1.12+ rejects detour to bare direct outbound
  - [x] bind-mount inode pinning after in-place cert rotation (single-file bind mounts)
  - [x] TUN `auto_route + strict_route` captures hy2's own QUIC egress
  - [x] sing-box has no per-user hysteria2 bandwidth caps
  - [x] Android Private DNS → DoT leak; must reject TCP/853 in route rules
  - [x] sing-box ANSI color logs (bonus — strip downstream, not at source)
- [x] `LICENSE`: AGPL-3.0 (canonical text from gnu.org, 661 lines).

### Stage 3 — Publish

- [x] Create `github.com/movingcursor/clearway` (public). Live at https://github.com/movingcursor/clearway. (Originally pushed to `ericguichard/clearway` 2026-04-25 morning; re-homed to the `movingcursor` org same day before the announcement went out — old repo deleted, no redirect needed since the URL was never advertised.)
- [x] Push scratch tree as first commit. `f0aa3de Initial commit` — 29 files, 7896 insertions.
- [x] Leak-scan `git log -p --all` against the plan's pattern set. **Caught 1 real leak** (eric's path-secret in the PowerShell template's example URL comment) before pushing — fixed in scratch + commit amended; production template also scrubbed. Wider trail discovered: same hex was copied verbatim into every household member's rendered `install-singbox.ps1` since the comment block isn't substituted by `generate-installer.sh`. **Decisions still pending** for the user: re-render production now (purges hex from rendered installers) and/or rotate eric's path-secret. Captured below in Open questions.
- [x] Add topics + description. Topics: sing-box, vpn, censorship-circumvention, reality, hysteria2, shadowtls, vless, wireguard. Description: "Multi-user, multi-protocol sing-box config generator + server stack for households and small teams in restrictive networks."
- [ ] Pin README sections — GitHub doesn't have a "pin sections" feature; the README's TOC + the `## Documentation` section already serves this purpose. Skipped unless you want a separate "About" sidebar or pinned issue.

### Stage 4 — Migrate the docker host to use the public repo

- [x] Clone the public repo to a side path. Cloned to `/opt/docker/apps/clearway-staging/`, then renamed to `/opt/docker/apps/clearway/` post-swap. (Origin remote later updated to `movingcursor/clearway` when the repo was re-homed.)
- [x] Copy real `profiles.yaml`, `.secrets.yaml`, `home_wg/*.conf`, `srv/p/*/`, `hy2.crt`, `hy2.key` into the clone.
- [x] Run `./render.py` end-to-end — server JSON content byte-identical (md5 match), only doc-comment diffs in template; client diffs were 5 README cosmetic ("(Oracle Cloud)" annotation removed). Caught one missing piece: `defaults.proxy_server_ips` had to be added to manifest since the new render reads it from manifest instead of hardcoding (load-bearing per hazards #6).
- [x] Swap `/opt/docker/apps/singbox-{profiles,server}` → symlinks into the clearway clone:
   - `/opt/docker/apps/clearway/` (full clone, .git intact)
   - `/opt/docker/apps/singbox-profiles → clearway/singbox-profiles`
   - `/opt/docker/apps/singbox-server   → clearway/singbox-server`
   - Old trees preserved at `*.bak-2026-04-25` for rollback.
   - Household-only ops scripts (banscan, daily-report, etc.), state files, nginx compose.yaml restored alongside cloned tree, hidden via `.git/info/exclude` (12 entries).
- [x] Verify every client still fetches correctly: `curl -sk https://0.dot0.one/p/$secret/README.md` → 200 for all 5 users; `singbox-mobile.json` parses as JSON with 15 outbounds.
- [x] Update `/opt/docker/.env`: added `SINGBOX_SERVER_DIR=/opt/docker/apps/singbox-server` (the new compose.yaml uses this var) and `PROFILE_HOST=0.dot0.one` (read by render.py).
- [x] `/opt/docker/compose.yaml` include paths: NO change needed — `apps/singbox-{profiles,server}/compose.yaml` paths resolve through the symlinks.
- [x] Tests in new location: `/opt/docker/apps/clearway/singbox-profiles && python3 tests/test_render.py` → 4/4 passing.
- [ ] Remove the now-orphan files from the private `oracle-docker` repo: NOT done yet — the bak-* dirs are still present. Decide later: remove (smaller footprint), or keep as a permanent rollback point. The clearway clone's `.git/info/exclude` and `.gitignore` cover the household-private overlay either way.

### Stage 5 — Go-live polish

- [x] Add GitHub Actions CI: run golden tests on every push. Workflow at `.github/workflows/test.yml`. First run completed in 11s, all 4 goldens passed. Badge on README.
- [x] Add a short "before you deploy" security-posture checklist. Wrote `docs/hardening.md` — checklist (not tutorial) covering SSH hardening, cloud firewall, host firewall, unattended-upgrades, secrets backup, image bumps, things explicitly out of scope. Cross-linked from README + quickstart.
- [ ] Social post: draft saved at `/tmp/clearway-announce.md` — short (Mastodon/Twitter/Bluesky) and long (HN/Reddit/forums) variants, plus posting notes. Eric to send when he's ready.

---

## Done

- 2026-04-25 (later, post-Stage-5): Moved the on-disk clone from
  `/opt/docker/apps/clearway/` up one level to `/opt/docker/clearway/`.
  Reason: clearway is its own project (its own README/LICENSE/CI/tests +
  multiple Docker apps), not a single Docker app, so it reads more
  honestly as a top-level peer of `docs/` and `scripts/` than as one
  of ~50 single-app dirs under `apps/`. Two symlinks repointed:
  `apps/singbox-profiles` and `apps/singbox-server` now use `../clearway/...`
  instead of `clearway/...`. `.env`, master `compose.yaml`, and all
  cron/scripts kept the old `apps/singbox-*` paths (they go through the
  symlinks), so production was unaffected. Tests 4/4 pass; container
  config reads cleanly post-move.
- 2026-04-25 (later): Re-homed public repo from `ericguichard/clearway`
  to `movingcursor/clearway`. New repo created under the `movingcursor`
  GitHub account, local origin remote updated to
  `git@github-movingcursor:movingcursor/clearway.git` (SSH alias),
  README badge URLs already pointed at movingcursor in the new push,
  old `ericguichard/clearway` deleted (0 stars / 0 forks / 0 issues —
  never had outside engagement, no redirect needed). Local working
  tree clean and in sync with the new origin.
- 2026-04-25: Stage 5 partial. CI + hardening doc shipped:
  - `.github/workflows/test.yml` runs the golden-test suite on every
    push to main and every PR. First run: 11s, ✓ 4/4 goldens.
  - `docs/hardening.md` — pre-deploy security checklist for the host
    (SSH, cloud firewall, host firewall, unattended-upgrades, secrets
    backup, image bumps cadence). Each item links to a canonical
    reference. Cross-linked from README + quickstart.
  - README badges (tests + license).
  Pushed as commit `1065515 Add CI + hardening checklist`. CI green.
  Announcement post still up to Eric to send (draft at
  `/tmp/clearway-announce.md`).
- 2026-04-25: Stage 4 complete. Production singbox stack now runs from the
  clearway clone via symlinks. `/opt/docker/apps/clearway/` holds the git
  repo + the singbox-profiles/ + singbox-server/ subdirs; the old paths
  `/opt/docker/apps/singbox-{profiles,server}` are now symlinks into the
  clone. Old trees preserved at `*.bak-2026-04-25/` for rollback. Master
  `compose.yaml` include paths unchanged (resolve through symlinks). Two
  new env vars in `/opt/docker/.env`: `SINGBOX_SERVER_DIR` + `PROFILE_HOST`.
  Manifest got one new field: `defaults.proxy_server_ips` (load-bearing
  per hazards #6 — the new render reads it from manifest, old hardcoded).
  All 3 services (singbox-server, singbox-profiles, singbox-exporter)
  healthy. All 5 user URLs return 200. End-to-end JSON parse OK.
- 2026-04-25: Stage 3 complete. Public repo live at https://github.com/ericguichard/clearway.
  Initial commit `f0aa3de`, 29 files, 7896 insertions. Topics + description set.
  **Leak found and fixed pre-publish**: eric's path-secret `7085a060...` was in the
  PowerShell template's example URL comment — caught by the plan-spec leak-scan
  against `git log -p --all`, scrubbed in scratch via `--amend`, mirrored to the
  production template at `/opt/docker/apps/singbox-profiles/templates/install-singbox.template.ps1`.
  Wider production trail found (same hex in every household member's rendered
  installer) — open question carried to Open Questions section above.
- 2026-04-25: Stage 2 complete. Added README.md, LICENSE (AGPL-3.0),
  docs/quickstart.md, docs/architecture.md, docs/hazards.md to scratch
  tree. 29 files total, leak-scan still clean, tests still 4/4 passing.
  - README.md: project intro + status + tree-view + quickstart pointer +
    threat model + license + contributing notes.
  - quickstart.md: 30-min walkthrough from a fresh VPS — prerequisites,
    .env, openssl hy2 cert, manifest edit, first render, reverse-proxy
    wiring, healthcheck verification, user onboarding, rotation cron.
  - architecture.md: the two halves diagram, manifest trio, dict-merge
    composition model, server template, 2h rotation grace, SS-2022 EIH,
    PROFILE_HOST flow, schema-update policy, testing.
  - hazards.md: 9 entries from memory (all 8 plan items + ANSI colors).
- 2026-04-25: Stage 1 complete. Scratch tree at `/tmp/clearway-scratch/` —
  25 files, 4/4 golden tests passing, zero hits on the leak-scan pattern set
  (no `/opt/docker`, no household IPs, no `0.dot0.one` / `cloud.oracle.com`,
  no Discord webhook refs, no household member names beyond the deliberate
  alice/bob/carol archetypes). Tree layout:
  ```
  clearway-scratch/
  ├── .gitignore
  ├── docs/                              (placeholder for Stage 2)
  ├── singbox-profiles/
  │   ├── render.py                      (scrubbed; ~2330 lines)
  │   ├── profiles.example.yaml          (alice/bob/carol)
  │   ├── home_wg/README.md
  │   ├── generate-installer.sh          (PROFILE_HOST env-driven)
  │   ├── rotate-short-ids.sh, rotate-reality-key.sh
  │   ├── templates/{install-singbox.template.ps1, singbox-server.template.jsonc}
  │   └── tests/{test_render.py, fixtures/, goldens/}
  └── singbox-server/
      ├── compose.yaml                   (env-driven SINGBOX_SERVER_DIR/VNIC_*)
      ├── safe-restart.sh, rotate-hy2-cert.sh, bump-singbox-image.sh
  ```
  All scripts now read `NOTIFY` env var instead of hardcoding the household
  Discord webhook; `SINGBOX_SERVER_DIR` / `SINGBOX_PROFILES_DIR` env vars
  let the two halves live anywhere relative to each other.

---

## Open questions

- Naming: **Clearway** committed — GitHub repo created. Rename still cheap (GitHub redirects) if something better surfaces, but unlikely now.
- License: **AGPL-3.0 shipped**.
- Host-side cron scripts under `singbox-server/` that we dropped: if any reader actually asks for e.g. `banscan.sh`, we can publish them as a separate `clearway-ops` companion repo later.
- GitHub Actions CI: stage-5 polish.
- **Production hygiene from Stage 3 leak-scan finding** (decide before Stage 4):
  - Re-render production to purge eric's path-secret from every household member's rendered `install-singbox.ps1`? Idempotent: `cd /opt/docker/apps/singbox-profiles && ./render.py -y`. Re-renders all installers + restarts singbox-server (no-op if config unchanged).
  - Rotate eric's path-secret? Justified if you treat "secret has been on disk in cleartext in cross-user installer files + OneDrive copies + this session's transcript" as compromised. Procedure: edit `.secrets.yaml.users.eric.secret` (delete to auto-regen, or replace), re-render, send eric his new URL.
