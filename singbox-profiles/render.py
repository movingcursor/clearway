#!/usr/bin/env python3
"""
singbox-profiles generator.

Reads profiles.yaml and renders per-device client configs, installers, and
(eventually) server-side user blocks. Composition is dict-level: each fragment
function returns a Python dict describing a piece of the final sing-box JSON,
and the composer merges them. Comments live in fragment source for humans
reading this file; output JSON is pure JSON (no comments).

See profiles.yaml for the schema and CLI examples.
"""
import argparse
import datetime
import difflib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import jsonschema
import yaml


# Schema for profiles.yaml — validated at manifest load time so typos fail
# loudly with a precise path instead of sliding through to a cryptic
# `sing-box check` error buried in rendered output. Kept deliberately loose
# on unknown keys (additionalProperties stays default-allow) because the
# renderer reads a fixed set and ignores the rest; the schema guards the
# positive shape, not the negative.
PROFILES_SCHEMA = {
    'type': 'object',
    'required': ['defaults', 'users'],
    'properties': {
        'defaults': {
            'type': 'object',
            'properties': {
                'utls_fingerprint': {'type': 'string'},
                'reality': {'type': 'object', 'required': ['server', 'server_port', 'handshake_sni', 'flow']},
                'ws-cf':   {'type': 'object', 'required': ['host', 'path', 'port']},
                'shadowtls': {'type': 'object', 'required': ['server_port', 'version', 'sni', 'shadowsocks_method']},
                'hy2':       {'type': 'object', 'required': ['server_port', 'sni']},
                # Optional list of CIDRs (e.g. the server's public IPs) that
                # should always route Direct from a client to avoid TUN
                # hairpin loops on hy2's outer QUIC. Omit / leave empty for
                # single-host deployments where the proxy hostname only
                # resolves to one IP that's already covered by other rules.
                'proxy_server_ips': {'type': 'array', 'items': {'type': 'string'}},
            },
        },
        'users': {
            'type': 'object',
            'patternProperties': {
                # Usernames: lowercase a-z, 2–24 chars. Narrow enough to catch
                # accidental capitals or whitespace that would then become
                # directory / URL path segments.
                '^[a-z][a-z0-9_-]{1,23}$': {
                    'type': 'object',
                    'required': ['countries', 'protocols', 'devices'],
                    'properties': {
                        'countries': {
                            'type': 'array',
                            'items': {'enum': ['cn', 'ru', 'ir']},
                        },
                        'protocols': {
                            'type': 'array',
                            # 'awg' is valid here but never emits a sing-box
                            # outbound — AWG lives in a standalone wg-quick
                            # .conf consumed by the Amnezia VPN app, not in
                            # the sing-box profile (deliberate two-app split,
                            # see docs/architecture.md). Including 'awg' in
                            # protocols flips the per-user awg.conf emission
                            # and the server-side [Peer] block; nothing in
                            # the sing-box outbound list changes.
                            'items': {'enum': ['reality', 'ws-cf', 'shadowtls', 'hy2', 'awg']},
                            'minItems': 1,
                        },
                        # Stage-3 hook: country-derived selector default can be
                        # overridden per-user. Schema-validated here in stage 1
                        # so .yaml authors can't typo. AWG is a valid value but
                        # picking it as preferred for the sing-box selector
                        # would be a no-op (AWG isn't a sing-box outbound) —
                        # documented in the schema, not enforced, since stage 3
                        # will be the one wiring this into selector emission.
                        'preferred_protocol': {
                            'enum': ['reality', 'ws-cf', 'shadowtls', 'hy2', 'awg'],
                        },
                        # Optional pin of the user's AWG IPv4 address (CIDR, /32
                        # convention). If omitted the renderer auto-allocates
                        # from .secrets.yaml's awg.subnet via a deterministic
                        # hash-of-username probe — stable across runs so a fresh
                        # render doesn't reshuffle every peer's address.
                        'awg_address': {'type': 'string'},
                        'admin': {'type': 'boolean'},
                        # Per-user uTLS fingerprint. Structural (chosen by
                        # operator to spread JA3/JA4 signatures across the
                        # household), not a secret — lives in profiles.yaml,
                        # not in .secrets.yaml. Enum-gated to catch typos
                        # and block sing-box values that wouldn't pass
                        # `sing-box check` anyway.
                        'utls_fingerprint': {
                            'enum': ['chrome', 'firefox', 'safari', 'ios', 'android',
                                     'edge', 'random', 'randomized', '360', 'qq'],
                        },
                        # Per-user ShadowTLS SNI override. If unset, the
                        # renderer picks from defaults.shadowtls.sni_pool
                        # via hash(username) — stable and automatic. Set
                        # this to pin a specific SNI for a user (useful
                        # if one pool member becomes unreliable). Must be
                        # a hostname; not enum-gated because the pool is
                        # fully operator-controlled.
                        'shadowtls_sni': {'type': 'string'},
                        'home': {
                            'type': 'object',
                            'properties': {
                                'country': {'type': 'string'},
                                'home_egress_countries': {'type': 'array', 'items': {'type': 'string'}},
                                'home_egress_tlds':      {'type': 'array', 'items': {'type': 'string'}},
                            },
                        },
                        'devices': {
                            'type': 'array',
                            'minItems': 1,
                            'items': {
                                'type': 'object',
                                'required': ['type', 'name'],
                                'properties': {
                                    'type': {'enum': ['mobile', 'windows']},
                                    'name': {'type': 'string', 'pattern': '^[a-z0-9][a-z0-9_-]*$'},
                                },
                            },
                        },
                    },
                },
            },
            # No user outside the pattern is allowed — catches capitalised
            # usernames that would otherwise render into the served directory
            # tree with case inconsistency.
            'additionalProperties': False,
        },
    },
}


def _validate_manifest_schema(manifest):
    """
    Raise a precise SystemExit with a JSONPath-like location when profiles.yaml
    doesn't match the schema. Better signal than sing-box check's post-render
    errors which point at generated output, not the source.
    """
    try:
        jsonschema.validate(manifest, PROFILES_SCHEMA)
    except jsonschema.ValidationError as e:
        loc = '/'.join(str(p) for p in e.absolute_path) or '<root>'
        sys.exit(f'profiles.yaml schema error at {loc}: {e.message}')


ROOT = Path(__file__).parent.resolve()
MANIFEST = ROOT / 'profiles.yaml'
SECRETS = ROOT / '.secrets.yaml'     # credentials (auto-managed, 0600)
HOME_WG_DIR = ROOT / 'home_wg'       # externally-generated WG .conf files
SRV = ROOT / 'srv' / 'p'
TEMPLATE_INSTALLER = ROOT / 'templates' / 'install-singbox.template.ps1'
GENERATE_INSTALLER = ROOT / 'generate-installer.sh'
SECRETS_FILE = ROOT / 'secrets.txt'  # user → secret mapping (kept for compat)

# Server sync paths. Default layout: this renderer at <repo>/singbox-profiles/,
# the docker-compose stack at <repo>/singbox-server/. Override SINGBOX_SERVER_DIR
# to point elsewhere (e.g. for a deployment where the two halves don't sit in
# the same repo).
SERVER_DIR = Path(os.environ.get('SINGBOX_SERVER_DIR', str(ROOT.parent / 'singbox-server'))).resolve()
SERVER_CONFIG = SERVER_DIR / 'config.json'
SERVER_TEMPLATE = ROOT / 'templates' / 'singbox-server.template.jsonc'
SERVER_RESTART = SERVER_DIR / 'safe-restart.sh'
# Optional .env file consulted for host-wide vars (VNIC bind IPs etc.) when
# they're not already set in the process environment. Defaults to <repo>/.env;
# override via CLEARWAY_ENV_FILE. Process-env always wins so docker-compose
# --env-file passes through transparently.
ENV_FILE = Path(os.environ.get('CLEARWAY_ENV_FILE', str(ROOT.parent / '.env')))


def _env_or_dotenv(key, default=None):
    """Resolve a single env var: process env first, then repo-level .env file,
    else `default`. Used by config-init paths that need to pick up vars set in
    .env even when render.py wasn't invoked with the env exported (e.g. a bare
    `python3 render.py` from any shell). _read_env is the strict variant for
    required vars; this is the optional-with-fallback variant."""
    if key in os.environ:
        return os.environ[key]
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return default


# Public hostname that serves per-user profile directories under /p/<secret>/.
# Used in README URLs, the Windows installer one-liner, and secrets.txt header.
# Resolved with .env fallback (matches the pattern other host-wide vars use)
# so a bare `python3 render.py` from any shell picks up the right value
# without needing the operator to remember to export PROFILE_HOST first —
# previously the absence of the env var silently rendered URLs against
# `profile.example.com`. To change it: update PROFILE_HOST in the repo-level
# .env, the Traefik Host() rule in singbox-server/compose.yaml, and the DNS
# A record, then re-run ./render.py to regenerate READMEs + installers.
PROFILE_HOST = _env_or_dotenv('PROFILE_HOST', 'profile.example.com')


def _read_env(keys):
    """Resolve a set of vars from the process environment first, falling back
    to the repo-level .env file. Used to substitute host-wide vars (VNIC IPs)
    into the server template. Exits if any requested key is unresolved — we
    don't want a silent bind to the literal placeholder at render time."""
    found = {k: os.environ[k] for k in keys if k in os.environ}
    missing = set(keys) - set(found)
    if missing and ENV_FILE.exists():
        for raw in ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip()
            if k in missing:
                # Strip surrounding quotes if any; keep inner content as-is.
                v = v.strip().strip('"').strip("'")
                found[k] = v
        missing = set(keys) - set(found)
    if missing:
        sys.exit(
            f'env vars missing (set in process env or {ENV_FILE}): {sorted(missing)}'
        )
    return found
# Hysteria2 TLS cert (self-signed; CN/SAN should be a plausible cover
# hostname for your deployment — SAN is required; Go TLS silently
# rejects SAN-less leaf certs even when pinned, see docs/hazards.md).
# Read at render time and inlined into every client's hy2 outbound as
# tls.certificate so clients pin this exact cert instead of the
# insecure:true fallback. When the cert is regenerated the next render
# picks up the new PEM automatically, but clients that haven't fetched
# the new manifest will fail hy2 TLS until they do — treat cert rotation
# as a client-wide rotation event.
HY2_CERT = SERVER_DIR / 'hy2.crt'
PENDING_ROTATIONS = ROOT / '.pending-rotations.yaml'
ROTATION_TTL_HOURS = 2  # credentials kept alive for 2h after removal from manifest

# AWG paths. awg-server/ is a sibling of singbox-server/ — separate container,
# separate config, deliberate isolation (see docs/architecture.md). Override
# CLEARWAY_AWG_SERVER_DIR for non-standard layouts; defaults match the in-repo
# layout used by tests + the production deploy.
AWG_SERVER_DIR = Path(os.environ.get('CLEARWAY_AWG_SERVER_DIR',
                                     str(ROOT.parent / 'awg-server'))).resolve()
AWG_SERVER_CONFIG = AWG_SERVER_DIR / 'config' / 'awg0.conf'
AWG_CLIENT_TEMPLATE = ROOT / 'templates' / 'awg-client.conf.template'
AWG_SERVER_TEMPLATE = ROOT / 'templates' / 'awg-server.conf.template'
AWG_SERVER_RESTART = AWG_SERVER_DIR / 'safe-restart.sh'

# AWG obfuscation params (Jc/Jmin/Jmax/S1/S2/H1-H4) and keypair / subnet /
# port / endpoint live in .secrets.yaml under the top-level `awg:` block.
# Required keys are validated up-front in load_manifest so a partial config
# fails loud rather than silently rendering a half-functional .conf.
AWG_REQUIRED_KEYS = (
    'subnet', 'port', 'endpoint_host',
    'server_private_key', 'server_public_key',
    'Jc', 'Jmin', 'Jmax', 'S1', 'S2',
    'H1', 'H2', 'H3', 'H4',
)

# Country metadata. Each restricted country lives in its own
# `data/countries/<iso>.yaml` so adding/editing one is a self-contained
# change. The loader builds the same `COUNTRY` dict the rest of the
# renderer consumes — shape is back-compat with the old in-line literal.
# "uk" is a user-facing alias for geoip-gb (ISO), handled separately
# in the home-egress code path.
def _load_countries():
    countries = {}
    for path in sorted((ROOT / 'data' / 'countries').glob('*.yaml')):
        iso = path.stem
        countries[iso] = yaml.safe_load(path.read_text())
    return countries

COUNTRY = _load_countries()


# Home-country metadata. Each ISO-2 home country MAY have a file at
# `data/home_countries/<iso>.yaml` carrying egress-side extras tied to
# physically living there (today: VoWiFi ePDG routing for Italy). Keyed
# by user.home.country and applied automatically — no per-user manifest
# opt-in. Empty dict for ISO codes without a file is the no-op default,
# so adding a new home country requires no render.py change.
def _load_home_countries():
    out = {}
    home_dir = ROOT / 'data' / 'home_countries'
    if not home_dir.exists():
        return out
    for path in sorted(home_dir.glob('*.yaml')):
        iso = path.stem
        out[iso] = yaml.safe_load(path.read_text()) or {}
    return out

HOME_COUNTRY = _load_home_countries()


# Protocols whose outbound speaks TLS through Go's crypto/tls and
# therefore benefits from a per-user uTLS fingerprint (JA3/JA4
# decorrelation across users). Hysteria2 is omitted: it runs over QUIC
# with its own TLS impl and uTLS doesn't apply.
TLS_PROTOCOLS = {'reality', 'ws-cf', 'shadowtls'}


def _warn_unused_shadowtls_sni(users):
    """Soft check: user sets `shadowtls_sni` but doesn't have `shadowtls`
    in their protocols. Dead config — likely a leftover from a removed
    protocol. Harmless, doesn't error."""
    for name, user in users.items():
        if name.startswith('_'):
            continue
        if not user.get('shadowtls_sni'):
            continue
        if 'shadowtls' in user.get('protocols', []):
            continue
        print(f"warning: user {name!r} has 'shadowtls_sni' set but no "
              f"'shadowtls' protocol — field is unused",
              file=sys.stderr)


def _check_mobile_shadowtls_sni(users, defaults):
    """Hard check: mobile sing-box (SFA/SFI) silently fails ShadowTLS
    after one probe when the SNI comes from `defaults.shadowtls.sni_pool`
    (see hazards.md #2 / project_shadowtls_mobile_pooled_sni_break).
    Mobile users with shadowtls must carry a per-user `shadowtls_sni`
    override so they pin a known-good SNI instead of getting a hash-pick
    from the pool. Single-`sni` deployments (no pool) are unaffected
    and stay quiet. Exits non-zero with the full list of offenders."""
    pool = (defaults.get('shadowtls') or {}).get('sni_pool') or []
    if not pool:
        return
    bad = []
    for name, user in users.items():
        if name.startswith('_'):
            continue
        if 'shadowtls' not in user.get('protocols', []):
            continue
        if user.get('shadowtls_sni'):
            continue
        if any(d.get('type') == 'mobile' for d in user.get('devices', [])):
            bad.append(name)
    if bad:
        for name in bad:
            print(f"error: user {name!r} has shadowtls + a mobile device but "
                  f"no per-user 'shadowtls_sni' override; mobile sing-box "
                  f"breaks on pool-picked SNIs after one probe",
                  file=sys.stderr)
        sys.exit(1)


def _warn_unused_utls_fingerprint(users):
    """Inverse of the hard check: warn (don't error) when a user sets
    `utls_fingerprint` but has no TLS-bearing protocol. The field is
    harmless but dead — uTLS doesn't apply to Hysteria2 (QUIC), so the
    setting won't shape any ClientHello. Likely an editing mistake or
    leftover from a removed protocol."""
    for name, user in users.items():
        if name.startswith('_'):
            continue
        if not user.get('utls_fingerprint'):
            continue
        if set(user.get('protocols', [])) & TLS_PROTOCOLS:
            continue
        print(f"warning: user {name!r} has 'utls_fingerprint' set but no "
              f"TLS-bearing protocol — field is unused (uTLS doesn't apply "
              f"to hy2)",
              file=sys.stderr)


def _check_per_user_utls_fingerprint(users):
    """Hard check: a user with any TLS-bearing protocol must have a
    per-user `utls_fingerprint`. Without it, all such users collapse to
    `defaults.utls_fingerprint` and share one ClientHello signature —
    defeating the JA3/JA4 decorrelation the field exists for. Exits
    non-zero with the full list of offenders so they can be fixed in
    one pass."""
    bad = []
    for name, user in users.items():
        if name.startswith('_'):
            continue
        if user.get('utls_fingerprint'):
            continue
        tls_protos = sorted(set(user.get('protocols', [])) & TLS_PROTOCOLS)
        if tls_protos:
            bad.append((name, tls_protos))
    if bad:
        for name, tls_protos in bad:
            print(f"error: user {name!r} has TLS protocol(s) {tls_protos} "
                  f"but no per-user 'utls_fingerprint' (required for "
                  f"JA3/JA4 decorrelation)",
                  file=sys.stderr)
        sys.exit(1)


def _warn_missing_recommended_protocols(users):
    """Soft per-country protocol check. Prints a stderr warning when a
    user is missing a protocol that any of their countries recommends.
    Doesn't filter — operator may know better (e.g. testing a narrower
    protocol set, or excluding one for a specific user). Edit the
    `protocols.recommended` list in data/countries/<iso>.yaml to change
    what fires."""
    for name, user in users.items():
        if name.startswith('_'):
            continue
        user_protos = set(user.get('protocols', []))
        recommended_by = {}  # protocol → list of countries that recommend it
        for cc in user.get('countries', []):
            for p in (COUNTRY.get(cc, {}).get('protocols') or {}).get('recommended', []):
                recommended_by.setdefault(p, []).append(cc)
        missing = {p: ccs for p, ccs in recommended_by.items() if p not in user_protos}
        if missing:
            details = ', '.join(f"{p} (recommended for {','.join(ccs)})"
                                for p, ccs in sorted(missing.items()))
            print(f"warning: user {name!r} missing recommended protocol(s): {details}",
                  file=sys.stderr)

# Home-egress countries are expected to be ISO-2 country codes that SagerNet
# ships a geoip rule-set for. Maps user-facing label → ISO-2 code when the
# two differ (today: only `uk` → `gb`, since user-facing ccTLD is .uk but
# the ISO 3166-1 alpha-2 code is gb and SagerNet's rule-set is geoip-gb).
# Other entries should use their own ISO code as the user-facing label.
HOME_COUNTRY_ISO_OVERRIDE = {'uk': 'gb'}


def home_country(cc):
    """
    Build the geoip+tld entry for an ISO-2 home-egress country code.
    Always emits:
      geoip tag = 'geoip-<iso>'   (ISO-2 after uk→gb style overrides)
      tlds      = [cc]            (user-facing label — used for domain_suffix)
      url       = SagerNet 'geoip-<iso>.srs'

    Arbitrary TLDs (gTLDs, brand TLDs, political-bloc labels like `eu`) go in
    `home.home_egress_tlds`, not here — that field is domain_suffix-only.
    """
    iso = HOME_COUNTRY_ISO_OVERRIDE.get(cc, cc)
    return {
        'geoip': f'geoip-{iso}',
        'tlds':  [cc],
        'url':   f'https://cdn.jsdelivr.net/gh/SagerNet/sing-geoip@rule-set/geoip-{iso}.srs',
    }

# Shared DNS blocklists (always emitted).
BLOCKLIST_RULESETS = [
    {'tag': 'hagezi-tif',     'url': 'https://cdn.jsdelivr.net/gh/razaxq/dns-blocklists-sing-box@rule-set/hagezi-tif.srs', 'update_interval': '6h'},
    {'tag': 'hagezi-adblock', 'url': 'https://cdn.jsdelivr.net/gh/razaxq/dns-blocklists-sing-box@rule-set/hagezi-pro.srs', 'update_interval': '12h'},
]

# Domains that always route via 🔒 Trusted (never via a Direct country alias).
# Sourced from data/trusted_domains.txt so the list can be edited without
# touching code. One suffix per line; `#` comments and blank lines are ignored.
def _load_trusted_domains():
    path = ROOT / 'data' / 'trusted_domains.txt'
    out = []
    for raw in path.read_text().splitlines():
        line = raw.split('#', 1)[0].strip()
        if line:
            out.append(line)
    return out

TRUSTED_DOMAINS = _load_trusted_domains()

# IPs of our own proxy servers — always Direct to avoid tunnel loops.
# Populated from defaults.proxy_server_ips in profiles.yaml at manifest load
# time; the empty default keeps single-VPS deployments working without explicit
# config (the route rule below only fires if this list is non-empty).
PROXY_SERVER_IPS = []


# ---------------------------------------------------------------------------
# Fragment functions. Each returns a fragment of the final JSON.
# ---------------------------------------------------------------------------

def frag_log():
    return {'log': {'level': 'warn', 'timestamp': True}}


def frag_ntp(device):
    """
    NTP — keeps sing-box's clock aligned for Reality's ±90s handshake window.
    Only emitted on mobile devices (phones drift after airplane mode / bad
    cell towers). Windows has reliable w32time, no need.

    Server: pool.ntp.org. No explicit detour — sing-box 1.12+ rejects a
    detour that points to a bare direct outbound ("detour to an empty
    direct outbound makes no sense") and crashes at init. Omitting detour
    yields the same behavior (NTP rides the default direct path, outside
    the tunnel) without tripping the check. See docs/hazards.md.
    """
    if device['type'] != 'mobile':
        return {}
    return {'ntp': {
        'enabled': True,
        'server': 'pool.ntp.org',
        'server_port': 123,
        'interval': '30m',
    }}


def frag_inbound(device):
    """TUN inbound. Windows needs `interface_name` for the wintun driver."""
    tun = {
        'type': 'tun',
        'tag': 'tun-in',
        'address': ['172.16.0.1/30'],
        'auto_route': True,
        'strict_route': True,
        'mtu': 1380,
        'stack': 'mixed',
    }
    if device['type'] == 'windows':
        tun['interface_name'] = 'singbox-tun0'
    return {'inbounds': [tun]}


def frag_dns(countries, has_home, home_endpoint=None, bootstrap_ip='1.1.1.1', ws_cf_host=None):
    """
    DNS block. A/AAAA → fakeip, everything else → cloudflare_doh.

    Country DoH servers (DNSPod/Yandex/Shecan) are deliberately NOT emitted.
    Earlier iterations routed `.cn`/`.ru`/`.ir` TLDs through country DoH so
    travellers would get country-local CDN edges, but (a) sing-box has no
    DNS fallback, so a single country-DoH outage took down all of that TLD
    space (produced Chrome DNS_PROBE_POSSIBLE in production), and (b) it was
    never actually buying the CDN benefit — TCP egress is the proxy server
    regardless. With fakeip the hostname is reconstructed at the outbound
    egress and the egress resolver picks the CDN edge (country-local for
    in-country Direct traffic, server-local for tunnelled traffic).

    `bootstrap_ip`: IP used by the bootstrap_dns server (cleartext UDP:53
    for the pre-tunnel WS-CDN hostname lookup). Defaults to 1.1.1.1 which
    works for every resident user. Callers set this to a country-local
    resolver (Alibaba/Yandex/Shecan) when the user's manifest entry implies
    a physical location inside one of those countries — see frag_dns
    call-site in compose().

    `ws_cf_host`: the Cloudflare-fronted hostname (defaults['ws-cf'].host)
    that must resolve via bootstrap_dns BEFORE the tunnel is up (the ws-cf
    outbound needs the CF edge IP to dial). None = ws-cf not in use; no
    bootstrap entry emitted.
    """
    # cloudflare_doh detour: 🌍 Default when Restricted exists (multi-region
    # or home-enabled users), else 🔀 Proxy (single-country residents — keeps
    # DNS simple and avoids coupling it to Restricted's default=Direct).
    cloud_detour = '🌍 Default' if (has_home or len(countries) > 1) else '🔀 Proxy'

    # bootstrap_dns: fixed-IP UDP via direct. Previously was type=local
    # (OS resolver), which on Android with Private DNS enabled sent queries
    # to the system's DoT target — and because auto_route + strict_route
    # pulls all egress back into the TUN, the query looped to 172.16.0.2:853
    # (TUN peer) and timed out after 5s before any proxy could come up.
    # Going straight to a fixed-IP resolver via direct sidesteps the system
    # resolver entirely. detour → ➡️ Direct is legal because that outbound
    # carries udp_fragment:false (non-empty, see frag_outbounds). IP comes
    # from the `bootstrap_ip` arg (see docstring).
    servers = [
        {'tag': 'bootstrap_dns', 'type': 'udp', 'server': bootstrap_ip, 'server_port': 53, 'detour': '➡️ Direct'},
        {'tag': 'fakeip-server', 'type': 'fakeip', 'inet4_range': '198.18.0.0/15'},
        {'tag': 'cloudflare_doh', 'type': 'https', 'server': '1.1.1.1', 'server_port': 443, 'path': '/dns-query', 'detour': cloud_detour},
    ]
    # Bootstrap_dns is used for proxy-server hostnames that must resolve
    # BEFORE the tunnel is up (chicken-and-egg). The WS-CDN host goes here
    # because that outbound needs it resolved to dial Cloudflare's edge.
    # Home WG DDNS hostname is deliberately NOT in bootstrap: it resolves
    # via cloudflare_doh once any proxy outbound is up (Reality/hy2/ShadowTLS
    # use IPs, so they bootstrap without DNS), which keeps the DDNS hostname
    # off the ISP's resolver.
    bootstrap_domains = [ws_cf_host] if ws_cf_host else []

    # Global threat/ad rejects (hagezi) plus any country-specific ones
    # (e.g. chocolate4u's iran-* lists when IR is enabled).
    reject_tags = [b['tag'] for b in BLOCKLIST_RULESETS]
    for cc in countries:
        for rs in COUNTRY[cc].get('dns_reject_rulesets', []) or []:
            reject_tags.append(rs['tag'])

    rules = []
    if bootstrap_domains:
        rules.append({'domain': bootstrap_domains, 'server': 'bootstrap_dns'})
    rules.append({'rule_set': reject_tags, 'action': 'reject'})
    # Home WG DDNS hostname → cloudflare_doh (tunnelled, so ISP can't see
    # it). Must precede the A/AAAA→fakeip rule, otherwise fakeip would hand
    # back 198.18.x.x and the WG handshake UDP would loop back into the TUN.
    if home_endpoint:
        rules.append({'domain': [home_endpoint], 'server': 'cloudflare_doh'})
    rules.append({'query_type': ['A', 'AAAA'], 'server': 'fakeip-server'})
    rules.append({'server': 'cloudflare_doh'})

    # No `strategy` set: we fakeip every A/AAAA regardless, so the
    # v4-vs-v6 preference only matters for (rare) non-A/AAAA flows that
    # actually dial by hostname. Previously pinned to `ipv4_only` as a
    # defensive default, which made iOS apps double-lookup when the AAAA
    # came back empty. Omitting it lets sing-box use its default
    # (prefer_ipv4) without suppressing AAAA outright.
    return {'dns': {'independent_cache': True, 'servers': servers, 'rules': rules}}


def frag_home_endpoint(home, device_wg):
    """
    🏠 Home WireGuard endpoint. Detours via the 🚇 Home Carrier selector
    (see frag_selectors) which defaults to 🔀 Proxy — preserving the
    "WG wrapped in proxy protocol" behaviour that makes home access work
    from inside hostile DPI (CN/RU/IR). The user can flip 🚇 Home Carrier
    to ➡️ Direct in the dashboard to send raw WG straight to the residential
    endpoint, which is the right choice when:
      - low latency matters more than DPI cover (VoWiFi voice calls,
        gaming) AND
      - the current network can carry raw WG without throttling/blocking
        (most non-restricted networks, hotel Wi-Fi outside CN, etc.)
    Flipping back to 🔀 Proxy restores the wrapped behaviour. The detour
    indirection through a selector means a single dashboard toggle
    propagates to every home-egress flow (including the auto-applied VoWiFi
    rules) without re-rendering. Pre-2026-04-27 the detour was hardcoded
    to 🔀 Proxy with no escape hatch, so any per-flow direct-WG case (e.g.
    VoWiFi) required either a second WG identity or rebuilding the config.

    All WG knobs are user-configurable via the manifest (see `home:` and
    `home_wg:` blocks). Only fields present in the manifest are emitted —
    optional peer knobs like pre_shared_key / reserved are skipped when
    absent so we don't emit empty strings that sing-box then rejects.
    """
    # Normalise address (accept list or scalar for legacy manifests).
    addr = device_wg['address']
    if isinstance(addr, str):
        addr = [addr]

    ep = {
        'type': 'wireguard',
        'tag': '🏠 Home',
        # Detour through the 🚇 Home Carrier selector instead of pinning
        # 🔀 Proxy directly. Default of that selector is 🔀 Proxy (zero
        # behavioural change) but the user can flip to ➡️ Direct on the
        # fly for low-latency raw-WG. See frag_home_endpoint docstring.
        'detour': '🚇 Home Carrier',
        # MTU 1280 (was 1380). The 🏠 Home WG endpoint rides inside whichever
        # protocol 🔀 Proxy resolves to (country-derived default, or the
        # user's manual pick) — so WG packets are double-encapsulated:
        # TUN(1380) → outer proxy (TLS/QUIC overhead) → WG(outer). 1380 left
        # no headroom for the outer layer and would silently fragment or
        # PMTU-blackhole large TCP payloads over Home Egress. 1280 is the
        # IPv6 minimum and safe for any nested path.
        'mtu': home.get('mtu', 1280),
        'address': addr,
        'private_key': device_wg['private_key'],
    }
    # Optional interface-level knobs
    if device_wg.get('listen_port'):
        ep['listen_port'] = device_wg['listen_port']
    if 'system' in device_wg:
        ep['system'] = device_wg['system']

    peer = {
        'address': home['endpoint'],
        'port': home['endpoint_port'],
        'public_key': home['peer_public_key'],
        'allowed_ips': home.get('allowed_ips', ['0.0.0.0/0', '::/0']),
        'persistent_keepalive_interval': home.get('persistent_keepalive_interval', 25),
    }
    # Optional peer knobs — only emit if set (empty PSK confuses sing-box).
    if home.get('peer_pre_shared_key'):
        peer['pre_shared_key'] = home['peer_pre_shared_key']
    if home.get('reserved'):
        peer['reserved'] = home['reserved']

    ep['peers'] = [peer]
    return {'endpoints': [ep]}


def frag_outbound_reality(defaults, reality, fp=None):
    d = defaults['reality']
    if fp is None:
        fp = defaults.get('utls_fingerprint', 'chrome')
    # No packet_encoding here: Reality uses flow=xtls-rprx-vision which is
    # TCP-only. The xudp tag (VLESS UDP-over-TCP multiplexing) is silently
    # ignored by Vision and only served to imply UDP support that doesn't
    # exist. UDP-bearing apps (QUIC, WireGuard-over-Reality) would have
    # appeared to work at config-load time and then silently fail at
    # runtime. Route rule below (in frag_route) rejects UDP/443 so QUIC
    # falls back to TCP cleanly across all outbounds.
    return {
        'type': 'vless', 'tag': 'reality',
        'server': d['server'], 'server_port': d['server_port'],
        'uuid': reality['uuid'],
        'flow': d['flow'],
        'tls': {
            'enabled': True,
            'server_name': d['handshake_sni'],
            'utls': {'enabled': True, 'fingerprint': fp},
            'reality': {'enabled': True, 'public_key': d['public_key'], 'short_id': reality['short_id']},
        },
    }


def frag_outbound_ws_cf(defaults, ws_cf_uuid, fp=None):
    d = defaults['ws-cf']
    if fp is None:
        fp = defaults.get('utls_fingerprint', 'chrome')
    # ECH enabled: Cloudflare (the WS-CDN front host) publishes ECH configs
    # in its HTTPS DNS record. With ech.enabled + no explicit `config`,
    # sing-box auto-fetches the ECH config via its DNS layer (cloudflare_doh)
    # and encrypts the ClientHello — the real SNI is no longer visible to
    # a passive DPI. Without this, the WS-CDN fallback is the most DPI-exposed
    # path because the SNI is a stable custom hostname rather than a popular
    # cover.
    #
    # Multiplex (smux) intentionally DISABLED. Original rationale: chatty
    # HTTP/1.1 stacks open many short TCP connections, and over WS+CF that's
    # many handshakes (CF edge → backend → sing-box); smux lets multiple
    # VLESS streams ride one WS tunnel, amortising setup cost. Removed in
    # production after Android clients started failing WS-CDN connections
    # with `process multiplex stream: read multiplex stream request: EOF`
    # right after the WS upgrade — smux handshake collapsing mid-read.
    # Cloudflare's WebSocket proxy has tightened buffering/streaming and
    # interacts poorly with smux's binary framing at min_streams=4. Re-enable
    # only after confirming the bug is fixed upstream; per-request handshake
    # overhead without multiplex is negligible on modern CF edges. See
    # docs/hazards.md.
    return {
        'type': 'vless', 'tag': 'ws-cf',
        'server': d['host'], 'server_port': d['port'],
        'uuid': ws_cf_uuid,
        'packet_encoding': 'xudp',
        'tls': {
            'enabled': True, 'server_name': d['host'],
            'utls': {'enabled': True, 'fingerprint': fp},
            'ech': {'enabled': True},
        },
        'transport': {'type': 'ws', 'path': d['path'], 'headers': {'Host': d['host']}},
    }


def frag_outbound_shadowtls(defaults, stls_pw, user_ss_pw, fp=None, sni=None):
    # SS-2022 multi-user mode: client concatenates <server_psk>:<user_psk>.
    # server_psk = defaults['shadowtls']['shadowsocks_password'] (from
    # .secrets.yaml shared.shadowsocks_password, seeded in load_manifest).
    # user_ss_pw = per-user PSK (from .secrets.yaml users.<name>.shadowsocks_password).
    # sing-box parses the colon-joined form and sends EIH for user identity.
    #
    # sni override: per-user ShadowTLS SNI. Server runs wildcard_sni:
    # authed, so the client can present any plausible Oracle hostname.
    # After auth the TLS stream becomes Shadowsocks and the client never
    # cert-validates, so cert-vs-SNI mismatch on the ServerHello doesn't
    # matter. Caller passes the pre-resolved sni; fall back to the single
    # defaults.shadowtls.sni for back-compat + disabled-pool mode.
    d = defaults['shadowtls']
    ss_pw = f"{d['shadowsocks_password']}:{user_ss_pw}"
    if fp is None:
        fp = defaults.get('utls_fingerprint', 'chrome')
    if sni is None:
        sni = d['sni']
    return [
        {
            'type': 'shadowtls', 'tag': 'shadowtls-transport',
            'server': defaults['reality']['server'], 'server_port': d['server_port'],
            'version': d['version'], 'password': stls_pw,
            'tls': {
                'enabled': True, 'server_name': sni,
                'utls': {'enabled': True, 'fingerprint': fp},
            },
        },
        {
            'type': 'shadowsocks', 'tag': 'shadowtls',
            'server': defaults['reality']['server'], 'server_port': d['server_port'],
            'method': d['shadowsocks_method'], 'password': ss_pw,
            'detour': 'shadowtls-transport',
        },
    ]


def frag_outbound_hy2(defaults, pw):
    d = defaults['hy2']
    # Pin the server's self-signed hy2 cert by inlining its PEM lines into
    # tls.certificate (sing-box field is list-of-strings, one line each).
    # This replaces the old `insecure: true` trust mode — a network-path
    # attacker can no longer present their own cert and MITM the obfs'd
    # QUIC session. Cert rotation requires a client-wide manifest refetch;
    # there's no grace window in sing-box TLS (old cert stops validating
    # the moment the server presents the new one). NOTE: the pinned cert
    # MUST carry a subjectAltName matching the cert's CN — pinning bypasses
    # CA trust but NOT hostname validation, and Go rejects SAN-less certs
    # since 1.15 (see docs/hazards.md).
    cert_lines = HY2_CERT.read_text().strip().splitlines()
    # Brutal-CC bandwidth hints. Hysteria2's custom congestion control
    # ("brutal CC") uses these as the initial BDP estimate. Without them
    # brutal ramps up slowly and cold-start throughput is much lower than
    # the link can sustain. Values are a safe household envelope: most home
    # broadband is 20-100 Mbps up / 200-1000 Mbps down. Overestimating hurts
    # (congestion collapse when the path can't actually deliver); these are
    # conservative. Tune per-device later via `hy2_up_mbps/hy2_down_mbps`
    # in profiles.yaml if needed. Server-side per-user caps are not a thing
    # in sing-box (see project_singbox_hy2_no_per_user_caps memory).
    up_mbps = d.get('up_mbps', 30)
    down_mbps = d.get('down_mbps', 200)
    out = {
        # type stays 'hysteria2' — that's the upstream sing-box outbound type
        # name (not our identifier). Tag is our short canonical 'hy2'.
        'type': 'hysteria2', 'tag': 'hy2',
        'server': defaults['reality']['server'],
        'password': pw,
        'up_mbps': up_mbps,
        'down_mbps': down_mbps,
        'obfs': {'type': 'salamander', 'password': d['obfs_salamander_password']},
        'tls': {'enabled': True, 'server_name': d['sni'], 'alpn': ['h3'],
                'certificate': cert_lines},
    }
    # Port-hopping. If defaults.hy2.server_ports is set, emit
    # server_ports (a list of "low:high" range strings) and omit
    # server_port entirely — sing-box would otherwise dial server_port
    # first, defeating the per-IP UDP volume-marking mitigation that
    # port-hopping exists for. Server-side: a host iptables redirect
    # collapses the range back to defaults.hy2.server_port.
    # Without server_ports, behave as before (single-port). See
    # docs/hazards.md.
    server_ports = d.get('server_ports')
    if server_ports:
        out['server_ports'] = list(server_ports)
    else:
        out['server_port'] = d['server_port']
    return out


def protocol_outbound_tags(protocols):
    """Ordered list of the proxy outbound tags emitted by frag_outbounds.
    AWG is intentionally excluded — it doesn't ride in the sing-box profile.
    Tags are the canonical short identifiers from the protocol-naming
    convention; selector outbounds reference them by these strings."""
    tags = []
    if 'reality'   in protocols: tags.append('reality')
    if 'ws-cf'     in protocols: tags.append('ws-cf')
    if 'shadowtls' in protocols: tags.append('shadowtls')
    if 'hy2'       in protocols: tags.append('hy2')
    return tags


def proxy_selector_default(user, proxy_tags):
    """
    Pick the default value for the 🔀 Proxy selector. Order:
      1. Per-user `preferred_protocol` if set AND it's actually emitted as
         a sing-box outbound (i.e. it's in proxy_tags). 'awg' as a preferred
         value is rejected at validation time elsewhere — AWG isn't a sing-box
         outbound, so honoring it here would point the selector at a tag that
         doesn't exist.
      2. Single-country resident → that country's `protocols.default` from
         data/countries/<iso>.yaml, IF the user has it enabled.
      3. Fallback: 'hy2' if available (speed-first traveller default), else
         the first tag in proxy_tags (just emit *something* deterministic).

    Reasoning lives in docs/architecture.md (selector-default + manual escape
    hatch). No urltest is emitted — the previous design's '⚡ Fastest' urltest
    is gone, so the selector either lands on a working protocol via this
    function or the user manually flips on connection failure.
    """
    if not proxy_tags:
        # Should never happen — protocols list has minItems=1, and at least
        # one of the four sing-box-native protocols is required when no AWG.
        # If the user has only AWG, that's a profile-yaml-without-sing-box
        # scenario, but render.py still has to emit *some* config; pick a
        # placeholder that'd be obvious in dashboard if reached.
        return '➡️ Direct'

    pref = user.get('preferred_protocol')
    if pref and pref in proxy_tags:
        return pref

    countries = user.get('countries') or []
    if len(countries) == 1:
        only_cc = countries[0]
        cc_default = (COUNTRY.get(only_cc, {}).get('protocols') or {}).get('default')
        if cc_default and cc_default in proxy_tags:
            return cc_default

    if 'hy2' in proxy_tags:
        return 'hy2'
    return proxy_tags[0]


def frag_selectors(user, device, defaults):
    """
    Build all selector outbounds. As of stage 3 (the AWG rollout) the
    renderer no longer emits any urltest outbound — the previous '⚡ Fastest'
    urltest was the steady-state probe pattern that motivated swapping to
    pure selector + manual fallback. The 🔀 Proxy selector picks a
    country-derived default protocol; the user manually flips in the
    dashboard if the default breaks. See docs/architecture.md.
    """
    countries = user['countries']
    protocols = user['protocols']
    has_home = 'home' in user
    proxy_tags = protocol_outbound_tags(protocols)

    # 🔀 Proxy: protocol chooser. `default` is country-derived (see
    # proxy_selector_default). All proxy_tags are listed as outbounds so the
    # user can manually pick any enabled protocol from the dashboard.
    proxy_selector = {
        'tag': '🔀 Proxy', 'type': 'selector',
        'outbounds': list(proxy_tags),
        'default': proxy_selector_default(user, proxy_tags),
    }

    selectors = [proxy_selector]

    # 🌍 Default — catch-all proxy selector. Listed before 🏠 Home Egress
    # because most users interact with Default far more often (it's the
    # global routing knob) and dashboards render selectors in array order.
    default_outbounds = ['🔀 Proxy']
    if has_home:
        default_outbounds.append('🏠 Home')
    default_outbounds.append('➡️ Direct')
    selectors.append({
        'tag': '🌍 Default', 'type': 'selector',
        'outbounds': default_outbounds,
        'default': '🔀 Proxy',
    })

    # 🏠 Home Egress — only if user has home block.
    # Fallback chain points at 🌍 Default (not 🔀 Proxy directly) so that
    # flipping Default to Direct/Home propagates to home-country traffic
    # too — keeps the user's "where does my traffic exit" choice in one
    # place instead of needing to flip Proxy and Home Egress in lockstep.
    if has_home:
        selectors.append({
            'tag': '🏠 Home Egress', 'type': 'selector',
            'outbounds': ['🌍 Default', '🏠 Home', '➡️ Direct'],
            'default': '🏠 Home',
        })
        # 🚇 Home Carrier — the WG endpoint's detour points here, so flipping
        # this selector switches between proxy-wrapped (default, DPI-safe in
        # CN/RU/IR) and raw direct WG (lower latency, fine on non-restricted
        # networks, required for VoWiFi voice quality). Order: 🔀 Proxy first
        # so it's the visual default in the dashboard. ➡️ Direct as the only
        # alternative; we don't expose individual proxy protocols here because
        # the user already has 🔀 Proxy itself for that finer-grained pin.
        selectors.append({
            'tag': '🚇 Home Carrier', 'type': 'selector',
            'outbounds': ['🔀 Proxy', '➡️ Direct'],
            'default': '🔀 Proxy',
        })

    # 🔒 Trusted — sensitive accounts. Default = 🔀 Proxy so banking /
    # Apple / 1Password are always tunnelled regardless of where the user
    # has Default pointed (a resident with Default=Direct still wants
    # Trusted to tunnel). 🌍 Default is the secondary option for users
    # who want Trusted to follow their global routing choice instead.
    selectors.append({
        'tag': '🔒 Trusted', 'type': 'selector',
        'outbounds': ['🔀 Proxy', '🌍 Default'],
        'default': '🔀 Proxy',
    })

    # 🚨 Restricted — country-switch for geo-behaviour. Options include
    # 🌍 Default plus every country alias the user covers.
    restricted_opts = ['🌍 Default']
    for cc in countries:
        restricted_opts.append(f"{COUNTRY[cc]['flag']} {COUNTRY[cc]['label']}")
    # Default: if user has a single country and no home → assume they're a
    # resident ("local" mode) and default Restricted to their country (Direct).
    # Otherwise → 🌍 Default (Proxy), matching traveller behaviour.
    if len(countries) == 1 and not has_home:
        only_cc = countries[0]
        default_restricted = f"{COUNTRY[only_cc]['flag']} {COUNTRY[only_cc]['label']}"
    else:
        default_restricted = '🌍 Default'

    selectors.append({
        'tag': '🚨 Restricted', 'type': 'selector',
        'outbounds': restricted_opts,
        'default': default_restricted,
    })

    return selectors


def frag_outbounds(user, device, defaults):
    """Compose the full outbounds array in canonical order."""
    # udp_fragment: false makes this outbound non-empty so bootstrap_dns
    # (below) can legally detour to it in sing-box 1.12+ (a bare direct
    # outbound fails the "detour to an empty direct outbound" validation).
    # Explicit-false matches sing-box's own default — zero behavioral
    # change versus a bare direct — we just need a field present.
    # domain_strategy was removed on outbounds in 1.12 (deprecated), so
    # this is the cleanest no-op marker available.
    out = [{'tag': '➡️ Direct', 'type': 'direct', 'udp_fragment': False}]

    # Country alias Direct outbounds (🇨🇳/🇷🇺/🇮🇷 per user.countries).
    for cc in user['countries']:
        out.append({'tag': f"{COUNTRY[cc]['flag']} {COUNTRY[cc]['label']}", 'type': 'direct'})

    # Per-user uTLS fingerprint: each user gets a stable assignment from
    # the safe-pool, so a DPI classifier fingerprinting JA3/JA4 hashes
    # sees N distinct signatures across the household rather than one
    # shared "chrome" hash tying every device together. Falls back to
    # defaults.utls_fingerprint if unset.
    fp = user.get('utls_fingerprint') or defaults.get('utls_fingerprint', 'chrome')

    # Per-user ShadowTLS SNI resolution:
    #   1. users.<name>.shadowtls_sni     — operator-assigned override
    #   2. defaults.shadowtls.sni_pool    — stable hash-picked from pool
    #   3. defaults.shadowtls.sni         — single fallback (pool disabled)
    # The hash uses the username so the assignment is reproducible across
    # runs — rerunning render.py doesn't flip who sees which cover.
    stls_sni = user.get('shadowtls_sni')
    if not stls_sni:
        pool = defaults['shadowtls'].get('sni_pool') or []
        if pool:
            stls_sni = pool[int.from_bytes(user.get('_name', '').encode(), 'big') % len(pool)]
        else:
            stls_sni = defaults['shadowtls']['sni']

    # Protocol outbounds in canonical order. AWG is absent here by design —
    # AmneziaWG never rides in the sing-box profile (separate Amnezia VPN
    # app), see docs/architecture.md.
    if 'reality'   in user['protocols']: out.append(frag_outbound_reality(defaults, device['reality'], fp=fp))
    if 'ws-cf'     in user['protocols']: out.append(frag_outbound_ws_cf(defaults, user['ws_cf_uuid'], fp=fp))
    if 'shadowtls' in user['protocols']: out.extend(frag_outbound_shadowtls(defaults, user['shadowtls_password'], user['shadowsocks_password'], fp=fp, sni=stls_sni))
    if 'hy2'       in user['protocols']: out.append(frag_outbound_hy2(defaults, user['hy2_password']))

    # Selectors.
    out.extend(frag_selectors(user, device, defaults))
    return {'outbounds': out}


def frag_route(user, device, defaults):
    """route block: rule_set + rules."""
    countries = user['countries']
    has_home = 'home' in user
    # Proxy-server IPs come from defaults.proxy_server_ips (list of CIDRs).
    # Falls back to the module-level PROXY_SERVER_IPS for back-compat / tests.
    proxy_server_ips = defaults.get('proxy_server_ips') or PROXY_SERVER_IPS

    rule_sets = list(BLOCKLIST_RULESETS)
    # Country rule-sets (routing) + optional DNS-reject-only rule-sets
    # (declared at the route level because all rule-sets must be declared
    # there; they're only referenced by DNS rules).
    for cc in countries:
        for rs in COUNTRY[cc].get('rulesets', []):
            rule_sets.append(rs)
        blocked = COUNTRY[cc].get('blocked_ruleset')
        if blocked:
            rule_sets.append(blocked)
        for rs in COUNTRY[cc].get('dns_reject_rulesets', []) or []:
            rule_sets.append(rs)
    # Home-country geoip rule-sets (routed to 🏠 Home Egress). Every ISO-2
    # entry in home_egress_countries emits a geoip rule-set; TLD-only entries
    # live in home_egress_tlds and don't contribute here.
    if has_home:
        for cc in user['home'].get('home_egress_countries', []):
            m = home_country(cc)
            rule_sets.append({'tag': m['geoip'], 'url': m['url'], 'update_interval': '7d'})

    # download_detour = 🌍 Default so the ruleset fetch follows the user's
    # current top-level routing decision (Proxy when travelling, Direct when
    # resident). jsdelivr.net is CloudFlare-fronted and usually direct-
    # reachable outside CN/IR; tunnelling is still available for users who
    # need it.
    emitted_rulesets = []
    for rs in rule_sets:
        emitted_rulesets.append({
            'tag': rs['tag'], 'type': 'remote', 'format': 'binary',
            'url': rs['url'],
            'download_detour': '🌍 Default',
            'update_interval': rs.get('update_interval', '7d'),
        })

    # Route rules — order matters (first-match-wins).
    rules = [
        {'port': 53, 'action': 'hijack-dns'},
        # sniff populates metadata.domain from the sniffed SNI/host header.
        # Destination override (fakeip → real hostname) happens automatically
        # via the cache_file's fakeip store; no separate override field is
        # needed in sing-box 1.13 (and attempting one is a decode error).
        {'inbound': 'tun-in', 'action': 'sniff'},
    ]
    # Proxy-server bypass MUST come before the UDP/443 reject below:
    # hy2's own outer QUIC transport is UDP/443 to the proxy server IP, which
    # gets captured by the TUN (auto_route + strict_route) and re-enters
    # route.rules. First-match-wins, so the server-IP Direct rule must
    # fire before the blanket UDP/443 reject — otherwise hy2 can't
    # establish at all. Also covers TUN hairpin loops when a proxy hostname
    # resolves to one of our IPs. Empty list = rule omitted (single-VPS
    # deployments rely on the per-protocol server hostnames being already
    # excluded by the route rules they hit before this one).
    if proxy_server_ips:
        rules.append({'ip_cidr': proxy_server_ips, 'outbound': '➡️ Direct'})
    rules.extend([
        # Reject QUIC (UDP/443) globally for *client app* traffic. Reality-
        # Vision, ShadowTLS, and ws-cf are all TCP-only transports; when
        # 🔀 Proxy resolves to any of them, outbound UDP/443 silently
        # black-holes (HTTP/3 hangs, browsers do the slow fallback to TCP
        # after ~a second). Rejecting here forces the immediate fallback.
        {'network': 'udp', 'port': 443, 'action': 'reject'},
        # Reject DoT (TCP/853). Android's "Private DNS" setting emits DoT
        # from the system resolver; under our auto_route+strict_route TUN
        # it surfaces as connections to 172.16.0.2:853 (the TUN peer)
        # which direct-outbound tries and fails after 5s on cellular,
        # cluttering the log at every startup. It also closes the door on
        # any app that sidesteps port-53 hijack by using DoT directly.
        # Port 53 hijack-dns (first rule) catches the resulting fallback
        # queries, so there's no capability loss — DoT-speaking apps just
        # drop to Do53 and get captured by sing-box's DNS stack.
        {'network': 'tcp', 'port': 853, 'action': 'reject'},
        {'ip_is_private': True, 'outbound': '➡️ Direct'},
        {'domain_suffix': ['local', 'lan'], 'outbound': '➡️ Direct'},
    ])

    # SSH → Direct for admin users. Placed early so it wins before any geo
    # rule sweeps port 22 into a country selector (which could Proxy it).
    if user.get('admin'):
        rules.append({'port': 22, 'outbound': '➡️ Direct'})

    # Trusted domain list — only emit if the user has ≥2 protocols or is a
    # multi-country user (where differentiating makes sense). For simple
    # single-country users, keep the rule set lean.
    if len(user['protocols']) >= 2 or len(countries) > 1 or has_home:
        rules.append({'domain_suffix': TRUSTED_DOMAINS, 'outbound': '🔒 Trusted'})

    # Home-egress routing (before country rules, so home ccTLDs don't get
    # swept into country Restricted). Three sources, emitted in
    # specificity order (narrowest first):
    #   - home_country auto-extras (data/home_countries/<iso>.yaml) — today
    #     the only consumer is the `vowifi` block (ePDG domain + per-MNO
    #     CIDRs) for Italy. Emitted first because the IPs are inside the
    #     parent geoip-it set and a generic geoip-it match would otherwise
    #     swallow them with no functional difference but worse readability
    #     in the route trace. Auto-applied — driven solely by user.home.country.
    #   - home_egress_countries: ISO-2 codes → both a geoip rule and a
    #     domain_suffix entry (matching the ccTLD).
    #   - home_egress_tlds: arbitrary TLDs (eu, one, brand TLDs) → only
    #     domain_suffix, no geoip.
    if has_home:
        # 1. home-country auto-extras (vowifi etc.). HOME_COUNTRY is a
        #    dict-of-dicts; missing entry / missing section → no-op.
        home_cc = user['home'].get('country')
        home_extras = HOME_COUNTRY.get(home_cc, {}) if home_cc else {}
        vowifi = home_extras.get('vowifi') or {}
        vowifi_doms = vowifi.get('domain_suffix') or []
        vowifi_cidrs = vowifi.get('ip_cidr') or []
        # Emit as two rules (one matcher type each) rather than one mixed
        # rule so the route trace clearly attributes a hit to either the
        # DNS-driven path or the IP-direct (carrier-bundle) fallback.
        if vowifi_doms:
            rules.append({'domain_suffix': vowifi_doms, 'outbound': '🏠 Home Egress'})
        if vowifi_cidrs:
            rules.append({'ip_cidr': vowifi_cidrs, 'outbound': '🏠 Home Egress'})

        # 2. broad geoip + ccTLD home egress.
        home_geoips, home_tlds = [], []
        for cc in user['home'].get('home_egress_countries', []):
            m = home_country(cc)
            home_geoips.append(m['geoip'])
            home_tlds.extend(m['tlds'])
        for tld in user['home'].get('home_egress_tlds', []):
            home_tlds.append(tld)
        if home_geoips:
            rules.append({'rule_set': home_geoips, 'outbound': '🏠 Home Egress'})
        if home_tlds:
            rules.append({'domain_suffix': home_tlds, 'outbound': '🏠 Home Egress'})

    # GFW-blocked / RU-blocked: route to 🌍 Default so they always tunnel
    # (never to a country alias Direct that would dead-end them).
    blocked_tags = []
    for cc in countries:
        b = COUNTRY[cc].get('blocked_ruleset')
        if b:
            blocked_tags.append(b['tag'])
    if blocked_tags:
        rules.append({'rule_set': blocked_tags, 'outbound': '🌍 Default'})

    # Country routing → 🚨 Restricted. Uses `restricted_geosite` override if
    # set (e.g. RU uses `ru-available-only-inside` — only sites that actually
    # require a RU IP), falling back to the broader `geosite` otherwise.
    # geosite stays the DNS-level matcher for routing to the country DoH.
    for cc in countries:
        m = COUNTRY[cc]
        geosite_tag = m.get('restricted_geosite') or m.get('geosite')
        tags = [t for t in [geosite_tag, m.get('geoip')] if t]
        if tags:
            rules.append({'rule_set': tags, 'outbound': '🚨 Restricted'})

    # Final (catch-all). Multi-country/home → 🌍 Default. Single-country
    # residents → 🔀 Proxy directly (matches their current "proxy by default
    # for non-local traffic" behaviour).
    final = '🌍 Default' if (has_home or len(countries) > 1) else '🔀 Proxy'

    # default_domain_resolver: cloudflare_doh (DoH-through-tunnel), NOT
    # bootstrap_dns. bootstrap is plaintext 1.1.1.1:53 and exists only
    # for the pre-tunnel WS-CDN host lookup (WS-CDN users) — no other
    # domain lookup benefits from going plaintext. Today no route rule
    # matches on domain (all use rule_set/domain_suffix/ip_cidr), so this
    # is future-proofing: the moment a `{domain: [...], outbound: ...}`
    # route rule is added, the lookup goes tunnelled by default instead
    # of leaking to the ISP.
    return {'route': {
        'auto_detect_interface': True,
        'default_domain_resolver': 'cloudflare_doh',
        'final': final,
        'rule_set': emitted_rulesets,
        'rules': rules,
    }}


def _mobile_url_block(user, secret):
    """
    Format the per-device remote-profile URL block for the mobile section.
    Single mobile device → one URL in a fenced code block (matches the
    pre-rename look). Multiple mobile devices → per-device labelled URLs.
    Each URL points at the device's `singbox-<name>.json` (the post-rename
    naming convention).
    """
    mobile_devices = [d for d in user.get('devices', []) if d['type'] == 'mobile']
    if len(mobile_devices) == 1:
        d = mobile_devices[0]
        return f"```\nhttps://{PROFILE_HOST}/p/{secret}/{device_filename(user, d)}\n```"
    lines = []
    for d in mobile_devices:
        lines.append(f"- **{d['name']}** — `https://{PROFILE_HOST}/p/{secret}/{device_filename(user, d)}`")
    return '\n'.join(lines)


def render_user_readme(uname, user, defaults):
    """
    Generate a ready-to-send Markdown README for this user. Written from
    user's perspective (not admin's), covering setup for each device, the
    dashboard secret if Windows, the AWG section if AWG is enabled, and a
    credentials summary at the end.

    Reads from the merged manifest (load_manifest already wired secrets +
    home_wg into user / device fields), so everything needed is already here.

    Stage 3 changes: single-client recommendation (sing-box official only —
    Hiddify-Next compat dropped, see docs/architecture.md), AWG section
    appears only when 'awg' is in protocols, country-derived selector default
    surfaced as plain text instead of "leave on ⚡ Fastest".
    """
    uname_cap = uname.capitalize()
    secret = user['secret']
    protocols = user.get('protocols', [])
    countries = user.get('countries', [])
    has_home = 'home' in user
    has_awg = 'awg' in protocols

    # Device tables
    devices = user.get('devices', [])
    has_mobile = any(d['type'] == 'mobile' for d in devices)
    has_windows = any(d['type'] == 'windows' for d in devices)

    # Country + home summary lines
    country_labels = ', '.join(f"{COUNTRY[c]['flag']} {COUNTRY[c]['label']}" for c in countries)
    protocol_labels = ', '.join(protocols) if protocols else 'none'

    # Resolve the country-derived selector default for the user-facing copy.
    # Computed identically to proxy_selector_default so the README accurately
    # reflects what the dashboard will show on first launch.
    proxy_default_tag = proxy_selector_default(user, protocol_outbound_tags(protocols))

    # Device list
    device_lines = []
    for d in devices:
        label = f"**{d['name']}** ({d['type']})"
        if d['type'] == 'windows':
            label += f" — clash secret `{d['clash_secret']}`"
        device_lines.append(f'- {label}')
    device_block = '\n'.join(device_lines)

    # Selector-default explanation — auto-derive same way frag_selectors does
    if len(countries) == 1 and not has_home:
        only_cc = countries[0]
        restricted_default_explain = (
            f"Defaults to **{COUNTRY[only_cc]['flag']} {COUNTRY[only_cc]['label']}** — "
            f"{COUNTRY[only_cc]['label']}-local traffic (e.g. baidu, yandex) stays Direct "
            f"on your local network. Flip to **🌍 Default** when you're travelling outside "
            f"{COUNTRY[only_cc]['label']} so everything tunnels."
        )
    else:
        restricted_default_explain = (
            "Defaults to **🌍 Default** (tunnel everything). When you're physically in one "
            "of the listed countries, flip to the matching flag so local traffic stays Direct."
        )

    # Mobile install + profile import section. Sing-box official only —
    # Hiddify-Next is explicitly NOT recommended (it overrides imported
    # profile structure with its own urltest "Auto" mode, defeating the
    # country-derived selector default this profile is built around).
    mobile_section = f"""## Mobile (Android / iOS)

### Step 1 — Install sing-box

The official sing-box client. **Do not use Hiddify-Next** — it wraps imported
profiles in its own auto-switcher, defeating the country-derived default this
profile is built around (see Troubleshooting below).

- **Android (Play Store)** — [io.nekohasekai.sfa](https://play.google.com/store/apps/details?id=io.nekohasekai.sfa) — free.
- **iOS (App Store)** — [sing-box VT](https://apps.apple.com/us/app/sing-box-vt/id6673731168) — **$3.99 one-time** (one of the few paid clearway components; refundable through standard App Store flow if it doesn't work for you).
- **Android (sideload)** — [SagerNet/sing-box-for-android releases](https://github.com/SagerNet/sing-box-for-android/releases). Download the APK, verify the SHA256 listed in the GitHub release notes against `sha256sum sing-box-android-*.apk` before installing. Use this only when the Play Store is unavailable in your region.

### Step 2 — Import the profile

**Recommended — remote profile URL (auto-updates):**

{_mobile_url_block(user, secret)}

In the sing-box app: **Profiles** → **+** → **Type: Remote** → paste the URL → **Auto Update: 60 min** → **Save**.
The app fetches the config, validates it, reloads live. On server-side changes the phone picks up the new config on its next poll.

The URL itself is the credential (128-bit random path). Treat like any other sensitive string.

**Fallback methods** (if the URL is unreachable from your local network):
- **Local file import** — get the `.json` from admin via AirDrop / iCloud / Google Drive / email, then in the app: Profiles → + → Import from file.
- **URL import (one-off)** — host the file on any reachable URL, then Profiles → + → Import from URL.
""" if has_mobile else ''

    # Windows section. The installer fetches one specific config; for users
    # with multiple windows devices that's the first one in profiles.yaml
    # order (matches apply_plan's installer regen). Others would need their
    # own installer URL — not yet supported, same gap as before the
    # filename-rename.
    if has_windows:
        win_dev = next(d for d in devices if d['type'] == 'windows')
        win_filename = device_filename(user, win_dev)

        windows_section = f"""## Windows setup

Runs as a Windows service via NSSM, config auto-updates hourly, service restarts automatically on config change.

### Install (one command, admin PowerShell)

Open **PowerShell as Administrator** (Start menu → right-click PowerShell → *Run as administrator*), paste:

```powershell
iwr https://{PROFILE_HOST}/p/{secret}/install-singbox.ps1 -OutFile $env:TEMP\\sb.ps1; & $env:TEMP\\sb.ps1
```

What it does automatically:
- Downloads sing-box, NSSM (service wrapper), wintun.dll (TUN driver).
- Fetches + validates the latest `{win_filename}` from the server.
- Installs a Windows service `sing-box` (auto-starts at boot, restarts on crash).
- Registers a scheduled task that pulls config updates every hour.

Re-running the same one-liner is safe — works as upgrade / repair.

### Dashboard — `http://127.0.0.1:9090/ui`

Loopback-only (not reachable from your LAN). First time open, paste API secret:

```
{win_dev['clash_secret']}
```

Leave API Base URL at `http://127.0.0.1:9090`. Browser caches the secret in localStorage after that.

### Common operations (admin PowerShell)

```powershell
# Is the service running?
Get-Service sing-box

# Force an immediate config + version check
Start-ScheduledTask -TaskName 'sing-box config updater'

# Tail the updater log
Get-Content "C:\\Program Files\\sing-box\\logs\\update-config.log" -Tail 30

# Tail sing-box's own log
Get-Content "C:\\Program Files\\sing-box\\logs\\service.err.log" -Tail 30
```
"""
    else:
        windows_section = ''

    # Credentials summary
    creds_lines = [
        f"- **Profile URL**: `https://{PROFILE_HOST}/p/{secret}/`",
        f"- **Server**: `{defaults['reality']['server']}`",
    ]
    for d in devices:
        reality = d.get('reality', {})
        parts = [f"  - **{uname}-{d['name']}** ({d['type']})"]
        if reality:
            parts.append(f"Reality short_id `{reality['short_id']}`, UUID `{reality['uuid']}`")
        if d['type'] == 'windows':
            parts.append(f"clash secret `{d['clash_secret']}`")
        creds_lines.append(' — '.join(parts))
    if 'ws-cf' in protocols:
        creds_lines.append(f"- **ws-cf** UUID: `{user.get('ws_cf_uuid')}` (shared across your devices)")
    if 'hy2' in protocols:
        creds_lines.append(f"- **hy2** password: `{user.get('hy2_password')}`")
    if 'shadowtls' in protocols:
        creds_lines.append(f"- **ShadowTLS** password: `{user.get('shadowtls_password')}`")
        # SS-2022 multi-user EIH: client password is <server_psk>:<user_psk>.
        # Both halves shown for manual-config reference.
        creds_lines.append(
            f"- **Shadowsocks-2022** PSK: "
            f"`{defaults['shadowtls']['shadowsocks_password']}:{user.get('shadowsocks_password')}` "
            f"(server:user)"
        )
    creds_block = '\n'.join(creds_lines)

    # AWG section — only emitted when the user has 'awg' in protocols. Lives
    # in a separate Amnezia VPN app rather than the sing-box profile (the
    # two-app split is intentional; see docs/architecture.md). On iOS only
    # one VPN profile is active at a time, so users tap to switch between
    # sing-box and Amnezia VPN; Android allows both to coexist.
    awg_section = ''
    if has_awg:
        awg_section = f"""## AmneziaWG (parallel resilience tunnel)

Your profile includes an AmneziaWG (AWG) tunnel for protocol diversity. AWG
runs as a *separate* VPN in the Amnezia VPN app — not via the sing-box
client above. Use it when the sing-box-native protocols are blocked
(typically RU/IR; AWG is what Russians are actually deploying against
RKN/TSPU and what survives Iranian QUIC blackouts).

### Step 1 — Install Amnezia VPN

- **Android** — [amnezia-client GitHub releases](https://github.com/amnezia-vpn/amnezia-client/releases). Verify the SHA256 against the release notes before installing.
- **iOS** — [Amnezia VPN App Store](https://apps.apple.com/app/amneziavpn/id1600529900) — free.

### Step 2 — Import your AWG config

Download your wg-quick config:

```
https://{PROFILE_HOST}/p/{secret}/awg.conf
```

In the Amnezia app: **+** → **From file/URL** → paste the URL or import the
saved file. The app picks up the AWG obfuscation params automatically.

### iOS caveat — only one VPN active at a time

iOS allows exactly one VPN profile to be running. To switch between sing-box
and Amnezia VPN: open the *other* app and start its VPN — iOS automatically
deactivates the previous one. Android allows both VPNs to coexist on
different network namespaces, no manual switching required.
"""

    # Home egress block
    home_block = ''
    if has_home:
        home_entries = list(user['home'].get('home_egress_countries', [])) + list(user['home'].get('home_egress_tlds', []))
        home_countries = ', '.join(e.upper() for e in home_entries)
        home_block = (
            f"\n## Home egress\n\n"
            f"Traffic for **{home_countries}** routes through a WireGuard tunnel back to your "
            f"home network (physical country: {user['home']['country'].upper()}), giving geo-correct "
            f"CDN edges for those TLDs.\n"
        )

    # Assemble
    md = f"""# {uname_cap}'s sing-box VPN

Generated by the admin's renderer. Mirrors the live state of your profile; if something looks stale, ask to re-render.

## What you have

**Devices ({len(devices)}):**

{device_block}

**Protocols:** {protocol_labels}

**Region coverage:** {country_labels or 'none'}
{home_block}

## Selectors (dashboard / app)

Your config exposes a few selectors you can flip in the sing-box app / metacubexd dashboard:

- **🔀 Proxy** — which protocol to use. Defaults to **{proxy_default_tag}** (the protocol best suited to your region — see *Manual fallback* below).
- **🚨 Restricted** — how to treat traffic to the countries you cover. {restricted_default_explain}
- **🔒 Trusted** — sensitive domains (banking, 1Password, Apple, Microsoft). Always tunneled by default.

Changes apply instantly; no restart needed.

### Manual fallback — what to do if everything stops working

Your profile has **no auto-switching** between protocols. This is deliberate —
the constant probing required for auto-switching is itself a fingerprint
DPI systems can detect, so removing it tightens what censors see on the
wire. The tradeoff: when your default protocol gets blocked, you flip
**🔀 Proxy** by hand to a different protocol from the dropdown. The other
protocols in your config are listed alongside the default, all ready to
go — switching takes one tap. If none of them work, fall back to the AWG
tunnel (separate Amnezia VPN app — see below) or message the admin.

{mobile_section}
{windows_section}{awg_section}

## Credentials (keep private)

{creds_block}

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Everything times out | Default protocol blocked / probed | In the dashboard, change 🔀 Proxy from **{proxy_default_tag}** to another protocol (the dropdown lists every one in your config). If all fail, switch to the AWG tunnel via the Amnezia VPN app. |
| Local-country sites slow | 🚨 Restricted on 🌍 Default | Flip to your country flag for Direct routing. |
| Tunnel connects but pages don't load | DNS cache after a mode change | Toggle VPN off / on once. |
| PC: dashboard shows 404 | metacubexd not yet downloaded (first boot) | Wait 30 s, reload. |
| Rule-sets fail on first start | Proxy not healthy at boot | Wait 30 s; they download once 🔀 Proxy reaches its first usable protocol. |

---

If something's off, ping the admin. This file is rendered from the central manifest — the admin regenerates it whenever anything about your profile changes.
"""
    return md


def frag_experimental(device):
    """clash_api (windows only) + cache_file (everyone)."""
    exp = {
        'cache_file': {
            'enabled': True, 'path': 'cache.db',
            'store_rdrc': True, 'rdrc_timeout': '7d', 'store_fakeip': True,
        },
    }
    if device['type'] == 'windows':
        exp['clash_api'] = {
            'external_controller': '127.0.0.1:9090',
            'secret': device['clash_secret'],
            'external_ui': 'metacubexd',
            'external_ui_download_url': 'https://github.com/MetaCubeX/metacubexd/archive/refs/heads/gh-pages.zip',
            'external_ui_download_detour': '🔀 Proxy',
        }
    return {'experimental': exp}


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

def compose(user, device, defaults):
    """Compose the full sing-box JSON for one (user, device)."""
    has_home = 'home' in user
    cfg = {}
    cfg.update(frag_log())
    cfg.update(frag_ntp(device))
    home_endpoint = user.get('home', {}).get('endpoint') if has_home else None
    # bootstrap_ip selection: infer physical-in-country from the same signal
    # frag_dns uses for cloud_detour — single-country user with no home_egress.
    # That combo means the whole profile is tuned for living inside this
    # country (Restricted.default points at the country-alias Direct, country
    # TLDs don't route to Home Egress). The user's physical location is
    # implicit in the manifest shape, reusing it here keeps the bootstrap
    # selection in lockstep with cloud_detour and Restricted.default.
    # Multi-country and home-equipped users stay on 1.1.1.1.
    bootstrap_ip = '1.1.1.1'
    if len(user['countries']) == 1 and not has_home:
        override = COUNTRY.get(user['countries'][0], {}).get('bootstrap_dns')
        if override:
            bootstrap_ip = override
    # WS-CDN host needs pre-tunnel resolution; pass it only if this user has
    # WS-CDN enabled, otherwise no bootstrap entry is needed.
    ws_cf_host = defaults.get('ws-cf', {}).get('host') if 'ws-cf' in user.get('protocols', []) else None
    cfg.update(frag_dns(user['countries'], has_home, home_endpoint=home_endpoint,
                        bootstrap_ip=bootstrap_ip, ws_cf_host=ws_cf_host))
    if has_home and device.get('home_wg'):
        cfg.update(frag_home_endpoint(user['home'], device['home_wg']))
    cfg.update(frag_inbound(device))
    cfg.update(frag_outbounds(user, device, defaults))
    cfg.update(frag_route(user, device, defaults))
    cfg.update(frag_experimental(device))
    return cfg


# ---------------------------------------------------------------------------
# Filename + output helpers
# ---------------------------------------------------------------------------

def device_filename(user, device):
    """
    Compute the per-device filename: always `singbox-<name>.json`. Type info
    stays in the YAML (drives README section selection + Windows-installer
    wiring) but doesn't appear in the filename — encoding it twice gave us
    a conditional naming rule and a hardcoded URL in the README that only
    worked for single-device-of-type users. One name per device, always.
    """
    return f"singbox-{device['name']}.json"


def user_output_dir(user):
    return SRV / user['secret']


def emit_json(obj):
    """Canonical JSON output — stable for diffing."""
    return json.dumps(obj, indent=2, ensure_ascii=False) + '\n'


# ---------------------------------------------------------------------------
# Diff + apply
# ---------------------------------------------------------------------------

def unified_diff(a_text, b_text, a_label, b_label):
    return ''.join(difflib.unified_diff(
        a_text.splitlines(keepends=True),
        b_text.splitlines(keepends=True),
        fromfile=a_label, tofile=b_label, n=3,
    ))


def compute_client_plan(manifest):
    """Pure planning: returns list of (path, new_text, action, uname) for every
    per-device output file + per-user README + per-user awg.conf (when AWG is
    enabled for that user). Does not print or write."""
    defaults = manifest['defaults']
    awg_state = manifest.get('_awg')  # set by load_manifest only when AWG is in use
    sfile_users_for_awg = {}
    if awg_state:
        # Re-read .secrets.yaml only enough to fetch awg_private_key per user
        # (the merged manifest already has it on the user dict — pulling from
        # there avoids a second file read and guarantees consistency with the
        # validation pass).
        for uname, user in manifest['users'].items():
            if user.get('awg_private_key'):
                sfile_users_for_awg[uname] = {'awg_private_key': user['awg_private_key']}
    plan = []
    for uname, user in manifest['users'].items():
        uname_user = dict(user); uname_user['_name'] = uname
        outdir = user_output_dir(user)
        expected = set()
        for dev in user['devices']:
            cfg = compose(uname_user, dev, defaults)
            text = emit_json(cfg)
            fname = device_filename(user, dev)
            path = outdir / fname
            expected.add(fname)
            if path.exists():
                action = 'unchanged' if path.read_text() == text else 'modify'
            else:
                action = 'create'
            plan.append((path, text, action, uname))

        # Per-user AWG client config — only when the user has 'awg' in
        # protocols. Lives next to the sing-box JSONs so the served-URL tree
        # at /p/<secret>/awg.conf can be fetched by the Amnezia VPN app the
        # same way the sing-box app fetches /p/<secret>/singbox-<device>.json.
        # Filename intentionally `awg.conf` (not `awg-mobile.conf`) — the
        # config is platform-agnostic and a single user has at most one AWG
        # identity (one [Peer] on the server side per user, not per device).
        if awg_state and 'awg' in user.get('protocols', []):
            awg_text = _render_awg_client_conf(
                uname,
                user,
                {'users': sfile_users_for_awg},
                awg_state['block'],
                awg_state['addresses'][uname],
            )
            awg_path = outdir / 'awg.conf'
            expected.add('awg.conf')
            if awg_path.exists():
                action = 'unchanged' if awg_path.read_text() == awg_text else 'modify'
            else:
                action = 'create'
            plan.append((awg_path, awg_text, action, uname))

        # Per-user README with all setup instructions + credentials inlined.
        # The admin sends this to the user when onboarding them.
        readme_text = render_user_readme(uname, uname_user, defaults)
        readme_path = outdir / 'README.md'
        if readme_path.exists():
            action = 'unchanged' if readme_path.read_text() == readme_text else 'modify'
        else:
            action = 'create'
        plan.append((readme_path, readme_text, action, uname))

        if outdir.exists():
            for existing in outdir.glob('singbox-*.json'):
                if existing.name not in expected:
                    plan.append((existing, None, 'delete', uname))
            # Also clean up an awg.conf left over from when a user had AWG
            # enabled but no longer does — same delete pattern as the
            # singbox-*.json sweep above.
            stale_awg = outdir / 'awg.conf'
            if stale_awg.exists() and 'awg.conf' not in expected:
                plan.append((stale_awg, None, 'delete', uname))

    # awg-server config emission. Real [Interface] + per-user [Peer] blocks
    # rendered from .secrets.yaml + the AWG users' derived public keys. Bind-
    # mounted into the awg-server container at /etc/amneziawg/awg0.conf.
    if awg_state:
        srv_text = _render_awg_server_config(manifest, awg_state)
        srv_path = AWG_SERVER_CONFIG
        if srv_path.exists():
            action = 'unchanged' if srv_path.read_text() == srv_text else 'modify'
        else:
            action = 'create'
        plan.append((srv_path, srv_text, action, '<awg-server>'))

    return plan


def client_plan_has_changes(plan):
    return any(a[2] in ('modify', 'create', 'delete') for a in plan)


def _short_path(path):
    """Display path as repo-relative when possible, falling back to absolute.
    The awg-server stub lives at <repo>/awg-server/config/awg0.conf, outside
    ROOT (which is singbox-profiles/), so a strict relative_to(ROOT) raises."""
    try:
        return path.relative_to(ROOT)
    except ValueError:
        try:
            return path.relative_to(ROOT.parent)
        except ValueError:
            return path


def print_client_summary(plan):
    print('── Client plan ──────────────────────────────────────────────')
    if not plan:
        print('  (nothing)')
        return
    for path, _, action, uname in plan:
        print(f'  [{action:9}] {uname:12} {_short_path(path)}')


def print_client_diffs(plan):
    for path, text, action, uname in plan:
        if action == 'modify':
            print(unified_diff(path.read_text(), text, f'a/{path.name}', f'b/{path.name}'))
        elif action == 'create':
            print(f'── new file: {_short_path(path)} ({len(text.splitlines())} lines)')
        elif action == 'delete':
            print(f'── will delete: {_short_path(path)}')


def render_all(manifest, dry_run=False, auto_yes=False):
    """Standalone client-only flow (kept for backwards compat; unused by default)."""
    plan = compute_client_plan(manifest)
    print_client_summary(plan)
    if dry_run:
        print_client_diffs(plan)
        return
    if not client_plan_has_changes(plan):
        print('Nothing to do — everything up to date.')
        return
    if not auto_yes:
        if input('Show diff [y/N]? ').strip().lower() == 'y':
            print_client_diffs(plan)
        if input('Apply? [y/N] ').strip().lower() != 'y':
            print('Aborted.'); return
    apply_plan(plan, manifest)


def apply_plan(plan, manifest):
    """Write all changes. Also refresh installers + secrets.txt."""
    for path, text, action, uname in plan:
        if action == 'create' or action == 'modify':
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, path.with_suffix(path.suffix + '.prev'))
            path.write_text(text)
            print(f'  wrote {_short_path(path)}')
        elif action == 'delete':
            path.unlink()
            print(f'  deleted {_short_path(path)}')

    # Refresh secrets.txt FIRST — generate-installer.sh reads this file to
    # look up each user's URL secret, so a new user's installer would fail
    # if secrets.txt wasn't yet updated.
    lines = [
        '# user -> path-secret mapping for singbox-profiles remote-profile URLs',
        f'# Full URL: https://{PROFILE_HOST}/p/<secret>/singbox-<device-name>.json',
        f'# Maintained by render.py — edit profiles.yaml instead.',
        '',
    ]
    for uname, user in manifest['users'].items():
        lines.append(f"{uname}\t{user['secret']}")
    SECRETS_FILE.write_text('\n'.join(lines) + '\n')
    print(f'  wrote {SECRETS_FILE.relative_to(ROOT)}')

    # Refresh installers for every windows device. generate-installer.sh
    # takes a config filename; we drive it for the primary windows device
    # per user (the first one in profiles.yaml order). Multiple-windows-
    # per-user still emits one installer; the others would need their own
    # installer URL — not yet supported. Same gap as before the post-AWG
    # filename rename, just renamed.
    for uname, user in manifest['users'].items():
        win_devs = [d for d in user['devices'] if d['type'] == 'windows']
        if not win_devs:
            continue
        fname = device_filename(user, win_devs[0])
        webhook = user.get('notify_webhook', '')
        args = [str(GENERATE_INSTALLER), uname, fname]
        if webhook:
            args.append(webhook)
        # Pass PROFILE_HOST through so generate-installer.sh substitutes the
        # same host into __PROFILE_HOST__ that render.py uses for README URLs.
        env = {**os.environ, 'PROFILE_HOST': PROFILE_HOST}
        try:
            subprocess.run(args, check=True, capture_output=True, text=True, env=env)
            print(f'  regenerated installer for {uname}')
        except subprocess.CalledProcessError as e:
            print(f'  ! installer regen failed for {uname}: {e.stderr}')


def _parse_wg_conf(text):
    """
    Parse a standard WireGuard .conf file (INI format). Returns a dict:
      {'interface': {k:v, ...}, 'peer': {k:v, ...}}
    Keys preserve their original case so emitters can match WG conventions.
    Supports a single [Interface] + single [Peer] section (the household
    never has multi-peer home clients).
    """
    iface, peer = {}, {}
    section = None
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('['):
            section = line.strip('[]').strip().lower()
            continue
        if '=' not in line:
            continue
        k, _, v = line.partition('=')
        k, v = k.strip(), v.strip()
        target = iface if section == 'interface' else peer if section == 'peer' else None
        if target is None:
            continue
        target[k] = v
    return {'interface': iface, 'peer': peer}


def load_home_wg():
    """
    Load every home_wg/<user>-<device>.conf. Returns a nested dict:
      { 'alice': { 'pixel': {'interface': {...}, 'peer': {...}}, ... }, ... }
    Files that don't match the <user>-<device>.conf naming are skipped.
    Missing home_wg dir → empty dict (users with `home:` get errors later).
    """
    out = {}
    if not HOME_WG_DIR.is_dir():
        return out
    for f in sorted(HOME_WG_DIR.glob('*.conf')):
        stem = f.stem  # e.g. 'alice-pixel'
        if '-' not in stem:
            continue
        uname, dev_name = stem.split('-', 1)
        out.setdefault(uname, {})[dev_name] = _parse_wg_conf(f.read_text())
    return out


def _wg_iface_to_sbx(iface, peer):
    """Convert parsed WG conf dict → sing-box device-level home_wg fields
    plus user-level home block peer fields. Returns (device_wg, home_peer)."""
    ep_host, _, ep_port = peer['Endpoint'].rpartition(':')
    device_wg = {
        'address': [a.strip() for a in iface['Address'].split(',')],
        'private_key': iface['PrivateKey'],
    }
    if 'ListenPort' in iface:
        device_wg['listen_port'] = int(iface['ListenPort'])
    home_peer = {
        'endpoint': ep_host,
        'endpoint_port': int(ep_port),
        'peer_public_key': peer['PublicKey'],
        'allowed_ips': [a.strip() for a in peer.get('AllowedIPs', '0.0.0.0/0, ::/0').split(',')],
        'persistent_keepalive_interval': int(peer.get('PersistentKeepalive', 25)),
        'mtu': int(iface.get('MTU', 1380)),
    }
    if peer.get('PreSharedKey'):
        home_peer['peer_pre_shared_key'] = peer['PreSharedKey']
    return device_wg, home_peer


def load_secrets():
    """Load .secrets.yaml; returns empty skeleton if missing."""
    if not SECRETS.exists():
        return {'users': {}}
    data = yaml.safe_load(SECRETS.read_text()) or {}
    data.setdefault('users', {})
    return data


def save_secrets(data):
    """Rewrite .secrets.yaml preserving the top-of-file comment header."""
    header_lines = []
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            if line.strip().startswith('#') or line.strip() == '':
                header_lines.append(line)
            else:
                break
    if not header_lines:
        header_lines = [
            '# Auto-managed by render.py. 0600 permissions recommended.',
            '# Home WG identities live under ../home_wg/<user>-<device>.conf',
            '',
        ]
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)
    SECRETS.write_text('\n'.join(header_lines) + '\n' + body)
    # Ensure 0600 on every write.
    try:
        SECRETS.chmod(0o600)
    except OSError:
        pass


def _new_b64_key(n=32):
    """32 random bytes → base64. Used for shadowsocks/shadowtls passwords."""
    import base64
    return base64.b64encode(secrets.token_bytes(n)).decode()


def _detect_and_apply_renames(sfile, manifest, auto_yes=False):
    """
    Detect potential device renames: a device name in .secrets.yaml that has
    no counterpart in profiles.yaml (orphan) AND a device name in profiles.yaml
    that has no counterpart in .secrets.yaml (missing). When it's exactly 1:1
    for a user, treat it as a rename and move the credentials — avoids the
    "rename looks like a rotation" pitfall.

    Returns (sfile, renamed_report).
    """
    report = []
    for uname, user in manifest['users'].items():
        profile_names = {d['name'] for d in user.get('devices', [])}
        s_user = sfile.get('users', {}).get(uname, {})
        secret_names = set((s_user.get('devices', {}) or {}).keys())
        orphans  = sorted(secret_names - profile_names)
        missings = sorted(profile_names - secret_names)

        if not orphans or not missings:
            continue  # nothing to resolve for this user

        if len(orphans) == 1 and len(missings) == 1:
            old_name, new_name = orphans[0], missings[0]
            if auto_yes:
                action = 'rename'  # unambiguous 1:1 — rename is safer than rotation
            else:
                ans = input(
                    f'{uname}: "{old_name}" in .secrets.yaml has no device in profiles.yaml, '
                    f'and "{new_name}" in profiles.yaml has no credentials. '
                    f'Rename {old_name} → {new_name}? [Y/n] '
                ).strip().lower()
                action = 'rename' if ans in ('', 'y') else 'skip'
            if action == 'rename':
                s_user['devices'][new_name] = s_user['devices'].pop(old_name)
                report.append(f'  renamed {uname}/{old_name} → {uname}/{new_name} in .secrets.yaml')
        else:
            report.append(
                f'  ! {uname}: ambiguous (orphan devices: {orphans}; missing devices: {missings}). '
                f'Resolve in .secrets.yaml manually before re-running.'
            )
            if auto_yes:
                sys.exit(1)
    return sfile, report


def _autogen_missing(sfile, manifest):
    """
    Fill any missing credentials in .secrets.yaml for every user + device
    that needs them (based on their protocols / type / home flag). Returns
    (sfile, changed_summary: list of human-readable lines).

    Does NOT touch WG keys — those come from home_wg/*.conf, never auto-gen'd.
    """
    changed = []
    for uname, user in manifest['users'].items():
        s_user = sfile['users'].setdefault(uname, {})
        protocols = user.get('protocols', [])

        # User-level credentials
        def need(field, gen, label=None):
            if not s_user.get(field):
                s_user[field] = gen()
                changed.append(f'  gen {uname}.{label or field} = {s_user[field]}')

        need('secret', lambda: secrets.token_hex(16))
        if 'ws-cf' in protocols:
            need('ws_cf_uuid', lambda: str(uuid.uuid4()))
        if 'hy2' in protocols:
            need('hy2_password', lambda: secrets.token_urlsafe(22))
        if 'shadowtls' in protocols:
            need('shadowtls_password', lambda: secrets.token_urlsafe(24))
            # Per-user SS-2022 PSK (multi-user EIH mode). The server-level PSK
            # lives in shared.shadowsocks_password; this one is the per-user
            # identity key. 2h grace on rotation (kind=shadowsocks_users).
            need('shadowsocks_password', lambda: _new_b64_key(32))

        # Device-level credentials
        s_user.setdefault('devices', {})
        for dev in user.get('devices', []):
            s_dev = s_user['devices'].setdefault(dev['name'], {})
            if 'reality' in protocols:
                s_dev.setdefault('reality', {})
                if not s_dev['reality'].get('uuid'):
                    s_dev['reality']['uuid'] = str(uuid.uuid4())
                    changed.append(f'  gen {uname}/{dev["name"]}.reality.uuid = {s_dev["reality"]["uuid"]}')
                if not s_dev['reality'].get('short_id'):
                    s_dev['reality']['short_id'] = secrets.token_hex(8)
                    changed.append(f'  gen {uname}/{dev["name"]}.reality.short_id = {s_dev["reality"]["short_id"]}')
            if dev['type'] == 'windows' and not s_dev.get('clash_secret'):
                s_dev['clash_secret'] = secrets.token_hex(24)
                changed.append(f'  gen {uname}/{dev["name"]}.clash_secret = {s_dev["clash_secret"]}')

    return sfile, changed


# ---------------------------------------------------------------------------
# AWG (AmneziaWG) — validation, address allocation, per-user .conf emission.
#
# AWG never appears as a sing-box outbound (see schema comment). It runs in
# a separate awg-server container consumed by the Amnezia VPN app on the
# client side. The renderer's only AWG responsibilities are:
#   1. Validate that .secrets.yaml carries a complete `awg:` block when any
#      user has 'awg' in protocols, and that each AWG user has a private key.
#   2. Allocate stable per-user addresses from the configured subnet.
#   3. Emit per-user `awg.conf` files into srv/p/<secret>/ via the wg-quick
#      template at templates/awg-client.conf.template.
#   4. Emit a stub awg-server config (real content arrives in stage 2).
# ---------------------------------------------------------------------------

def _awg_users(manifest):
    """Iterator over (uname, user) pairs for users with AWG enabled. Underscore
    pseudo-users (anything starting with `_`) are skipped, matching the rest of
    the renderer's convention for in-band scratch keys."""
    for uname, user in manifest['users'].items():
        if uname.startswith('_'):
            continue
        if 'awg' in (user.get('protocols') or []):
            yield uname, user


def _validate_awg_block(awg_block, sfile, manifest):
    """
    Raise SystemExit with a precise message if the AWG configuration is
    incomplete. Called only when at least one user has 'awg' in protocols
    (no AWG users → no validation needed → renderer can run without an
    `awg:` block at all, which keeps non-AWG deployments unaffected by
    stage 1's additions).
    """
    if not awg_block:
        sys.exit(
            "users have 'awg' in protocols but .secrets.yaml has no top-level "
            "`awg:` block. Add one with subnet/port/endpoint_host/server_private_key/"
            "server_public_key plus the obfuscation params (Jc/Jmin/Jmax/S1/S2/H1-H4). "
            "See docs/quickstart.md for a generation snippet."
        )
    missing = [k for k in AWG_REQUIRED_KEYS if k not in awg_block]
    if missing:
        sys.exit(
            f"awg block in .secrets.yaml is missing required keys: {missing}. "
            f"All of {list(AWG_REQUIRED_KEYS)} must be present — partial configs "
            f"silently break clients (one mismatched obfuscation param times out "
            f"with no error, by AWG design)."
        )
    # subnet must parse and be large enough for the server address (.1) plus
    # at least one AWG user. /30 wouldn't fit two host addresses; /29 = 6 host
    # addrs, comfortable upper bound for the household scale clearway targets.
    try:
        net = __import__('ipaddress').ip_network(str(awg_block['subnet']), strict=False)
    except (ValueError, TypeError) as e:
        sys.exit(f'awg.subnet {awg_block["subnet"]!r} is not a valid CIDR: {e}')
    hosts = list(net.hosts())
    if len(hosts) < 2:
        sys.exit(
            f'awg.subnet {awg_block["subnet"]!r} too small ({len(hosts)} host '
            f'addrs); need at least 2 (server + 1 client). Use /29 or larger.'
        )

    # Each AWG user must have a private key in .secrets.yaml. The key is
    # operator-supplied (out-of-band `wg genkey`, paired with a server-side
    # [Peer] entry that uses the matching public key) — render.py never
    # auto-generates AWG keys because the server-side Peer list is paired
    # state and a silent rotation would orphan the client.
    bad = []
    for uname, _user in _awg_users(manifest):
        s_user = sfile.get('users', {}).get(uname, {})
        if not s_user.get('awg_private_key'):
            bad.append(uname)
    if bad:
        sys.exit(
            f"users with 'awg' in protocols but no awg_private_key in "
            f".secrets.yaml: {bad}. Generate per user with `wg genkey`, "
            f"paste under users.<name>.awg_private_key. The matching public "
            f"key is computed at render time."
        )


def _allocate_awg_addresses(manifest, awg_block):
    """
    Deterministic per-user IPv4 allocation inside `awg.subnet`. Reserves
    the first host address (.1) for the awg-server, then for each AWG user:
      - if `awg_address` is pinned in profiles.yaml, validate + use it
      - otherwise hash(username) → starting index, linear-probe forward
        until a free slot, fail loud if the subnet is full

    Hash-based probing means adding a user mid-life doesn't reshuffle
    existing peers (each is found at the same hash position on every run).
    Sorting users alphabetically before allocation keeps the iteration order
    stable in goldens.

    Returns: (server_address, {uname: ip_str}). server_address is an
    `ipaddress.IPv4Address`; uname→ip is plain strings (used directly in
    the [Peer] AllowedIPs line and the per-user [Interface] Address line).
    """
    import hashlib
    import ipaddress
    net = ipaddress.ip_network(str(awg_block['subnet']), strict=False)
    hosts = list(net.hosts())
    server_addr = hosts[0]   # .1 by convention; matches the [Interface] Address
                             # in awg-server.conf. Reserved — never assigned to
                             # any user, even via awg_address pin.
    available = hosts[1:]
    n = len(available)

    used = set()
    addresses = {}

    # Pass 1: process pinned awg_address values first so hash-allocated peers
    # avoid them. Errors here exit immediately so the operator sees the bad
    # pin instead of a downstream "subnet full" red herring.
    for uname, user in sorted(_awg_users(manifest)):
        pin = user.get('awg_address')
        if not pin:
            continue
        try:
            iface = ipaddress.ip_interface(pin)
            ip = iface.ip
        except ValueError as e:
            sys.exit(f'user {uname!r} has invalid awg_address {pin!r}: {e}')
        if ip not in net:
            sys.exit(
                f'user {uname!r} awg_address {ip} is outside awg.subnet {net} '
                f'— pin must be inside the configured subnet.'
            )
        if ip == server_addr:
            sys.exit(
                f'user {uname!r} awg_address {ip} collides with the reserved '
                f'server address {server_addr}.'
            )
        if ip in used:
            sys.exit(f'user {uname!r} awg_address {ip} duplicates another user.')
        used.add(ip)
        addresses[uname] = str(ip)

    # Pass 2: hash-allocate every remaining AWG user.
    for uname, user in sorted(_awg_users(manifest)):
        if uname in addresses:
            continue
        h = int(hashlib.sha256(uname.encode()).hexdigest(), 16)
        for i in range(n):
            cand = available[(h + i) % n]
            if cand not in used:
                used.add(cand)
                addresses[uname] = str(cand)
                break
        else:
            sys.exit(
                f'awg.subnet {net} is full — cannot allocate an address for '
                f'user {uname!r}. Increase the subnet (e.g. /23) or remove an '
                f'AWG user.'
            )

    return server_addr, addresses


def _render_awg_client_conf(uname, user, sfile, awg_block, address):
    """
    Substitute the per-user values + AWG block into the wg-quick client
    template. Pure string replace on __PLACEHOLDER__ tokens (matches the
    style used by singbox-server.template.jsonc and the PowerShell installer
    template).

    `address` comes from _allocate_awg_addresses; expressed as plain
    "10.66.66.20" (no CIDR) — the template appends /32 explicitly so a
    pinned awg_address in profiles.yaml that already carries /32 doesn't
    silently double up.
    """
    if not AWG_CLIENT_TEMPLATE.exists():
        sys.exit(f'AWG client template missing: {AWG_CLIENT_TEMPLATE}')
    tpl = AWG_CLIENT_TEMPLATE.read_text()
    s_user = sfile.get('users', {}).get(uname, {})
    subs = {
        '__USER_PRIVATE_KEY__':  s_user['awg_private_key'],
        '__USER_ADDRESS__':      f'{address}/32',
        '__SERVER_PUBLIC_KEY__': awg_block['server_public_key'],
        '__SERVER_HOST__':       str(awg_block['endpoint_host']),
        '__AWG_PORT__':          str(awg_block['port']),
        '__AWG_JC__':            str(awg_block['Jc']),
        '__AWG_JMIN__':          str(awg_block['Jmin']),
        '__AWG_JMAX__':          str(awg_block['Jmax']),
        '__AWG_S1__':            str(awg_block['S1']),
        '__AWG_S2__':            str(awg_block['S2']),
        '__AWG_H1__':            str(awg_block['H1']),
        '__AWG_H2__':            str(awg_block['H2']),
        '__AWG_H3__':            str(awg_block['H3']),
        '__AWG_H4__':            str(awg_block['H4']),
    }
    for k, v in subs.items():
        if k not in tpl:
            sys.exit(f'awg-client template placeholder {k} missing from {AWG_CLIENT_TEMPLATE}')
        tpl = tpl.replace(k, v)
    return tpl


def _x25519_public_from_private(b64_private):
    """
    Derive an X25519 / WireGuard public key from a base64-encoded 32-byte
    private key. Used for both the server keypair (cross-checking that
    awg.server_public_key in .secrets.yaml matches awg.server_private_key)
    and for each user's public key (which goes into the server config's
    [Peer] block — render.py never asks the operator to compute it).

    Returns the standard wg-format base64 public key. Raises ValueError on
    a malformed or wrong-length private key — caught upstream and turned
    into a precise SystemExit so the operator sees which user's key broke.
    """
    import base64
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    raw = base64.b64decode(b64_private, validate=True)
    if len(raw) != 32:
        raise ValueError(f'expected 32-byte X25519 private key, got {len(raw)} bytes')
    pub = X25519PrivateKey.from_private_bytes(raw).public_key()
    return base64.b64encode(
        pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode('ascii')


def _render_awg_server_config(manifest, awg_state):
    """
    Render the awg-server config from the wg-quick template + per-user
    [Peer] blocks. Replaces the stage-1 stub. AWG_SERVER_TEMPLATE provides
    the [Interface] block; this function appends one [Peer] per AWG user
    with their derived public key + allocated /32 address.

    Each user's public key is computed from `awg_private_key` at render
    time — no second source of truth, no risk of drift between client
    .conf and server [Peer] block.
    """
    if not AWG_SERVER_TEMPLATE.exists():
        sys.exit(f'AWG server template missing: {AWG_SERVER_TEMPLATE}')
    awg_block = awg_state['block']
    addresses = awg_state['addresses']
    server_address = awg_state['server_address']

    # Build [Peer] blocks for every AWG user. Sorted-by-username order
    # keeps the rendered file stable when peers come and go (golden-test
    # friendliness + clean diffs).
    peer_lines = []
    for uname, user in sorted(_awg_users(manifest)):
        try:
            pub = _x25519_public_from_private(user['awg_private_key'])
        except Exception as e:
            sys.exit(
                f'failed to derive AWG public key for user {uname!r}: {e}. '
                f'Check that .secrets.yaml.users.{uname}.awg_private_key is '
                f'a valid base64 32-byte X25519 private key (output of `wg genkey`).'
            )
        # PersistentKeepalive=0 server-side mitigates the WG keepalive-storm
        # bug (sing-box #3981 / wireguard-go shared upstream): an active
        # server-side keepalive on every peer causes battery drain on idle
        # mobile clients. Clients drive keepalive themselves (25s in the
        # client template) for NAT traversal — this only disables the
        # *server-initiated* keepalive. See docs/hazards.md.
        peer_lines.append(
            f'[Peer]\n'
            f'# {uname}\n'
            f'PublicKey = {pub}\n'
            f'AllowedIPs = {addresses[uname]}/32\n'
            f'PersistentKeepalive = 0\n'
        )
    peers_block = '\n'.join(peer_lines) if peer_lines else (
        '# (no AWG users in profiles.yaml — empty peer set; awg-server will\n'
        '#  start but accept no client handshakes until users are added.)\n'
    )

    tpl = AWG_SERVER_TEMPLATE.read_text()
    subs = {
        '__AWG_PORT__':               str(awg_block['port']),
        '__AWG_SERVER_PRIVATE_KEY__': awg_block['server_private_key'],
        '__AWG_JC__':                 str(awg_block['Jc']),
        '__AWG_JMIN__':               str(awg_block['Jmin']),
        '__AWG_JMAX__':               str(awg_block['Jmax']),
        '__AWG_S1__':                 str(awg_block['S1']),
        '__AWG_S2__':                 str(awg_block['S2']),
        '__AWG_H1__':                 str(awg_block['H1']),
        '__AWG_H2__':                 str(awg_block['H2']),
        '__AWG_H3__':                 str(awg_block['H3']),
        '__AWG_H4__':                 str(awg_block['H4']),
        '__PEERS__':                  peers_block,
    }
    for k, v in subs.items():
        if k not in tpl:
            sys.exit(f'awg-server template placeholder {k} missing from {AWG_SERVER_TEMPLATE}')
        tpl = tpl.replace(k, v)
    return tpl


def load_manifest(auto_yes=False):
    """
    Load profiles.yaml + .secrets.yaml + home_wg/*.conf, merge into one
    in-memory manifest dict, auto-generate any missing credentials, and
    persist new secrets back to .secrets.yaml.

    Merge rules:
      - credentials: .secrets.yaml overrides nothing in profiles.yaml
        (profiles.yaml shouldn't carry them any more), just provides them.
      - home_wg: the per-device home_wg block and the user-level home peer
        block (endpoint/public_key/etc.) come from home_wg/*.conf files.
      - If a user has `home:` but lacks a .conf for a device, render errors
        out with a precise message when that device is composed.
    """
    manifest = yaml.safe_load(MANIFEST.read_text())
    # Schema-validate upfront so typos ("windws", forgotten `countries`,
    # capitalised usernames) fail fast with a clear path instead of
    # cascading into a cryptic sing-box check error after full render.
    _validate_manifest_schema(manifest)
    sfile = load_secrets()
    home_wg = load_home_wg()

    # Auto-generate shared.shadowsocks_password if missing — purely symmetric,
    # safe to gen. Reality keypair + hy2 obfs are NOT auto-gen'd (paired / need
    # matching server state rotation).
    shared = sfile.setdefault('shared', {}) or {}
    if not shared.get('shadowsocks_password'):
        shared['shadowsocks_password'] = _new_b64_key(32)
        print(f'  gen shared.shadowsocks_password = {shared["shadowsocks_password"]}')
        save_secrets(sfile)
    # Server-side clash_api secret — used by the docker healthcheck and any
    # admin-side cscli-style poking. Auto-gen on first render so existing
    # installs pick it up transparently. 32-char hex matches the per-device
    # clash_secret format.
    if not shared.get('server_clash_secret'):
        shared['server_clash_secret'] = secrets.token_hex(24)
        print(f'  gen shared.server_clash_secret = {shared["server_clash_secret"]}')
        save_secrets(sfile)

    # Merge shared credentials (reality keypair, hy2 obfs password, ss PSK)
    # into defaults so the rest of the renderer sees unified state.
    if 'reality_public_key' in shared:
        manifest['defaults']['reality']['public_key'] = shared['reality_public_key']
    # reality_private_key was previously hardcoded in the server template;
    # moved to .secrets.yaml so rotate-reality-key.sh can update both halves
    # of the keypair atomically. Only used by render_server_text (clients
    # only need the public half).
    if 'reality_private_key' in shared:
        manifest['defaults']['reality']['private_key'] = shared['reality_private_key']
    if 'hy2_obfs_salamander_password' in shared:
        manifest['defaults']['hy2']['obfs_salamander_password'] = shared['hy2_obfs_salamander_password']
    manifest['defaults']['shadowtls']['shadowsocks_password'] = shared['shadowsocks_password']
    # Surface the server clash_api secret in defaults so render_server_text
    # can substitute it into the template without needing the full manifest.
    manifest['defaults']['server_clash_secret'] = shared['server_clash_secret']

    # Detect device renames (orphan in .secrets.yaml ↔ missing in profiles.yaml)
    # BEFORE auto-gen — otherwise the missing side would just get fresh creds
    # and the orphan would sit as dead data, effectively rotating the device's
    # credentials on a pure rename.
    sfile, rename_report = _detect_and_apply_renames(sfile, manifest, auto_yes=auto_yes)
    if rename_report:
        print('── Device renames ───────────────────────────────────────────')
        for line in rename_report:
            print(line)

    # Auto-generate any missing credentials before merging (so we never render
    # a device with a null secret).
    sfile, gen_report = _autogen_missing(sfile, manifest)
    if gen_report:
        print('── Auto-generated credentials ───────────────────────────────')
        for line in gen_report:
            print(line)

    # Persist .secrets.yaml if renames OR auto-gen changed anything. Renames
    # alone (without auto-gen) also need saving — otherwise the moved key
    # would reset to the orphan name on next run.
    if rename_report or gen_report:
        save_secrets(sfile)
        print(f'  wrote {SECRETS.relative_to(ROOT)}')

    # AWG validation + address allocation. Runs only when at least one user
    # has 'awg' in protocols; deployments without AWG never touch the awg block
    # and `.secrets.yaml` doesn't need to carry one. Errors here exit before
    # any output is written, so a partial AWG config can't half-render.
    awg_block = sfile.get('awg')
    if any(True for _ in _awg_users(manifest)):
        _validate_awg_block(awg_block, sfile, manifest)
        server_addr, awg_addresses = _allocate_awg_addresses(manifest, awg_block)
        # Stash on the manifest so downstream emit functions don't have to
        # re-load .secrets.yaml or re-allocate. Underscore prefix matches the
        # rest of the renderer's convention for in-band scratch state.
        manifest['_awg'] = {
            'block': awg_block,
            'server_address': str(server_addr),
            'addresses': awg_addresses,  # {uname: 'a.b.c.d'}
        }

    # Merge .secrets.yaml into manifest.users
    for uname, user in manifest['users'].items():
        s_user = sfile['users'].get(uname, {})
        # shadowsocks_password restored to per-user list 2026-04-22 after
        # switching to SS-2022 multi-user EIH. shared.shadowsocks_password is
        # still in use — but as the inbound-level server PSK, not the session key.
        # awg_private_key joined the merge in the AWG addition (stage 1) — only
        # surfaces on user dicts whose protocols actually include 'awg'; the
        # _validate_awg_block check above already failed if a user has 'awg'
        # but no key, so a missing field here means the user just doesn't use AWG.
        for field in ('secret', 'ws_cf_uuid', 'hy2_password',
                      'shadowtls_password', 'shadowsocks_password',
                      'awg_private_key', 'notify_webhook'):
            if field in s_user:
                user.setdefault(field, s_user[field])
        for dev in user.get('devices', []):
            s_dev = s_user.get('devices', {}).get(dev['name'], {})
            if 'reality' in s_dev:
                dev.setdefault('reality', s_dev['reality'])
            if 'clash_secret' in s_dev:
                dev.setdefault('clash_secret', s_dev['clash_secret'])

    # Merge home_wg/*.conf into user + device blocks
    for uname, user in manifest['users'].items():
        if 'home' not in user:
            continue
        user_wg = home_wg.get(uname, {})
        for dev in user.get('devices', []):
            conf = user_wg.get(dev['name'])
            if not conf:
                sys.exit(
                    f'missing home WG config for {uname}/{dev["name"]}.\n'
                    f'Expected file: {HOME_WG_DIR}/{uname}-{dev["name"]}.conf\n'
                    f'Drop a standard WireGuard .conf file (generated on the home router) at that path and re-run.'
                )
            dev_wg, home_peer = _wg_iface_to_sbx(conf['interface'], conf['peer'])
            dev['home_wg'] = dev_wg
            # All peer fields come from the conf — overwrite anything in
            # profiles.yaml's home: block (structural-only by design).
            for k, v in home_peer.items():
                user['home'][k] = v

    return manifest


def validate(manifest):
    """
    Render every device to a tmp dir and run `sing-box check` on each. Never
    touches live files. Exits non-zero if any config fails validation.
    """
    defaults = manifest['defaults']
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for uname, user in manifest['users'].items():
            user['_name'] = uname
            for dev in user['devices']:
                cfg = compose(user, dev, defaults)
                path = tmp / f"{uname}-{dev['name']}.json"
                path.write_text(emit_json(cfg))
                # Run sing-box check via the same docker image the server uses.
                r = subprocess.run(
                    ['docker', 'run', '--rm', '-v', f'{path}:/c.json',
                     'ghcr.io/sagernet/sing-box:latest', 'check', '-c', '/c.json'],
                    capture_output=True, text=True,
                )
                label = f"{uname}/{dev['name']}"
                if r.returncode == 0:
                    print(f'  ✓ {label}')
                else:
                    print(f'  ✗ {label}: {r.stderr.strip()}')
                    failures.append(label)
    if failures:
        sys.exit(f'\n{len(failures)} config(s) failed validation: {", ".join(failures)}')
    print(f'\nall {sum(len(u["devices"]) for u in manifest["users"].values())} configs valid')


# ---------------------------------------------------------------------------
# Server sync — regenerate the singbox-server config.json from
# the manifest, with 2-hour rotation grace so rotated credentials stay alive
# long enough for clients (hourly poll) to fetch the new config.
# ---------------------------------------------------------------------------

def _strip_jsonc(text):
    """Strip // line comments from JSONC, preserving strings and escapes."""
    out = []
    in_str = False
    esc = False
    i = 0
    while i < len(text):
        c = text[i]
        if esc:
            esc = False; out.append(c); i += 1; continue
        if c == '\\':
            esc = True; out.append(c); i += 1; continue
        if c == '"':
            in_str = not in_str; out.append(c); i += 1; continue
        if not in_str and c == '/' and i + 1 < len(text) and text[i + 1] == '/':
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        out.append(c); i += 1
    return ''.join(out)


def load_server_config():
    """Parse the live server config.json (returns dict)."""
    if not SERVER_CONFIG.exists():
        return None
    return json.loads(_strip_jsonc(SERVER_CONFIG.read_text()))


def manifest_server_view(manifest):
    """
    Build the desired-state view from the manifest — the 5 arrays the server
    config template expects. Order is stable (users appear in manifest order).
    """
    flow = manifest['defaults']['reality']['flow']
    shadowtls, shadowsocks, hy2, reality_users, short_ids, ws_cf = [], [], [], [], [], []

    for uname, user in manifest['users'].items():
        protocols = user.get('protocols', [])
        if 'shadowtls' in protocols and user.get('shadowtls_password'):
            shadowtls.append({'name': uname, 'password': user['shadowtls_password']})
            # Per-user SS-2022 EIH entry on the shared shadowsocks inbound.
            # Parallels shadowtls_users — same set of users, different secret.
            if user.get('shadowsocks_password'):
                shadowsocks.append({'name': uname, 'password': user['shadowsocks_password']})
        if 'hy2' in protocols and user.get('hy2_password'):
            hy2.append({'name': uname, 'password': user['hy2_password']})
        if 'ws-cf' in protocols and user.get('ws_cf_uuid'):
            ws_cf.append({'name': uname, 'uuid': user['ws_cf_uuid']})
        if 'reality' in protocols:
            for dev in user.get('devices', []):
                reality_users.append({
                    'name': f"{uname}-{dev['name']}",
                    'uuid': dev['reality']['uuid'],
                    'flow': flow,
                })
                short_ids.append(dev['reality']['short_id'])

    return {
        'shadowtls_users': shadowtls,
        'shadowsocks_users': shadowsocks,
        'hy2_users': hy2,
        'reality_users': reality_users,
        'reality_short_ids': short_ids,
        'ws_cf_users': ws_cf,
    }


def server_view(server_config):
    """
    Project the current live server config.json into the same shape as
    manifest_server_view — so we can diff them.
    """
    view = {
        'shadowtls_users': [],
        'shadowsocks_users': [],
        'hy2_users': [],
        'reality_users': [],
        'reality_short_ids': [],
        'ws_cf_users': [],
    }
    if not server_config:
        return view
    for ib in server_config.get('inbounds', []):
        tag = ib.get('tag')
        if tag == 'shadowtls-in':
            view['shadowtls_users'] = ib.get('users', [])
        elif tag == 'shadowsocks-in':
            view['shadowsocks_users'] = ib.get('users', [])
        elif tag == 'hy2-in':
            view['hy2_users'] = ib.get('users', [])
        elif tag == 'reality-in':
            view['reality_users'] = ib.get('users', [])
            view['reality_short_ids'] = ib.get('tls', {}).get('reality', {}).get('short_id', [])
        elif tag == 'ws-cf-in':
            view['ws_cf_users'] = ib.get('users', [])
    return view


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def load_pending_rotations():
    """Load the rotation state file. Returns a dict of lists per credential kind."""
    blank = {
        'shadowtls_users': [], 'shadowsocks_users': [], 'hy2_users': [],
        'reality_users': [], 'reality_short_ids': [], 'ws_cf_users': [],
    }
    if not PENDING_ROTATIONS.exists():
        return blank
    data = yaml.safe_load(PENDING_ROTATIONS.read_text()) or {}
    for k in blank:
        data.setdefault(k, [])
    return data


def save_pending_rotations(pending):
    """Write the rotation state file. Omits empty categories for tidiness."""
    trimmed = {k: v for k, v in pending.items() if v}
    if not trimmed:
        if PENDING_ROTATIONS.exists():
            PENDING_ROTATIONS.unlink()
        return
    header = (
        '# Auto-managed by render.py. Tracks credentials that were removed\n'
        '# from profiles.yaml but are kept alive on the server temporarily so\n'
        f'# clients (hourly poll) can fetch and switch. TTL: {ROTATION_TTL_HOURS}h\n'
        '# per entry. After expiry, the next ./render.py --server-apply drops\n'
        '# them from the server config.\n'
        '#\n'
        '# Do not edit by hand; change profiles.yaml and re-run.\n\n'
    )
    PENDING_ROTATIONS.write_text(header + yaml.safe_dump(trimmed, sort_keys=False, allow_unicode=True))


def _item_key(kind, item):
    """
    Stable identity key for deduping entries across manifest vs pending.
    Keying uses *authentication* fields only — not `name`, which is purely
    a cosmetic audit label. Consequences:
      - Renaming a device (e.g. alice-mobile → alice-phone, same UUID) is NOT
        a rotation. The server entry is renamed, no grace period.
      - Changing a UUID/password/short_id IS a rotation. Grace applies.
    """
    if kind == 'reality_short_ids':
        return item  # string
    if kind in ('shadowtls_users', 'shadowsocks_users', 'hy2_users'):
        return ('password', item.get('password'))
    if kind in ('reality_users', 'ws_cf_users'):
        return ('uuid', item.get('uuid'))
    return json.dumps(item, sort_keys=True)


def _expired(entry, now):
    exp = entry.get('expires_at')
    if not exp:
        return False
    try:
        exp_dt = datetime.datetime.fromisoformat(exp.replace('Z', '+00:00'))
    except ValueError:
        return False
    return exp_dt <= now


def compute_rotation_plan(manifest_view, server_view_data, pending):
    """
    Compare desired (manifest) vs live (server) state, accounting for still-
    valid pending-rotation entries. Returns:
        merged_view : what the new server config should contain (desired +
                      unexpired pending)
        new_pending : updated pending state (expired dropped, newly-removed
                      items added with 2h TTL, items now back in manifest
                      removed from pending)
        rotation_report : human-readable list of what's happening
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    expire_at = (now + datetime.timedelta(hours=ROTATION_TTL_HOURS))
    expire_iso = expire_at.isoformat(timespec='seconds').replace('+00:00', 'Z')

    report = []  # list of (kind, action, item, note)
    merged = {}
    new_pending = {}

    for kind in manifest_view:
        desired = manifest_view[kind]
        live = server_view_data.get(kind, [])
        pen = pending.get(kind, [])

        desired_keys = {_item_key(kind, x) for x in desired}
        live_keys    = {_item_key(kind, x) for x in live}

        # Expire old pending first. Items whose TTL passed get dropped
        # permanently — we record their keys so the "newly orphaned" scan
        # below doesn't immediately re-grace them (they're still on the live
        # server because we haven't written yet, but we WANT to drop them on
        # this write, not keep them another 2h).
        kept_pending = []
        just_expired_keys = set()
        for p in pen:
            key = _item_key(kind, p.get('value', p))
            if _expired(p, now):
                report.append((kind, 'expire', p.get('value', p), f'dropped (was kept since {p.get("added_at")})'))
                just_expired_keys.add(key)
            elif key in desired_keys:
                # Already back in the manifest — no longer needs grace.
                report.append((kind, 'reclaim', p.get('value', p), 'back in manifest'))
            else:
                kept_pending.append(p)

        kept_pending_keys = {_item_key(kind, p.get('value', p)) for p in kept_pending}

        # Items that were live but are neither desired, nor already in pending,
        # nor just-expired: newly rotated-out, move to pending with fresh TTL.
        for item in live:
            key = _item_key(kind, item)
            if key in desired_keys:
                continue  # still wanted
            if key in kept_pending_keys:
                continue  # already tracked
            if key in just_expired_keys:
                continue  # just expired — drop permanently, don't re-grace
            kept_pending.append({
                'value': item,
                'added_at': _now_iso(),
                'expires_at': expire_iso,
            })
            report.append((kind, 'grace-start', item, f'kept until {expire_iso}'))

        # Final merged = desired + still-valid pending (values).
        merged[kind] = list(desired) + [p['value'] for p in kept_pending]

        # Note new additions for the report.
        for item in desired:
            if _item_key(kind, item) not in live_keys:
                report.append((kind, 'add', item, 'new in manifest'))

        new_pending[kind] = kept_pending

    return merged, new_pending, report


def _fmt_json_array_for_template(items, indent=8):
    """
    Render a Python list as a JSON array with indentation matching the
    template's local context. The template places arrays at 6-space indent
    for `"users": [` and 10-space for `"short_id": [`, but when we emit them
    as `"users": [<...>]` with json.dumps we inherit no context. To keep the
    output pretty, we indent children at `indent`, close bracket aligned to
    `indent - 2`.
    """
    if not items:
        return '[]'
    child_ind = ' ' * indent
    close_ind = ' ' * (indent - 2)
    lines = [child_ind + json.dumps(x, ensure_ascii=False) for x in items]
    return '[\n' + ',\n'.join(lines) + '\n' + close_ind + ']'


def render_server_text(merged_view, defaults):
    """Substitute placeholders in the template with generated arrays + scalar
    values from profiles.yaml.defaults (SNIs, obfs password, etc.)."""
    tpl = SERVER_TEMPLATE.read_text()
    # Host-wide VNIC IPs read from process env / repo .env so the sing-box
    # listen addresses stay in sync with the compose-layer VNIC_* vars.
    host_env = _read_env({'VNIC_SECONDARY_IP'})
    subs = {
        '__VNIC_SECONDARY_IP__': host_env['VNIC_SECONDARY_IP'],
        '__USERS_SHADOWTLS__':   _fmt_json_array_for_template(merged_view['shadowtls_users'], indent=8),
        '__USERS_SHADOWSOCKS__': _fmt_json_array_for_template(merged_view['shadowsocks_users'], indent=8),
        '__USERS_HY2__':         _fmt_json_array_for_template(merged_view['hy2_users'], indent=8),
        '__USERS_REALITY__':     _fmt_json_array_for_template(merged_view['reality_users'],   indent=8),
        '__SHORT_IDS_REALITY__': _fmt_json_array_for_template(merged_view['reality_short_ids'], indent=12),
        '__USERS_WS_CF__':       _fmt_json_array_for_template(merged_view['ws_cf_users'],    indent=8),
        # Scalar values from defaults (profiles.yaml) — single source of truth
        # for SNIs, obfs password, etc. Changing any of these in profiles.yaml
        # propagates to both client configs and the server on --server-apply.
        '__REALITY_HANDSHAKE_SNI__':    defaults['reality']['handshake_sni'],
        # Reality X25519 private key — paired with defaults.reality.public_key
        # (shared.reality_public_key in .secrets.yaml). rotate-reality-key.sh
        # generates a new pair and writes both halves atomically.
        '__REALITY_PRIVATE_KEY__':      defaults['reality']['private_key'],
        '__SHADOWTLS_SNI__':            defaults['shadowtls']['sni'],
        '__HY2_SNI__':                  defaults['hy2']['sni'],
        '__HY2_OBFS_PASSWORD__':        defaults['hy2']['obfs_salamander_password'],
        '__SHADOWSOCKS_PASSWORD__':     defaults['shadowtls']['shadowsocks_password'],
        # Server-side clash_api auth secret (auto-gen'd into shared.server_clash_secret).
        '__SERVER_CLASH_SECRET__':      defaults['server_clash_secret'],
    }
    for k, v in subs.items():
        if k not in tpl:
            sys.exit(f'template placeholder {k} missing from {SERVER_TEMPLATE}')
        tpl = tpl.replace(k, v)
    return tpl


def compute_server_plan(manifest):
    """
    Pure planning: compute the new server config, validate with sing-box check,
    compute rotation plan. Returns a dict:
      {
        'available': bool           # template + server_config loadable
        'changed':   bool           # new config differs from live
        'new_text':  str            # rendered config.json
        'report':    list           # rotation actions for display
        'new_pending': dict         # updated pending-rotations state
        'check_ok':  bool
        'check_err': str | None
        'live_text': str            # current config.json as-is
      }
    """
    if not SERVER_TEMPLATE.exists():
        return {'available': False, 'reason': f'template missing: {SERVER_TEMPLATE}'}

    server_cfg = load_server_config()
    if server_cfg is None:
        server_cfg = {'inbounds': []}

    m_view = manifest_server_view(manifest)
    s_view = server_view(server_cfg)
    pending = load_pending_rotations()
    merged, new_pending, report = compute_rotation_plan(m_view, s_view, pending)
    new_text = render_server_text(merged, manifest['defaults'])

    # Validate with sing-box check (mounts hy2 cert paths like safe-restart does).
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as tf:
        tf.write(new_text); tmp_path = tf.name
    try:
        r = subprocess.run(
            ['docker', 'run', '--rm',
             '-v', f'{tmp_path}:/etc/sing-box/config.json:ro',
             '-v', f'{SERVER_DIR}/hy2.crt:/etc/sing-box/hy2.crt:ro',
             '-v', f'{SERVER_DIR}/hy2.key:/etc/sing-box/hy2.key:ro',
             'ghcr.io/sagernet/sing-box:latest', 'check', '-c', '/etc/sing-box/config.json'],
            capture_output=True, text=True,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    check_ok = r.returncode == 0
    check_err = None if check_ok else r.stderr.strip()

    live_text = SERVER_CONFIG.read_text() if SERVER_CONFIG.exists() else ''
    changed = (live_text != new_text)

    return {
        'available': True, 'changed': changed,
        'new_text': new_text, 'live_text': live_text,
        'report': report, 'new_pending': new_pending,
        'check_ok': check_ok, 'check_err': check_err,
    }


def print_server_summary(splan):
    print('── Server plan ──────────────────────────────────────────────')
    if not splan.get('available'):
        print(f'  (unavailable: {splan.get("reason")})')
        return
    if not splan['changed'] and not splan['report']:
        print('  (nothing — server config already matches manifest)')
        return
    if not splan['report']:
        print('  (no rotations; diff is cosmetic)')
    else:
        for kind, action, item, note in splan['report']:
            label = kind.replace('_', ' ')
            desc = item if isinstance(item, str) else item.get('name') or json.dumps(item)
            print(f'  [{action:12}] {label:22} {desc}  — {note}')
    if splan['check_ok']:
        print('  ✓ sing-box check passed')
    else:
        print(f'  ✗ sing-box check FAILED: {splan["check_err"]}')


def print_server_diff(splan):
    if not splan.get('available') or not splan['changed']:
        return
    sys.stdout.writelines(difflib.unified_diff(
        splan['live_text'].splitlines(keepends=True),
        splan['new_text'].splitlines(keepends=True),
        fromfile='a/config.json', tofile='b/config.json', n=3,
    ))


def apply_server_plan(splan):
    """Write config.json, save pending-rotations, run safe-restart, roll back on failure."""
    if not splan.get('available'):
        sys.exit(splan.get('reason', 'server plan unavailable'))
    if not splan['check_ok']:
        sys.exit(f'sing-box check failed — aborting, live server untouched: {splan["check_err"]}')

    if SERVER_CONFIG.exists():
        shutil.copy2(SERVER_CONFIG, SERVER_CONFIG.with_suffix('.json.prev'))
        SERVER_CONFIG.with_suffix('.json.prev').chmod(0o600)
    SERVER_CONFIG.write_text(splan['new_text'])
    # Reality private_key + ShadowTLS / SS / hy2 passwords live in cleartext
    # in this file. Default umask gives 0644 on write — clamp to 0600 so a
    # non-root reader on the host can't lift the secrets.
    SERVER_CONFIG.chmod(0o600)
    save_pending_rotations(splan['new_pending'])
    print(f'  wrote {SERVER_CONFIG}')

    if not SERVER_RESTART.exists():
        print(f'  ! {SERVER_RESTART} not found — restart singbox-server manually')
        return
    print(f'  running {SERVER_RESTART}...')
    r = subprocess.run([str(SERVER_RESTART)], capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
        prev = SERVER_CONFIG.with_suffix('.json.prev')
        if prev.exists():
            shutil.copy2(prev, SERVER_CONFIG)
            print(f'  rolled back {SERVER_CONFIG}')
        sys.exit(f'safe-restart failed (exit {r.returncode}) — rolled back')
    print('  ✓ singbox-server restarted')


def server_sync(manifest, apply_changes=False, auto_yes=False):
    """Standalone server-only flow for --server-dry-run / --server-apply."""
    print('── Validating new server config...')
    splan = compute_server_plan(manifest)
    print_server_summary(splan)
    print()
    print_server_diff(splan)
    if not splan.get('available') or not splan['check_ok']:
        sys.exit(1)
    if not splan['changed']:
        save_pending_rotations(splan['new_pending'])  # persist expiry tidies
        return
    if not apply_changes:
        print('\n(dry-run — no writes)')
        return
    if not auto_yes:
        if input('\nApply + safe-restart singbox-server? [y/N] ').strip().lower() != 'y':
            print('Aborted.'); return
    apply_server_plan(splan)


def render_combined(manifest, dry_run=False, auto_yes=False):
    """
    Default flow: compute client + server plans, show both summaries, single
    apply prompt. If one side has nothing to do it's silently skipped.
    """
    cplan = compute_client_plan(manifest)
    splan = compute_server_plan(manifest)

    print_client_summary(cplan)
    print()
    print_server_summary(splan)

    has_client = client_plan_has_changes(cplan)
    has_server = splan.get('available') and splan['changed']

    if dry_run:
        if has_client:
            print('\n── Client diffs ─────────────────────────────────────────────')
            print_client_diffs(cplan)
        if has_server:
            print('\n── Server diff ──────────────────────────────────────────────')
            print_server_diff(splan)
        return

    if splan.get('available') and not splan['check_ok']:
        sys.exit(1)

    if not has_client and not has_server:
        # Still persist pending-rotation tidies (expired entries dropped).
        if splan.get('available'):
            save_pending_rotations(splan['new_pending'])
        print('\nNothing to do — everything up to date.')
        return

    if not auto_yes:
        if input('\nShow diffs [y/N]? ').strip().lower() == 'y':
            if has_client:
                print('\n── Client diffs ─────────────────────────────────────────────')
                print_client_diffs(cplan)
            if has_server:
                print('\n── Server diff ──────────────────────────────────────────────')
                print_server_diff(splan)
        scope = []
        if has_client: scope.append('client configs')
        if has_server: scope.append('server config + safe-restart')
        if input(f'\nApply {" + ".join(scope)}? [y/N] ').strip().lower() != 'y':
            print('Aborted.'); return

    if has_client:
        apply_plan(cplan, manifest)
    if has_server:
        apply_server_plan(splan)


def main():
    p = argparse.ArgumentParser(description='singbox-profiles generator — edit profiles.yaml by hand, then run this')
    p.add_argument('--validate', action='store_true', help='render to tmp + sing-box check each (client + server); no writes')
    p.add_argument('--dry-run', action='store_true', help='show combined client+server plan; no writes')
    p.add_argument('-y', '--yes', action='store_true', help='apply without prompt')
    p.add_argument('--server-dry-run', action='store_true', help='show server-side diff + rotation plan only (no client); no writes')
    p.add_argument('--server-apply', action='store_true', help='apply server-side only + safe-restart singbox-server')
    args = p.parse_args()
    # load_manifest merges profiles.yaml + .secrets.yaml + home_wg/*.conf,
    # detects device renames, and auto-fills any missing credentials (written
    # back to .secrets.yaml). `auto_yes` propagates so rename prompts are
    # skipped under `-y` (unambiguous 1:1 renames auto-apply; ambiguous ones
    # abort rather than silently rotating).
    manifest = load_manifest(auto_yes=args.yes)
    _warn_missing_recommended_protocols(manifest['users'])
    _warn_unused_utls_fingerprint(manifest['users'])
    _warn_unused_shadowtls_sni(manifest['users'])
    _check_per_user_utls_fingerprint(manifest['users'])
    _check_mobile_shadowtls_sni(manifest['users'], manifest.get('defaults', {}))
    if args.validate:
        validate(manifest)
        # Also validate server config (in-memory), reusing compute_server_plan.
        splan = compute_server_plan(manifest)
        if splan.get('available'):
            status = '✓' if splan['check_ok'] else '✗'
            print(f'  {status} server-config: sing-box check {"passed" if splan["check_ok"] else "FAILED"}')
            if not splan['check_ok']:
                print(f'    {splan["check_err"]}')
                sys.exit(1)
        return
    if args.server_dry_run:
        server_sync(manifest, apply_changes=False)
        return
    if args.server_apply:
        server_sync(manifest, apply_changes=True, auto_yes=args.yes)
        return
    render_combined(manifest, dry_run=args.dry_run, auto_yes=args.yes)


if __name__ == '__main__':
    main()
