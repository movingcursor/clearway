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

- `defaults.{reality, ws_cdn, shadowtls, hysteria2}` — server endpoints,
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
`secret`, `ws_cdn_uuid`, `hysteria2_password`, `shadowtls_password`,
`shadowsocks_password`, and per-device `reality.{uuid, short_id}` and
`clash_secret`.

Per-user fields (`secret`, `ws_cdn_uuid`, `hysteria2_password`,
`shadowtls_password`, `shadowsocks_password`, optional `notify_webhook`)
are auto-generated. Per-device fields (`reality.uuid`, `reality.short_id`,
`clash_secret`) are auto-generated. **Reality keypair** (`shared.reality_*`)
and **hy2 obfs password** (`shared.hysteria2_obfs_salamander_password`) are
NOT auto-generated — they require a paired server-side change (Reality
keypair) or shared-symmetric-secret coordination, so rotating them is a
deliberate operator action. Use `rotate-reality-key.sh` rather than editing
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

This means routine cred rotation (`rotate-short-ids.sh`) is a flag-day-free
operation — any client that polls within 2h of rotation picks up the new
short_id while the old one is still valid; any client that polls *after*
the rotation gets the new short_id from the start. The poll interval has
to be < 2h for this to work; the default Windows hourly task and mobile
`auto_update_interval: 60min` both qualify.

What this *doesn't* cover:

- **Reality keypair rotation** — the public key is shared across all users,
  so there's no "old + new" slot on the server. `rotate-reality-key.sh`
  is a flag day; clients without a recent poll will fail Reality until they
  re-fetch. Other protocols on the same client are unaffected, and
  `urltest` keeps traffic moving on whichever non-Reality protocol is
  fastest during the rotation window.
- **hy2 cert rotation** — the cert is pinned by every client; same
  flag-day model. `rotate-hy2-cert.sh` posts a notification (via `NOTIFY`
  if set) so operators are aware of the temporary hy2 outage; urltest
  carries traffic on Reality / ShadowTLS / WS-CDN until clients poll the
  new pin.

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
- `test_carol` — multi-country traveller without home_wg

Run as `python3 tests/test_render.py` (assert) or `--update` (regenerate
goldens). When you intentionally change rendered output, regenerate and
eyeball the diff in `git diff tests/goldens/` before committing.

CI runs the same harness on every push (see [`.github/workflows/`](https://github.com/) once added at Stage 5).
