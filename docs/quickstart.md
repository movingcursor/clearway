# Quickstart

Set up Clearway on a fresh VPS in about 30 minutes. Targets Oracle Cloud
ARM Ampere (free tier — 4 OCPU / 24 GiB RAM is plenty), but anything with
a public IP and Docker works.

This walkthrough deploys the full default stack: ShadowTLS + Hysteria2 +
VLESS+Reality + VLESS-over-WS-via-Cloudflare. Skip steps for inbounds you
don't want.

> **Before pointing real users at this**, work through
> [docs/hardening.md](hardening.md). The container hardening shipped in
> compose.yaml handles container-side security; the host the stack runs
> on is your responsibility (SSH, cloud firewall, backups, image bumps).

## Prerequisites

- A VPS with a public IP, Docker + Docker Compose v2, openssl, git.
- A domain you control with at least two A records:
  - **`vpnws.<your-domain>`** — pointed at Cloudflare (proxy on / orange
    cloud) for the WS-CDN front. Cloudflare needs an Origin server
    config that points back to your VPS — this guide assumes Traefik or
    Caddy fronting the VPS does the TLS termination.
  - **`profile.<your-domain>`** — A record direct to your VPS public IP
    (no Cloudflare proxy), TLS terminated by your reverse proxy. This is
    the URL clients fetch their config from.
- A handshake-cover hostname for Reality. Pick a real, busy, allowed-
  everywhere site that's TLS-1.3-capable and plausibly reachable from
  your VPS. For an Oracle Cloud VM, an Oracle Cloud console hostname
  works (`console.<region>.oraclecloud.com`); for AWS / GCP / DO, pick a
  matching cloud-provider hostname. Verify TLS 1.3 before pinning:
  ```
  openssl s_client -tls1_3 -connect <host>:443 -servername <host>
  ```
  If the handshake aborts, the host doesn't speak TLS 1.3 — pick another.
- A reverse proxy in front of the VPS handling Let's Encrypt for
  `profile.<your-domain>` and `vpnws.<your-domain>`. Traefik, Caddy, or
  nginx — anything that routes `Host:` matches works. The repo doesn't
  ship a reverse-proxy config (out of scope; pick what you already run).

## 1. Clone and create the env file

```sh
git clone https://github.com/<your-fork>/clearway.git /opt/clearway
cd /opt/clearway
```

Create a repo-level `.env` (gitignored) with the host-wide config:

```sh
cat > .env <<'EOF'
# Public hostname clients fetch their config from. Must match the cert on
# your reverse proxy.
PROFILE_HOST=profile.example.com

# Repo paths — used by docker compose for bind mounts. Absolute paths.
SINGBOX_SERVER_DIR=/opt/clearway/singbox-server

# UID / GID that own config.json + hy2.{crt,key}. Should match the user
# who runs ./render.py and `docker compose up`. `id -u` / `id -g`.
PUID=1000
PGID=1000

# Hysteria2 binds explicitly to this IP. On a single-VNIC host, set to
# your primary public IP or 127.0.0.1 (compose runs host-network so
# 127.0.0.1 binds the loopback only — useful for testing). On multi-VNIC
# hosts (Oracle Cloud's two-VNIC layout, AWS multi-ENI), set this to the
# VNIC that owns the public IP you want hy2 reachable on. See
# docs/hazards.md #6 for why explicit binding matters with multi-VNIC.
VNIC_SECONDARY_IP=10.0.0.10
EOF
chmod 600 .env
```

Add a host-level sysctl so the singbox-server container (running as
non-root) can bind to ports 443/8443:

```sh
sudo tee /etc/sysctl.d/99-singbox-unpriv-port.conf <<'EOF'
net.ipv4.ip_unprivileged_port_start=443
EOF
sudo sysctl --system
```

## 2. Generate the hysteria2 cert

```sh
cd singbox-server
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout hy2.key -out hy2.crt -days 730 -nodes \
  -subj "/CN=cloud.example.com" \
  -addext "subjectAltName=DNS:cloud.example.com"
chmod 600 hy2.key
chmod 644 hy2.crt
chown $(id -u):$(id -g) hy2.{crt,key}
cd ..
```

`cloud.example.com` here matches what you'll set as `defaults.hysteria2.sni`
in `profiles.yaml` next. Both must agree — clients pin the cert by PEM
*and* validate hostname against `tls.server_name`. See [hazard #1](hazards.md#1-hy2-self-signed-cert-must-have-subjectaltname-not-just-cn).

## 3. Edit the manifest

```sh
cd singbox-profiles
cp profiles.example.yaml profiles.yaml
```

Open `profiles.yaml` and edit:

- `defaults.reality.server` — your VPS public IP.
- `defaults.reality.handshake_sni` — your verified TLS-1.3 cover host.
- `defaults.ws_cdn.host` — `vpnws.<your-domain>`.
- `defaults.shadowtls.sni` — your ShadowTLS cover host (typically same as
  `defaults.hysteria2.sni`; both should match the hy2 cert SAN if you want
  to share one cover identity).
- `defaults.hysteria2.sni` — must match the hy2 cert CN/SAN above.
- (Optional) `defaults.proxy_server_ips` — only set if you have multiple
  public IPs / multiple servers. Single-VPS deployments leave it empty.
- `users.*` — replace `alice`/`bob`/`dave` with your actual users. See
  the inline comments for what each archetype demonstrates.

For users with `home:` blocks, drop a `home_wg/<user>-<device>.conf`
file alongside (standard `wg-quick` format). See
`singbox-profiles/home_wg/README.md`.

## 4. First render

The first render auto-generates every secret it needs (UUIDs, passwords,
Reality keypair, etc.) into `.secrets.yaml`:

```sh
PROFILE_HOST=$(grep ^PROFILE_HOST ../.env | cut -d= -f2) ./render.py
```

Or set `PROFILE_HOST` and `VNIC_SECONDARY_IP` in your shell env / source
`../.env` first; `render.py` reads from process env, falling back to the
repo `.env`.

You'll be prompted to confirm the auto-generated changes. Say `y`. Output
files land in `singbox-profiles/srv/p/<secret>/`.

The first render also writes `singbox-server/config.json` and runs
`safe-restart.sh`, which `sing-box check`s the new config inside a
throwaway container before reconciling the real one. If `check` fails,
the live server stays untouched.

## 5. Wire up your reverse proxy

You need two routes:

- **`profile.<your-domain>`** → static file serving from
  `singbox-profiles/srv/`. The path inside is `/p/<secret>/...`. Any
  static-file backend works (Traefik `file` provider with
  `Directory: srv`, Caddy `file_server`, nginx `location /p/`).
- **`vpnws.<your-domain>`** → reverse-proxy WebSocket traffic to
  `http://172.17.0.1:10001/` (the singbox-server container's WS-in
  inbound, bound to docker0). TLS terminated by your reverse proxy;
  inside the docker host the connection is plaintext.

The repo doesn't ship reverse-proxy config because everyone runs a
different one. The only requirements: HTTPS termination on both
hostnames, and WebSocket upgrade support on `vpnws`.

## 6. Verify

After the first render + restart:

```sh
docker compose --profile singbox-server up -d singbox-server
docker logs singbox-server --tail 20
docker ps --filter name=singbox-server  # should show "healthy" after ~1m
```

Healthy means: ShadowTLS (TCP 8443), WS-in (TCP 172.17.0.1:10001),
Hysteria2 (UDP 443 on `${VNIC_SECONDARY_IP}`), and clash_api (TCP
127.0.0.1:9095) all answer. Reality on TCP 443 isn't probed (would log
spurious "invalid connection" lines on every healthcheck — see
`compose.yaml` comment).

Sanity-check the served URLs:

```sh
curl -s -o /dev/null -w "%{http_code}\n" \
  https://profile.<your-domain>/p/$(awk -v u=alice '$1==u{print $2}' singbox-profiles/secrets.txt)/singbox-mobile.json
# → 200
```

## 7. Onboard a user

The renderer wrote a per-user README to `srv/p/<secret>/README.md` with
copy-paste setup instructions for each device, the install URLs, and the
credentials block. Send the user the URL:

```
https://profile.<your-domain>/p/<their-secret>/
```

They can read the README in a browser and follow the per-platform sections.
For mobile (sing-box app on Android/iOS), the URL of the JSON is the
remote-profile URL — paste it into the app's "Remote profile" field, set
auto-update to 60 minutes, save. For Windows, the README contains a
PowerShell one-liner that pulls the install script + sets up an NSSM
service + scheduled hourly config refresh.

## 8. Rotation cron

Two periodic tasks for routine hygiene:

```cron
# Monthly Reality short_id rotation. Zero-downtime via the 2h grace
# window (see docs/architecture.md). 03:30 UTC on the 1st.
30 3 1 * *  cd /opt/clearway/singbox-profiles && ./rotate-short-ids.sh

# Yearly hy2 cert rotation. Flag-day for hy2 specifically; clients
# pick up the new pin on next poll. Schedule for a low-traffic window.
0 5 1 4 *  HY2_SNI=cloud.example.com /opt/clearway/singbox-server/rotate-hy2-cert.sh
```

For Reality keypair rotation (a bigger flag day — see
[architecture.md § rotation](architecture.md#rotation-and-the-2-hour-grace-window)):
run `singbox-profiles/rotate-reality-key.sh` manually when you need to,
during a window where a brief Reality outage is acceptable.

For monthly image bumps:

```cron
# Monthly sing-box image bump (digest pin update). 04:00 UTC on the 5th.
0 4 5 * *  /opt/clearway/singbox-server/bump-singbox-image.sh
```

If you want notifications for any of these (success / failure pings to
Slack / Discord / your own webhook), set `NOTIFY=/path/to/notify.sh` in
the cron line. The script gets one argument: a one-line summary string.

## 9. Adding more users

Edit `profiles.yaml` → add a `users.<name>:` block → run `./render.py`.
The renderer auto-fills all credentials, writes their per-device configs
+ README, regenerates the Windows installer, and re-applies the server
config (with 2h rotation grace for any creds that changed). Send the new
user their URL.

Removing a user is the same in reverse: delete the block from
`profiles.yaml`, re-render. Their credentials sit in
`.pending-rotations.yaml` for 2h (so any in-flight session can complete),
then drop on the next render.

## Troubleshooting

- **`./render.py` aborts with "env vars missing"** — `PROFILE_HOST` and
  `VNIC_SECONDARY_IP` aren't in process env or repo `.env`. Fix the
  `.env` or export them in your shell.
- **`sing-box check` fails on first render** — read the error; usually
  a typo in `profiles.yaml`. Schema validation runs first and gives a
  precise JSONPath.
- **Container starts but healthcheck never goes green** — `docker logs
  singbox-server` shows what's wrong. Common: hy2 cert path readability
  (chown to `${PUID}:${PGID}`), VNIC bind IP wrong (kernel chooses egress
  source IP via default route — see
  [hazard #6](hazards.md#6-tun-auto_route--strict_route-captures-hy2s-own-quic-egress)).
- **Mobile client connects but everything times out** — try flipping
  `🔀 Proxy` from `⚡ Fastest` to a specific protocol via the in-app
  selector. If only one protocol works, the others have an issue
  (cert mismatch, DNS leak, or a hazard listed in
  [hazards.md](hazards.md)).
- **Windows installer fails on `Invoke-WebRequest`** — `PROFILE_HOST` not
  reachable from the Windows machine (DNS, firewall, or the user's
  current network). The installer retries 3× with backoff before failing;
  if all fail, run the install script with `-Verbose` to see the URL
  it's trying.

For deeper debugging, the [hazards](hazards.md) doc covers every silent-
failure mode we've hit in production. Read it once even if everything
looks fine — it's faster than re-discovering them.
