# Clearway

[![tests](https://github.com/movingcursor/clearway/actions/workflows/test.yml/badge.svg)](https://github.com/movingcursor/clearway/actions/workflows/test.yml)
[![license: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

Multi-user, multi-protocol [sing-box](https://github.com/SagerNet/sing-box)
config generator + server stack for households and small teams in (or
travelling to) restrictive networks.

You write a YAML manifest describing your users, their devices, and which
of CN / RU / IR they live in or visit. `./render.py` produces a per-device
sing-box config for each one (Android, iOS, Windows), a hardened
docker-compose stack for the server side, and a one-liner Windows
installer that keeps everything auto-updated.

The default protocol mix — VLESS+Reality on TCP/443, Hysteria2 on UDP/443,
ShadowTLS+Shadowsocks-2022 on TCP/8443, VLESS-over-WebSocket fronted by
Cloudflare on TCP/443, plus optional AmneziaWG on UDP/51820 — gives
clients five orthogonal paths through DPI. The in-app **selector** picks
the protocol best suited to the user's region as the default; no
auto-switching probe runs in the steady state (the constant probing was
itself a fingerprint), so the user manually flips selectors in the
dashboard if their default breaks. AmneziaWG runs in the separate
[Amnezia VPN](https://amnezia.org/) app on the client side as a parallel
resilience tunnel for RU/IR users — see [`docs/architecture.md`](docs/architecture.md).

Per-user PSKs / UUIDs, per-user uTLS fingerprint randomization, optional
per-user ShadowTLS SNI, optional WireGuard "home egress" so a traveller's
home-country traffic exits from a specific home network, all expressed
in YAML you edit by hand. DNS-level threat-feed and ad blocking via
[Hagezi](https://github.com/hagezi/dns-blocklists) lists is on by
default. **The recommended client is the official sing-box app**
([Android](https://play.google.com/store/apps/details?id=io.nekohasekai.sfa) /
[iOS $3.99](https://apps.apple.com/us/app/sing-box-vt/id6673731168) /
[Android sideload](https://github.com/SagerNet/sing-box-for-android/releases)
/ [Windows via NSSM-managed service](docs/quickstart.md)) — clearway's
generated profile uses official-sing-box-only features (the no-probe
selector design specifically) that Hiddify-Next overrides with its own
auto-switcher.

## Status

Used in production by a small household network. Stable enough to run
unattended; the `singbox-server/` keep-scripts handle restart-on-config-
change, hy2 cert rotation, and image-pin bumps with rollback.

This repo extracts that household stack into something deployable from
scratch. The plumbing (renderer + composer + server template + golden
tests) is the same code; what's stripped is the household-specific data,
notification webhooks, OneDrive mirroring, and the operator-side cron
scripts that don't generalize.

## What's in the box

```
clearway/
├── singbox-profiles/         renderer half
│   ├── render.py             reads profiles.yaml + .secrets.yaml + home_wg/
│   │                         emits per-device client configs (singbox + AWG),
│   │                         the singbox + awg-server configs, all in one
│   │                         pass; 2h rotation grace built in for sing-box
│   │                         protocols
│   ├── profiles.example.yaml three documented user archetypes
│   ├── home_wg/              drop user-device WireGuard .conf files here
│   ├── templates/            singbox-server.template.jsonc + awg-{client,
│   │                         server}.conf.template + Windows installer
│   ├── tests/                stdlib-only golden-file tests + X25519 unit test
│   ├── generate-installer.sh per-user Windows install-singbox.ps1 builder
│   ├── rotate-shortids.sh   monthly Reality short_id rotation
│   └── rotate-realitykey.sh quarterly Reality keypair rotation
├── singbox-server/           server half — sing-box-native protocols
│   ├── compose.yaml          hardened (cap-drop ALL, read-only rootfs,
│   │                         no-new-privileges, mem/cpu limits, digest pin)
│   ├── safe-restart.sh       sing-box check before reconcile; restart on
│   │                         bind-mount inode change
│   ├── rotate-hy2-cert.sh    yearly hy2 self-signed cert rotation
│   └── bump-image.sh         controlled image-digest upgrade
└── awg-server/               server half — AmneziaWG (opt-in profile)
    ├── compose.yaml          amneziavpn/amneziawg-go pinned by digest;
    │                         NET_ADMIN unavoidable for userspace WG, balanced
    │                         by read-only rootfs, narrow port exposure, etc.
    ├── safe-restart.sh       structural awk validation + bind-mount inode fix
    ├── rotate-params.sh      quarterly Jc/Jmin/Jmax/S1/S2/H1-H4 rotation
    └── bump-image.sh         controlled image-digest upgrade
```

## Quickstart

See [`docs/quickstart.md`](docs/quickstart.md) for the end-to-end walk-
through (about 30 minutes on a fresh VPS).

The short version, once prerequisites are in place:

```sh
git clone https://github.com/<your-fork>/clearway /opt/clearway
cd /opt/clearway
cp singbox-profiles/profiles.example.yaml singbox-profiles/profiles.yaml
$EDITOR .env                                # PROFILE_HOST, VNIC_SECONDARY_IP, PUID, PGID
$EDITOR singbox-profiles/profiles.yaml      # users, countries, protocols
cd singbox-server && openssl req -x509 ...  # see hazards.md #1 for the SAN-required openssl
./singbox-profiles/render.py                # auto-fills credentials, renders both halves
docker compose --profile singbox-server up -d
```

Onboarding a user is `add a YAML block + ./render.py`; users get a
per-device URL (`https://${PROFILE_HOST}/p/<secret>/`) with a generated
README and either a sing-box remote-profile URL (mobile) or a one-liner
PowerShell install command (Windows).

## Documentation

- [`docs/quickstart.md`](docs/quickstart.md) — fresh-VPS deployment, step-
  by-step. Start here if you want to run it.
- [`docs/architecture.md`](docs/architecture.md) — the renderer's
  composition model, the manifest trio (`profiles.yaml` /
  `.secrets.yaml` / `home_wg/`), 2-hour rotation grace, SS-2022 multi-
  user EIH, server template substitution. Read before changing
  `render.py` or adding a protocol.
- [`docs/hazards.md`](docs/hazards.md) — the silent-failure modes we've
  hit in production. Read once even if everything looks fine — most of
  these took more than a weekend to root-cause and the workarounds are
  baked into the renderer.
- [`docs/hardening.md`](docs/hardening.md) — pre-deploy security checklist
  for the host the stack runs on (SSH, cloud firewall, backups, image
  bumps). Read before pointing real users at a public IP.
- [`singbox-profiles/home_wg/README.md`](singbox-profiles/home_wg/README.md)
  — `.conf` format for the optional home-egress feature.

## Tests

The renderer has a stdlib-only golden-file test harness. Three fixture
users exercise every render branch (full traveller with home_wg,
single-country resident, multi-country traveller without home_wg).
Run on every change to `render.py` or the server template:

```sh
cd singbox-profiles
python3 tests/test_render.py            # assert against goldens
python3 tests/test_render.py --update   # regenerate after intentional changes
```

Goldens are committed; CI runs the same harness on every push (Stage 5).

## Configuration via env vars

| Var                  | What                                                  | Default                       |
| -------------------- | ----------------------------------------------------- | ----------------------------- |
| `PROFILE_HOST`       | Public hostname clients fetch profiles from           | `profile.example.com`         |
| `VNIC_SECONDARY_IP`  | IP that hy2/Reality bind on (multi-VNIC: matters)     | (required)                    |
| `SINGBOX_SERVER_DIR` | Path to the singbox-server dir (compose bind mounts)  | `<repo>/singbox-server`       |
| `PUID` / `PGID`      | UID:GID owning config.json + hy2.{crt,key}            | (required)                    |
| `NOTIFY`             | Optional path to a notification script for cron jobs  | (unset — prints to stderr)    |
| `HY2_SNI`            | Cover hostname baked into the hy2 cert by rotate-hy2  | `cloud.example.com`           |

Set in a repo-level `.env` (gitignored — see `.gitignore`).

## Why these five protocols (DPI signature families)

The protocol mix isn't an arbitrary "more is better" stack — each
protocol covers a *different* DPI signature family. A national firewall
that classifies and blocks one family typically can't apply the same
classifier to the others without breaking unrelated traffic, so the user
manually switches lanes in the dashboard when one shape gets flagged.

| Family                          | Protocol                | What the wire looks like                                                      |
| ------------------------------- | ----------------------- | ----------------------------------------------------------------------------- |
| TLS-mimic-no-tunnel             | VLESS+Reality           | Real TLS handshake stolen from a public site; no SNI faking                   |
| CDN-fronted WebSocket           | VLESS-over-WS via CF    | TLS to Cloudflare's edge with ECH, WS upgrade, VLESS inside                   |
| Handshake-with-passthrough      | ShadowTLS+SS-2022       | Real TLS handshake to a cover SNI, then encrypted payload over TCP            |
| Obfuscated QUIC                 | Hysteria2 (salamander)  | Random-looking UDP payload, no parseable QUIC Initial                         |
| Obfuscated WG handshake         | **AmneziaWG**           | UDP that looks like nothing — junk packets pre-handshake (Jc/Jmin/Jmax),      |
|                                 |                         | padded init/response (S1/S2), custom magic headers (H1-H4) replacing WG's     |

**Shared UDP-class risk worth flagging:** AmneziaWG and Hysteria2 both
die under blanket UDP marking. The protocol-specific classifiers that
censors actually deploy are uncorrelated, but the IP-level UDP throttling
RU TSPU and Iranian ISPs have demonstrated isn't — when both UDP
protocols go dark together, fall back to one of the three TCP shapes.

AmneziaWG's role is specifically the threat model RU users actually face:
TSPU's TCP-freeze attack on suspicious foreign IPs has degraded Reality
through late 2025; CF CIDR whitelisting has degraded VLESS-over-WS in
parallel. AWG is the protocol Russians are actually deploying — Amnezia's
Banzaev confirms the operates-stably-with-periodic-signature-blocks
model in the Jan 2026 TechRadar interview. AWG runs in a *separate*
[Amnezia VPN](https://amnezia.org/) app rather than the sing-box profile;
the two-app split is documented in [architecture.md](docs/architecture.md).

Adding a sixth protocol that falls into one of these families (e.g.
TUIC v5 — also obfuscated QUIC, same family as hy2) doesn't add real
resilience — a classifier that flags one will flag the other. We've
deliberately *not* added several otherwise-popular protocols on this
basis. As of early 2026 the only candidate in upstream-stable sing-box
that opens a genuinely new family is **AnyTLS** (real TLS session +
random padding + N:1 multiplex, available since sing-box v1.12.0), but
field reports flag fingerprintable structural quirks; it's a watchlist
item, not a default. Re-evaluate ~every 6 months.

## Threat model + design assumptions

**In scope:**

- DPI on the path between clients and the proxy server (CN GFW, RU TSPU,
  IR national firewall) — addressed via four protocol shapes with
  different DPI signatures, per-user fingerprint decorrelation, and
  Cloudflare-fronted fallback.
- Server-side passive cred capture — every credential is per-user,
  per-device where the protocol allows; rotation is routine.
- Client-side stale config — auto-update + 2h server-side rotation grace
  means you can rotate creds without coordinating with users.

**Out of scope:**

- Active DPI that physically blocks the destination IP. ws-cf through
  Cloudflare is the only inbound that survives "the proxy IP is
  blackholed" — keep your Cloudflare zone working and make sure
  `defaults['ws-cf'].host` is reachable.
- Endpoint compromise. If a user's device is rooted or has malware, none
  of this protects them.
- Anonymity. This routes traffic through your VPS — the VPS sees every
  destination. Use Tor over Clearway if you also want anonymity.
- Per-user bandwidth caps at the sing-box layer
  ([hazards.md #7](docs/hazards.md#7-sing-box-has-no-per-user-hy2-bandwidth-caps)).

## Contributing

Bug reports especially welcome — if you hit a silent-failure mode that
isn't in [hazards.md](docs/hazards.md), file an issue with the symptom,
the smallest reproducible config (with credentials redacted), and the
relevant log lines from `docker logs singbox-server`. The hazards doc is
the single biggest piece of operational knowledge in this repo; growing
it is more valuable than most code changes.

For code changes:

1. Run `python3 singbox-profiles/tests/test_render.py` first.
2. If the change intentionally affects rendered output, regenerate
   goldens with `--update` and include the diff in the PR.
3. Anything user-visible should be reflected in the per-user README
   that `render.py` generates.

## License

[AGPL-3.0](LICENSE). Derivatives must remain open. If you deploy a
modified version as a service, the modified source must be available
to the service's users.

## Acknowledgments

Stands on the shoulders of:

- [SagerNet/sing-box](https://github.com/SagerNet/sing-box) — the protocol
  multiplexer this all runs on.
- [WireGuard](https://www.wireguard.com/) (Jason A. Donenfeld) — the base
  tunnel protocol AmneziaWG extends.
- [Amnezia VPN](https://amnezia.org/) (the [amnezia-vpn](https://github.com/amnezia-vpn)
  org) — three components clearway depends on directly:
  [amneziawg-go](https://github.com/amnezia-vpn/amneziawg-go) (the userspace
  daemon awg-server runs),
  [amneziawg-tools](https://github.com/amnezia-vpn/amneziawg-tools) (the
  `awg` CLI used to load the obfuscation parameters), and the
  [Amnezia VPN client apps](https://github.com/amnezia-vpn/amnezia-client)
  on Android / iOS / desktop that import the per-user `.conf` files. The
  protocol design — junk packets pre-handshake (Jc/Jmin/Jmax), padded
  init/response (S1/S2), custom magic headers (H1-H4) — is what makes
  AmneziaWG usable in CN/RU/IR where vanilla WireGuard is fingerprinted
  and blocked.
- [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) —
  the CN geosite/geoip rule-sets and the GFW geosite list used for split
  routing in the CN profile.
- [runetfreedom/russia-v2ray-rules-dat](https://github.com/runetfreedom/russia-v2ray-rules-dat) —
  the RU geosite/geoip rule-sets, including the `ru-blocked` and
  `ru-available-only-inside` subsets that drive the 🚨 Restricted route
  for Russian users.
- [chocolate4u/Iran-sing-box-rules](https://github.com/chocolate4u/Iran-sing-box-rules) —
  the IR geosite/geoip rule-sets plus the malware/phishing/ads lists
  layered into the default DNS reject rule when IR is enabled.
- [SagerNet/sing-geoip](https://github.com/SagerNet/sing-geoip) — the
  per-country geoip rule-sets the renderer pulls in for the optional
  home-egress feature (one `geoip-<iso>.srs` per `home_egress_countries`
  entry).
- [Hagezi](https://github.com/hagezi/dns-blocklists) — the DNS
  threat-intelligence (TIF) and pro-adblock lists wired into the default
  DNS reject rule, served as sing-box rule-sets via
  [razaxq/dns-blocklists-sing-box](https://github.com/razaxq/dns-blocklists-sing-box).
- [MetaCubeX/metacubexd](https://github.com/MetaCubeX/metacubexd) — the
  clash-api dashboard the Windows installer wires up.
- [NSSM](https://nssm.cc/), [WireGuard / wintun](https://www.wintun.net/) —
  the Windows install path.
