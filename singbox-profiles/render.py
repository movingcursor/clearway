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
                'ws_cdn':  {'type': 'object', 'required': ['host', 'path', 'port']},
                'shadowtls': {'type': 'object', 'required': ['server_port', 'version', 'sni', 'shadowsocks_method']},
                'hysteria2': {'type': 'object', 'required': ['server_port', 'sni']},
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
                            'items': {'enum': ['reality', 'ws_cdn', 'shadowtls', 'hysteria2']},
                            'minItems': 1,
                        },
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


# Public hostname that serves per-user profile directories under /p/<secret>/.
# Used in README URLs, the Windows installer one-liner, and secrets.txt header.
# Set via the PROFILE_HOST env var (or repo-level .env). To change it: update
# the env var, the Traefik Host() rule in singbox-server/compose.yaml, and the
# DNS A record, then re-run ./render.py to regenerate READMEs + installers.
PROFILE_HOST = os.environ.get('PROFILE_HOST', 'profile.example.com')

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


# Protocols whose outbound speaks TLS through Go's crypto/tls and
# therefore benefits from a per-user uTLS fingerprint (JA3/JA4
# decorrelation across users). Hysteria2 is omitted: it runs over QUIC
# with its own TLS impl and uTLS doesn't apply.
TLS_PROTOCOLS = {'reality', 'ws_cdn', 'shadowtls'}


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
              f"to hysteria2)",
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


def frag_dns(countries, has_home, home_endpoint=None, bootstrap_ip='1.1.1.1', ws_cdn_host=None):
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

    `ws_cdn_host`: the Cloudflare-fronted hostname (defaults.ws_cdn.host)
    that must resolve via bootstrap_dns BEFORE the tunnel is up (the WS-CDN
    outbound needs the CF edge IP to dial). None = WS-CDN not in use; no
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
    bootstrap_domains = [ws_cdn_host] if ws_cdn_host else []

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
    🏠 Home WireGuard endpoint. Detours via 🔀 Proxy so the WG handshake +
    data plane ride inside whichever proxy protocol the user has selected
    (keeps WG UDP out of the clear on hostile networks AND honours the
    user's explicit selector choice — if the user pins 🔀 Proxy to
    🔐 Reality, home WG rides over Reality too). Prior to 2026-04-23 the
    detour was hardcoded to ⚡ Fastest, which meant pinning 🔀 Proxy had
    no effect on home-country traffic. Since 🔀 Proxy's own default is
    ⚡ Fastest, the out-of-the-box behaviour is identical; only manual
    pins now propagate to home WG.

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
        'detour': '🔀 Proxy',
        # MTU 1280 (was 1380). The 🏠 Home WG endpoint rides inside whichever
        # proxy protocol 🔀 Proxy resolves to — ⚡ Fastest by default,
        # Reality/hy2/ShadowTLS/WS-CDN if the user has pinned — so WG
        # packets are double-encapsulated: TUN(1380) → outer proxy (TLS/QUIC
        # overhead) → WG(outer). 1380 left no headroom for the outer layer and
        # would silently fragment or PMTU-blackhole large TCP payloads over
        # Home Egress. 1280 is the IPv6 minimum and safe for any nested path.
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
        'type': 'vless', 'tag': '🔐 Reality',
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


def frag_outbound_ws_cdn(defaults, ws_cdn_uuid, fp=None):
    d = defaults['ws_cdn']
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
        'type': 'vless', 'tag': '☁️ WS-CDN',
        'server': d['host'], 'server_port': d['port'],
        'uuid': ws_cdn_uuid,
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
            'type': 'shadowsocks', 'tag': '👻 ShadowTLS',
            'server': defaults['reality']['server'], 'server_port': d['server_port'],
            'method': d['shadowsocks_method'], 'password': ss_pw,
            'detour': 'shadowtls-transport',
        },
    ]


def frag_outbound_hysteria2(defaults, pw):
    d = defaults['hysteria2']
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
    return {
        'type': 'hysteria2', 'tag': '🚀 Hysteria2',
        'server': defaults['reality']['server'], 'server_port': d['server_port'],
        'password': pw,
        'up_mbps': up_mbps,
        'down_mbps': down_mbps,
        'obfs': {'type': 'salamander', 'password': d['obfs_salamander_password']},
        'tls': {'enabled': True, 'server_name': d['sni'], 'alpn': ['h3'],
                'certificate': cert_lines},
    }


def protocol_outbound_tags(protocols):
    """Ordered list of the user-facing proxy outbound tags (input for ⚡ Fastest / 🔀 Proxy)."""
    tags = []
    if 'reality'   in protocols: tags.append('🔐 Reality')
    if 'ws_cdn'    in protocols: tags.append('☁️ WS-CDN')
    if 'shadowtls' in protocols: tags.append('👻 ShadowTLS')
    if 'hysteria2' in protocols: tags.append('🚀 Hysteria2')
    return tags


def selector_urltest_url(device):
    """Mobile uses gstatic.com/generate_204 (Android/iOS native reachability),
    Windows same — keep uniform."""
    return 'https://www.gstatic.com/generate_204'


def frag_selectors(user, device, defaults):
    """
    Build all selector + urltest outbounds. Controls which tags appear where.
    """
    countries = user['countries']
    protocols = user['protocols']
    has_home = 'home' in user
    proxy_tags = protocol_outbound_tags(protocols)

    # ⚡ Fastest probes every enabled proxy protocol.
    # Mobile configs historically use longer intervals to save battery.
    interval = '10m' if device['type'] == 'mobile' else '5m'
    tolerance = 150 if device['type'] == 'mobile' else 100
    idle_timeout = '1h' if device['type'] == 'mobile' else '30m'

    fastest = {
        'tag': '⚡ Fastest', 'type': 'urltest',
        'outbounds': proxy_tags,
        'url': selector_urltest_url(device),
        'interval': interval, 'tolerance': tolerance,
        'idle_timeout': idle_timeout,
        'interrupt_exist_connections': True,
    }

    # 🔀 Proxy: protocol chooser.
    proxy_selector = {
        'tag': '🔀 Proxy', 'type': 'selector',
        'outbounds': ['⚡ Fastest'] + proxy_tags,
        'default': '⚡ Fastest',
    }

    selectors = [fastest, proxy_selector]

    # 🏠 Home Egress — only if user has home block.
    if has_home:
        selectors.append({
            'tag': '🏠 Home Egress', 'type': 'selector',
            'outbounds': ['🏠 Home', '🔀 Proxy', '➡️ Direct'],
            'default': '🏠 Home',
        })

    # 🌍 Default — catch-all proxy selector.
    default_outbounds = ['🔀 Proxy']
    if has_home:
        default_outbounds.append('🏠 Home')
    default_outbounds.append('➡️ Direct')
    selectors.append({
        'tag': '🌍 Default', 'type': 'selector',
        'outbounds': default_outbounds,
        'default': '🔀 Proxy',
    })

    # 🔒 Trusted — always-proxy for sensitive accounts.
    selectors.append({
        'tag': '🔒 Trusted', 'type': 'selector',
        'outbounds': ['🔀 Proxy', '➡️ Direct'],
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

    # Protocol outbounds in canonical order.
    if 'reality'   in user['protocols']: out.append(frag_outbound_reality(defaults, device['reality'], fp=fp))
    if 'ws_cdn'    in user['protocols']: out.append(frag_outbound_ws_cdn(defaults, user['ws_cdn_uuid'], fp=fp))
    if 'shadowtls' in user['protocols']: out.extend(frag_outbound_shadowtls(defaults, user['shadowtls_password'], user['shadowsocks_password'], fp=fp, sni=stls_sni))
    if 'hysteria2' in user['protocols']: out.append(frag_outbound_hysteria2(defaults, user['hysteria2_password']))

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
    # resident). Previously hard-pinned to ⚡ Fastest which a) required the
    # urltest to have converged on a live proxy before first-boot could seed
    # the blocklists, and b) prevented a resident who flipped Default→Direct
    # from using their (fast, uncensored) ISP for the ruleset CDN fetch.
    # jsdelivr.net is CloudFlare-fronted and usually direct-reachable
    # outside CN/IR; tunnelling is still available for users who need it.
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
        # Vision, ShadowTLS, and VLESS+WS are all TCP-only transports; when
        # ⚡ Fastest picks any of them, outbound UDP/443 silently black-holes
        # (HTTP/3 hangs, browsers do the slow fallback to TCP after ~a
        # second). Rejecting here forces the immediate fallback.
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
    # swept into country Restricted). Two sources:
    #   - home_egress_countries: ISO-2 codes → both a geoip rule and a
    #     domain_suffix entry (matching the ccTLD).
    #   - home_egress_tlds: arbitrary TLDs (eu, one, brand TLDs) → only
    #     domain_suffix, no geoip.
    if has_home:
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


def render_user_readme(uname, user, defaults):
    """
    Generate a ready-to-send Markdown README for this user. Written from
    user's perspective (not admin's), covering setup for each device, the
    dashboard secret if Windows, and a credentials summary at the end.

    Reads from the merged manifest (load_manifest already wired secrets +
    home_wg into user / device fields), so everything needed is already here.
    """
    uname_cap = uname.capitalize()
    secret = user['secret']
    protocols = user.get('protocols', [])
    countries = user.get('countries', [])
    has_home = 'home' in user

    # Device tables
    devices = user.get('devices', [])
    has_mobile = any(d['type'] == 'mobile' for d in devices)
    has_windows = any(d['type'] == 'windows' for d in devices)

    # Country + home summary lines
    country_labels = ', '.join(f"{COUNTRY[c]['flag']} {COUNTRY[c]['label']}" for c in countries)
    protocol_labels = ', '.join(protocols) if protocols else 'none'

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

    # Mobile section
    mobile_section = f"""## Mobile setup

**Recommended — remote profile URL (auto-updates):**

```
https://{PROFILE_HOST}/p/{secret}/singbox-mobile.json
```

In the sing-box app: **Profiles** → **+** → **Type: Remote** → paste the URL → **Auto Update: 60 min** → **Save**.
The app fetches the config, validates it, reloads live. On server-side changes the phone picks up the new config on its next poll.

The URL itself is the credential (128-bit random path). Treat like any other sensitive string.

**Fallback methods** (if the URL is unreachable from your local network):
- **Local file import** — get `singbox-mobile.json` from admin via AirDrop / iCloud / Google Drive / email, then in the app: Profiles → + → Import from file.
- **URL import (one-off)** — host the file on any reachable URL, then Profiles → + → Import from URL.
""" if has_mobile else ''

    # Windows section
    if has_windows:
        win_dev = next(d for d in devices if d['type'] == 'windows')
        win_filename = 'singbox-windows.json'
        # If multiple windows devices, fall back to per-device filename
        win_devices = [d for d in devices if d['type'] == 'windows']
        if len(win_devices) > 1:
            win_filename = f"singbox-windows-{win_dev['name']}.json"

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
    if 'ws_cdn' in protocols:
        creds_lines.append(f"- **WS-CDN** UUID: `{user.get('ws_cdn_uuid')}` (shared across your devices)")
    if 'hysteria2' in protocols:
        creds_lines.append(f"- **Hysteria2** password: `{user.get('hysteria2_password')}`")
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

- **🔀 Proxy** — which protocol to use. Leave on **⚡ Fastest** (auto-picks by latency) unless debugging.
- **🚨 Restricted** — how to treat traffic to the countries you cover. {restricted_default_explain}
- **🔒 Trusted** — sensitive domains (banking, 1Password, Apple, Microsoft). Always tunneled by default.

Changes apply instantly; no restart needed.

{mobile_section}
{windows_section}

## Credentials (keep private)

{creds_block}

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Everything times out | Primary protocol blocked / probed | In the dashboard, change 🔀 Proxy from ⚡ Fastest to a specific protocol (e.g. ☁️ WS-CDN). |
| Local-country sites slow | 🚨 Restricted on 🌍 Default | Flip to your country flag for Direct routing. |
| Tunnel connects but pages don't load | DNS cache after a mode change | Toggle VPN off / on once. |
| PC: dashboard shows 404 | metacubexd not yet downloaded (first boot) | Wait 30 s, reload. |
| Rule-sets fail on first start | Proxy not healthy at boot | Wait 30 s; they download on ⚡ Fastest's first successful probe. |

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
            'external_ui_download_detour': '⚡ Fastest',
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
    ws_cdn_host = defaults.get('ws_cdn', {}).get('host') if 'ws_cdn' in user.get('protocols', []) else None
    cfg.update(frag_dns(user['countries'], has_home, home_endpoint=home_endpoint,
                        bootstrap_ip=bootstrap_ip, ws_cdn_host=ws_cdn_host))
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
    Compute the per-device filename.
    - Single device of that type in user's list → singbox-<type>.json
    - Multiple devices of the same type → singbox-<type>-<name>.json
    """
    same_type = [d for d in user['devices'] if d['type'] == device['type']]
    if len(same_type) == 1:
        return f"singbox-{device['type']}.json"
    return f"singbox-{device['type']}-{device['name']}.json"


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
    per-device output file + per-user README. Does not print or write."""
    defaults = manifest['defaults']
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
    return plan


def client_plan_has_changes(plan):
    return any(a[2] in ('modify', 'create', 'delete') for a in plan)


def print_client_summary(plan):
    print('── Client plan ──────────────────────────────────────────────')
    if not plan:
        print('  (nothing)')
        return
    for path, _, action, uname in plan:
        print(f'  [{action:9}] {uname:8} {path.relative_to(ROOT)}')


def print_client_diffs(plan):
    for path, text, action, uname in plan:
        if action == 'modify':
            print(unified_diff(path.read_text(), text, f'a/{path.name}', f'b/{path.name}'))
        elif action == 'create':
            print(f'── new file: {path.relative_to(ROOT)} ({len(text.splitlines())} lines)')
        elif action == 'delete':
            print(f'── will delete: {path.relative_to(ROOT)}')


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
            print(f'  wrote {path.relative_to(ROOT)}')
        elif action == 'delete':
            path.unlink()
            print(f'  deleted {path.relative_to(ROOT)}')

    # Refresh secrets.txt FIRST — generate-installer.sh reads this file to
    # look up each user's URL secret, so a new user's installer would fail
    # if secrets.txt wasn't yet updated.
    lines = [
        '# user -> path-secret mapping for singbox-profiles remote-profile URLs',
        f'# Full URL: https://{PROFILE_HOST}/p/<secret>/singbox-mobile.json',
        f'# Maintained by render.py — edit profiles.yaml instead.',
        '',
    ]
    for uname, user in manifest['users'].items():
        lines.append(f"{uname}\t{user['secret']}")
    SECRETS_FILE.write_text('\n'.join(lines) + '\n')
    print(f'  wrote {SECRETS_FILE.relative_to(ROOT)}')

    # Refresh installers for every windows device. generate-installer.sh
    # takes a config filename; for now we drive it for the primary windows
    # device per user (singbox-windows.json). Multiple-windows-per-user would
    # need installer-per-device URLs — not yet supported.
    for uname, user in manifest['users'].items():
        win_devs = [d for d in user['devices'] if d['type'] == 'windows']
        if not win_devs:
            continue
        # Pick the default singbox-windows.json (single-windows case). Multi
        # windows: fall back to first device's filename.
        fname = 'singbox-windows.json'
        if not (user_output_dir(user) / fname).exists():
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
        if 'ws_cdn' in protocols:
            need('ws_cdn_uuid', lambda: str(uuid.uuid4()))
        if 'hysteria2' in protocols:
            need('hysteria2_password', lambda: secrets.token_urlsafe(22))
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
    if 'hysteria2_obfs_salamander_password' in shared:
        manifest['defaults']['hysteria2']['obfs_salamander_password'] = shared['hysteria2_obfs_salamander_password']
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

    # Merge .secrets.yaml into manifest.users
    for uname, user in manifest['users'].items():
        s_user = sfile['users'].get(uname, {})
        # shadowsocks_password restored to per-user list 2026-04-22 after
        # switching to SS-2022 multi-user EIH. shared.shadowsocks_password is
        # still in use — but as the inbound-level server PSK, not the session key.
        for field in ('secret', 'ws_cdn_uuid', 'hysteria2_password',
                      'shadowtls_password', 'shadowsocks_password', 'notify_webhook'):
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
    shadowtls, shadowsocks, hysteria2, reality_users, short_ids, vless_ws = [], [], [], [], [], []

    for uname, user in manifest['users'].items():
        protocols = user.get('protocols', [])
        if 'shadowtls' in protocols and user.get('shadowtls_password'):
            shadowtls.append({'name': uname, 'password': user['shadowtls_password']})
            # Per-user SS-2022 EIH entry on the shared shadowsocks inbound.
            # Parallels shadowtls_users — same set of users, different secret.
            if user.get('shadowsocks_password'):
                shadowsocks.append({'name': uname, 'password': user['shadowsocks_password']})
        if 'hysteria2' in protocols and user.get('hysteria2_password'):
            hysteria2.append({'name': uname, 'password': user['hysteria2_password']})
        if 'ws_cdn' in protocols and user.get('ws_cdn_uuid'):
            vless_ws.append({'name': uname, 'uuid': user['ws_cdn_uuid']})
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
        'hysteria2_users': hysteria2,
        'reality_users': reality_users,
        'reality_short_ids': short_ids,
        'ws_cdn_users': vless_ws,
    }


def server_view(server_config):
    """
    Project the current live server config.json into the same shape as
    manifest_server_view — so we can diff them.
    """
    view = {
        'shadowtls_users': [],
        'shadowsocks_users': [],
        'hysteria2_users': [],
        'reality_users': [],
        'reality_short_ids': [],
        'ws_cdn_users': [],
    }
    if not server_config:
        return view
    for ib in server_config.get('inbounds', []):
        tag = ib.get('tag')
        if tag == 'shadowtls-in':
            view['shadowtls_users'] = ib.get('users', [])
        elif tag == 'shadowsocks-in':
            view['shadowsocks_users'] = ib.get('users', [])
        elif tag == 'hysteria2-in':
            view['hysteria2_users'] = ib.get('users', [])
        elif tag == 'reality-in':
            view['reality_users'] = ib.get('users', [])
            view['reality_short_ids'] = ib.get('tls', {}).get('reality', {}).get('short_id', [])
        elif tag == 'vless-ws-in':
            view['ws_cdn_users'] = ib.get('users', [])
    return view


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def load_pending_rotations():
    """Load the rotation state file. Returns a dict of lists per credential kind."""
    blank = {
        'shadowtls_users': [], 'shadowsocks_users': [], 'hysteria2_users': [],
        'reality_users': [], 'reality_short_ids': [], 'ws_cdn_users': [],
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
    if kind in ('shadowtls_users', 'shadowsocks_users', 'hysteria2_users'):
        return ('password', item.get('password'))
    if kind in ('reality_users', 'ws_cdn_users'):
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
        '__USERS_HYSTERIA2__':   _fmt_json_array_for_template(merged_view['hysteria2_users'], indent=8),
        '__USERS_REALITY__':    _fmt_json_array_for_template(merged_view['reality_users'],   indent=8),
        '__SHORT_IDS_REALITY__': _fmt_json_array_for_template(merged_view['reality_short_ids'], indent=12),
        '__USERS_VLESS_WS__':   _fmt_json_array_for_template(merged_view['ws_cdn_users'],    indent=8),
        # Scalar values from defaults (profiles.yaml) — single source of truth
        # for SNIs, obfs password, etc. Changing any of these in profiles.yaml
        # propagates to both client configs and the server on --server-apply.
        '__REALITY_HANDSHAKE_SNI__':    defaults['reality']['handshake_sni'],
        # Reality X25519 private key — paired with defaults.reality.public_key
        # (shared.reality_public_key in .secrets.yaml). rotate-reality-key.sh
        # generates a new pair and writes both halves atomically.
        '__REALITY_PRIVATE_KEY__':      defaults['reality']['private_key'],
        '__SHADOWTLS_SNI__':            defaults['shadowtls']['sni'],
        '__HYSTERIA2_SNI__':            defaults['hysteria2']['sni'],
        '__HYSTERIA2_OBFS_PASSWORD__':  defaults['hysteria2']['obfs_salamander_password'],
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
    _check_per_user_utls_fingerprint(manifest['users'])
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
