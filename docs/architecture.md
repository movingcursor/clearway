# Architecture

How the pieces fit together. Read this before changing the renderer or
adding a new protocol — most of the design choices are load-bearing in
non-obvious ways.

## The two halves

```
        ┌────────────────────────────────────┐
        │  singbox-profiles/                 │
        │  ─────────────────                 │
        │  profiles.yaml      (manifest)     │
        │  .secrets.yaml      (credentials)  │
        │  home_wg/*.conf     (WG identities)│
        │  render.py          (the renderer) │
        │  templates/         (PS1 + JSONC)  │
        └─────────────┬──────────────────────┘
                      │  ./render.py
                      ▼
        ┌────────────────────────────────────┐
        │  Two independent outputs           │
        ├────────────────────────────────────┤
        │  1. Per-device client configs      │
        │     srv/p/<secret>/                │
        │       singbox-mobile.json          │
        │       singbox-windows.json         │
        │       install-singbox.ps1          │
        │       README.md                    │
        │     ↓ served over HTTPS at         │
        │     https://<PROFILE_HOST>/p/...   │
        │                                    │
        │  2. singbox-server config          │
        │     ../singbox-server/config.json  │
        │     ↓ docker compose up -d         │
        │     sing-box server container      │
        └────────────────────────────────────┘
```

A single `./render.py` invocation produces both halves. They share the same
manifest (one source of truth — every credential, every user, every device
state) so the server's `users[]` arrays and the clients' outbound configs
can never drift out of sync.

The two halves are also physically independent: the renderer runs on
whichever machine you maintain config from (the docker host is a natural
choice, but it doesn't have to be); the server runs wherever your VPS is.
You can run the renderer on a laptop, push the rendered config to the VPS,
and never run Python on the VPS itself if you don't want to.

## The manifest trio

The renderer reads three files. Each owns a different category of state.

### `profiles.yaml` — the desired state, written by hand

Only contains things a human edits: user list, per-user country/protocol
choices, device list, home-egress preferences. No secrets, no derived
state. Schema-validated at load time (`PROFILES_SCHEMA` in `render.py`)
with JSONPath-precise error messages — typos fail loudly with a precise
location instead of cascading into a cryptic `sing-box check` error after
full render.

What lives here:

- `defaults.{reality, ws-cf, shadowtls, hy2}` — server endpoints,
  ports, SNIs, the default uTLS fingerprint, optional `proxy_server_ips`
  for multi-host deployments.
- `users.<name>.countries / protocols / devices` — what each user gets.
- `users.<name>.home` — optional WG-tunnelled home-egress block.
- `users.<name>.shadowtls_sni` — optional per-user SNI override (see
  [hazard #2](hazards.md)).

What doesn't:

- Any UUID, password, key, or secret. The renderer auto-generates and
  stores them in `.secrets.yaml`.

### `.secrets.yaml` — the credentials, auto-managed

On every render, the renderer fills in any missing credentials and writes
them back. Mode is clamped to 0600 on every write. The "auto-fill missing"
loop is what makes onboarding a new user trivial — add `users.alice` to
`profiles.yaml`, run `./render.py`, and `.secrets.yaml` gets a freshly-generated
`secret`, `ws_cf_uuid`, `hy2_password`, `shadowtls_password`,
`shadowsocks_password`, and per-device `reality.{uuid, short_id}` and
`clash_secret`.

Per-user fields (`secret`, `ws_cf_uuid`, `hy2_password`,
`shadowtls_password`, `shadowsocks_password`, optional `notify_webhook`)
are auto-generated. Per-device fields (`reality.uuid`, `reality.short_id`,
`clash_secret`) are auto-generated. **Reality keypair** (`shared.reality_*`)
and **hy2 obfs password** (`shared.hy2_obfs_salamander_password`) are
NOT auto-generated — they require a paired server-side change (Reality
keypair) or shared-symmetric-secret coordination, so rotating them is a
deliberate operator action. Use `rotate-realitykey.sh` rather than editing
by hand.

### `home_wg/<user>-<device>.conf` — externally-generated WireGuard identities

Standard `wg-quick(8)` `.conf` files for any device that has a `home:` block
in `profiles.yaml`. The interface section becomes the device's `home_wg`
block in the rendered config; the peer section overwrites the user-level
`home` peer fields (endpoint, public key, AllowedIPs, etc.). `profiles.yaml`'s
`home:` block is structural-only (country / TLD lists); the peer details
always come from the `.conf`.

If a user has `home:` but the `.conf` is missing, render aborts with the
exact path it expected. Drop it in and re-run.

See `singbox-profiles/home_wg/README.md` for the file naming convention
and an example.

## Renderer composition — the dict-merge model

The renderer doesn't template strings. Each fragment is a function that
returns a Python `dict` describing a piece of the final sing-box JSON; the
composer merges them into a single dict and `json.dumps` serializes the
result. Comments live in fragment-function source for humans reading the
code; rendered JSON is pure JSON (no comments, no JSONC).

The fragment list, in `compose()` order:

```python
def compose(user, device, defaults):
    cfg = {}
    cfg.update(frag_log())                         # log.level / timestamp
    cfg.update(frag_ntp(device))                   # mobile-only NTP
    cfg.update(frag_dns(...))                      # DNS servers + rules
    if has_home and device.get('home_wg'):
        cfg.update(frag_home_endpoint(...))        # 🏠 Home WG endpoint
    cfg.update(frag_inbound(device))               # TUN inbound
    cfg.update(frag_outbounds(user, device, ...))  # Direct + per-protocol
                                                   # outbounds + selectors
    cfg.update(frag_route(user, device, defaults)) # rule_set + rules
    cfg.update(frag_experimental(device))          # cache_file + clash_api
    return cfg
```

Each fragment has a single, narrow responsibility and reads only from the
manifest dict it receives. There's no module-level state mutation between
fragments, which makes them trivially testable in isolation (see
`tests/test_render.py`).

Adding a new protocol means adding `frag_outbound_<name>` (returns the
outbound dict), wiring it into `frag_outbounds` (canonical-order append),
and extending `protocol_outbound_tags()`. Roughly 30 lines for a Reality
or hy2-style outbound; more if it has its own server template.

## The server template

`singbox-profiles/templates/singbox-server.template.jsonc` is a JSONC
file (JSON with `// line comments`) shaped like the final
`singbox-server/config.json`. The renderer:

1. Reads the template.
2. Strips JSONC comments (`_strip_jsonc`).
3. Substitutes `__PLACEHOLDER__` tokens — `__VNIC_SECONDARY_IP__`,
   `__USERS_SHADOWTLS__`, `__SHORT_IDS_REALITY__`,
   `__REALITY_PRIVATE_KEY__`, etc. — with values from the manifest.
4. Writes the result to `<server-dir>/config.json` (mode 0600).
5. Triggers `safe-restart.sh` on the server.

Why a template instead of building the dict like clients do: the server
config is mostly static — five inbound definitions plus a clash_api block.
Only the user arrays and a few SNI/key strings change per render. A
template + token substitution is shorter, more legible, and easier to
hand-audit than the equivalent dict-builder Python.

The substitution is strict: any `__PLACEHOLDER__` token in the template
that the renderer doesn't supply causes a `sys.exit(...)` at render time,
so a typo in the template fails fast.

## Rotation and the 2-hour grace window

Removing a user's credentials from `.secrets.yaml` and re-rendering must
not break a client that hasn't polled the new config yet. The pattern:

1. Operator removes user / rotates a credential.
2. `./render.py` computes the new server config, but before writing it,
   diffs against the live server config to find credentials that *were*
   live but are no longer in the manifest.
3. Each newly-orphaned credential gets added to `.pending-rotations.yaml`
   with an `expires_at` 2 hours in the future.
4. The rendered server config = manifest credentials + still-valid pending
   credentials. So the orphaned creds stay live on the server for 2h.
5. On the next render after expiry, `compute_rotation_plan` drops the
   expired entries permanently.

Identity for "is this the same credential" is the *authentication* field,
not `name`. Renaming `alice-mobile` → `alice-phone` keeps the same UUID
and is recognized as a rename (no rotation, no grace). Changing the UUID
*is* a rotation (grace applies). The `_item_key()` function in `render.py`
encodes this.

This means routine cred rotation (`rotate-shortids.sh`) is a flag-day-free
operation — any client that polls within 2h of rotation picks up the new
short_id while the old one is still valid; any client that polls *after*
the rotation gets the new short_id from the start. The poll interval has
to be < 2h for this to work; the default Windows hourly task and mobile
`auto_update_interval: 60min` both qualify.

What this *doesn't* cover:

- **Reality keypair rotation** — the public key is shared across all users,
  so there's no "old + new" slot on the server. `rotate-realitykey.sh`
  is a flag day; clients without a recent poll will fail Reality until they
  re-fetch. Other protocols on the same client are unaffected — the user
  flips 🔀 Proxy to a different protocol manually if their default was
  reality (see *Selector default and manual fallback* below).
- **hy2 cert rotation** — the cert is pinned by every client; same
  flag-day model. `rotate-hy2-cert.sh` posts a notification (via `NOTIFY`
  if set) so operators are aware of the temporary hy2 outage; users on hy2
  default flip to ShadowTLS or ws-cf in the dashboard until they poll the
  new pin.

## Selector default and manual fallback

The 🔀 Proxy selector emits `type: selector` (not `urltest`) with a
country-derived default. There is no automatic on-error switch between
protocols; the user manually flips in the dashboard if their default
breaks. This is a deliberate change from the pre-AWG design (which used
`urltest` to auto-pick the fastest protocol every few minutes).

### Why no automatic fallback

The pre-AWG renderer emitted a `⚡ Fastest` urltest outbound that probed
every enabled protocol on a 5-10 minute cadence. That probing pattern is
itself a fingerprint — regular small requests to multiple foreign IPs at
fixed intervals, observable behaviorally even when individual protocols
pass DPI. Removing it tightens the threat model materially.

Two on-error fallback approaches were considered as replacements:

1. **Route rules matching outbound errors and re-routing**. sing-box 1.13's
   route-rule schema doesn't include an "outbound returned an error"
   matcher; this isn't expressible in vanilla sing-box.
2. **A thin watcher (sidecar) updating the selector default via the
   clash_api**. Adds a runtime component that has to be deployed on every
   client device — not viable for the iOS app, doesn't fit the renderer-
   only deployment model.

Neither produces a zero-runtime-component solution that sing-box 1.13
supports natively. We chose pure selector + documented manual fallback:
the 🔀 Proxy selector lists every enabled protocol; the user taps to
switch when the default fails. Per-user README spells out the procedure.

### Country defaults

Single-country residents get the protocol most likely to survive in their
region as the selector default:

| Country | Default | Why |
|---|---|---|
| `cn` | `reality` | Fastest when the proxy IP isn't IP-blocked; ws-cf is the survival fallback. |
| `ru` | `shadowtls` | Most resilient to RKN/TSPU's TCP-freeze attack on suspicious foreign IPs. |
| `ir` | `shadowtls` | The most consistently surviving sing-box-native path post-2025 IR shutdown. |
| traveller / multi-country | `hy2` | Speed-first default when no specific region is targeted. |

Each country's `data/countries/<iso>.yaml` carries a `protocols.default`
field; `proxy_selector_default()` in render.py reads it. Override per-user
with `users.<name>.preferred_protocol: <tag>` in `profiles.yaml`.

### AWG as a parallel resilience tunnel

AWG never shows up as a sing-box selector option — it's served by a
separate Amnezia VPN app on the user's device. When all four sing-box-
native protocols fail (e.g. RKN coordinated TCP-freeze + UDP/443 throttle),
the user starts the AWG tunnel from the Amnezia app as the second-leg
fallback. The two-app split is documented in the per-user README and
[hazards.md](hazards.md#amnezia-vpn-app-on-ios-conflicts-with-active-sing-box-vpn-profile).

## awg-server: separate container, separate failure domain

AmneziaWG runs in `amneziavpn/amneziawg-go` (the official Amnezia upstream
image) as a sidecar to `singbox-server`. Two services in `compose.yaml`,
opt-in via `--profile awg-server`. sing-box stays unmodified at upstream
HEAD — clearway's stack pays no version-lag tax for using AWG. Failure
isolation is a free bonus: a wedged AWG container doesn't touch the four
sing-box-native protocols, and a wedged sing-box doesn't touch AWG.

The alternative (replace upstream sing-box with a hoaxisr/amnezia-box
fork that carries the AWG outbound type) would have made every existing
protocol pay the fork tax — single-maintainer dependency, version lag
(1.12.x vs upstream 1.13.x), no upstream docker images, schema validators
lag the fork. The 95% of clearway functionality that's not AWG inherits
this tax to enable a feature used by some users on one protocol. The
sidecar architecture skips that math entirely.

amneziawg-go runs userspace WireGuard and needs `NET_ADMIN` to manage the
TUN device + iptables NAT rules. This is unavoidable for any WG userspace
implementation. Compensate with read-only rootfs, image digest pin, narrow
port exposure (UDP/51820 only via VNIC_SECONDARY_IP), no-new-privileges.
See [hardening.md](hardening.md).

## AWG subnet allocation

Per-device IPv4 addresses inside `awg.subnet` are allocated deterministically
by `_allocate_awg_addresses()` in render.py. AWG identities are per-device
(each device of an AWG-enabled user gets its own keypair, /32 address, and
[Peer] block on the server). The mechanics:

1. The first host address (e.g. `10.66.66.1` for the default `/24`) is
   reserved for the awg-server's own [Interface] block. Never assigned to
   any device, even via explicit pin.

2. Pinned `awg_address` values from `profiles.yaml` (set per device under
   `users.<n>.devices[].awg_address`) are processed first. Operator-set
   pins are validated (must parse as a CIDR, must lie inside `awg.subnet`,
   must not collide with the server address or another pin). Errors here
   exit before any rendering happens, so a bad pin surfaces with a precise
   message rather than as a downstream "subnet full" red herring.

3. Hash-allocated for the rest. `int(sha256("<uname>/<dev_name>").hexdigest(), 16) % N`
   picks a starting offset; linear-probe forward (modulo `N`, sorted by
   `(uname, dev_name)` for determinism) until a free slot. Fail loud if
   the subnet is full.

Hashing on `<uname>/<dev_name>` means **adding a user or device mid-life
doesn't reshuffle existing peers** — each is found at the same hash
position on every run, which keeps `.conf` distributions stable across
renders. Linear probe handles incidental collisions without resetting
the whole allocation.

`awg.subnet` defaults to `10.66.66.0/24` (254 host addrs, comfortable
upper bound for the household scale clearway targets — a 5-user household
with 3 devices each fills 15 of the 254 slots). The default is chosen
unconventional-enough to rarely collide with home networks; deployments
where it *does* collide change it in `.secrets.yaml` and re-render.
Subnet change is a flag-day for AWG devices (every `.conf` becomes
invalid; redistribute) — see [hazards.md #17](hazards.md#17-awg-subnet-collision-with-home_wg-ranges).

## SS-2022 multi-user EIH

ShadowTLS's inner Shadowsocks-2022 inbound runs in **multi-user EIH** mode:

- The inbound carries a server-level `password` (32-byte base64 PSK),
  shared across all users.
- Each user has their own per-user `password`, a separate 32-byte PSK.
- Clients send the colon-joined form `<server_psk>:<user_psk>` as their
  outbound password. sing-box splits on `:`, encrypts under the server PSK,
  embeds an Encrypted Identity Header (EIH) carrying the user PSK's
  identity, and routes the session.

This gets you per-user revocation on a single inbound (rotate one user's
PSK without touching others) and per-user audit identity in server logs,
without running N separate inbounds.

`render.py`'s `frag_outbound_shadowtls` builds the colon-joined password
on the client side (`f"{d['shadowsocks_password']}:{user_ss_pw}"`); the
server template emits a `users[]` array with per-user `password` entries.
Both halves come from `.secrets.yaml`: `shared.shadowsocks_password` (the
server-level PSK) and per-user `users.<name>.shadowsocks_password`.

## File serving and `PROFILE_HOST`

The renderer writes per-user output to `singbox-profiles/srv/p/<secret>/`.
This directory is meant to be served behind a reverse proxy (Traefik,
Caddy, nginx, whatever) at `https://<PROFILE_HOST>/p/<secret>/`. The path
secret is the credential — knowing the URL grants access to that user's
config, install script, and credentials README. Treat it like a token:
generate fresh ones with `secrets.token_hex(16)` (the renderer does this),
rotate by removing the user from `.secrets.yaml.users.<name>.secret` and
re-rendering.

`PROFILE_HOST` is read from the env var of the same name (or repo
`.env`). The renderer bakes it into:
- per-user README URLs (the user-facing onboarding doc)
- the Windows installer one-liner inside that README
- `secrets.txt` header (operator-side mapping)
- the rendered Windows `install-singbox.ps1` — every URL that fetches
  config / version / NSSM / wintun goes through `PROFILE_HOST`

There's no fallback domain in clients — if `PROFILE_HOST` is unreachable
from a Windows client, the hourly config-update task silently fails (and
notifies via `WEBHOOK_URL` if set on that user). Initial install needs
`PROFILE_HOST` reachable, but once installed, the client keeps running on
its last good config until either the host comes back or the user
manually fetches a new config.

## When to update the schema

The `PROFILES_SCHEMA` dict in `render.py` is the contract between
`profiles.yaml` and the renderer. Update it when:

- Adding a new manifest field (new `defaults.*` block, new per-user knob).
  The schema is `additionalProperties: default-allow` for unknown keys, so
  forgetting to add a field doesn't break loading — but a typo in an
  *existing* field name will silently no-op instead of erroring.
- Adding a new enum value (a new country code, a new protocol, a new
  device type). Add it to the `enum:` list so typos fail at load time.
- Adding a new username pattern restriction. The current pattern enforces
  lowercase `[a-z][a-z0-9_-]{1,23}` because uppercase usernames render
  into served directory paths with case inconsistency, which has bitten
  us before.

## Testing

`tests/test_render.py` is a stdlib-only golden-file harness. It imports
`render.py` as a module, monkey-patches the path globals to point at
`tests/fixtures/`, runs the full pipeline, and byte-compares each
rendered config against `tests/goldens/<user>-<device>.json`.

The fixtures cover the combinations the renderer actually branches on:

- `test_alice` — full traveller (every protocol, home_wg, multi-country, admin)
- `test_bob` — single-country resident (subset of protocols, no home)
- `test_dave` — multi-country traveller without home_wg

Run as `python3 tests/test_render.py` (assert) or `--update` (regenerate
goldens). When you intentionally change rendered output, regenerate and
eyeball the diff in `git diff tests/goldens/` before committing.

CI runs the same harness on every push (see [`.github/workflows/`](https://github.com/) once added at Stage 5).
