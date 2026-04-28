# DNS Architecture Review (eric mobile config, post-hotfixes)

Written 2026-04-24 after the hy2 outage debugging session.

## Current layout (post-fixes)

```
dns.servers:
  bootstrap_dns   → udp://1.1.1.1:53        detour ➡️ Direct
  fakeip-server   → fakeip 198.18.0.0/15
  cloudflare_doh  → https://1.1.1.1/dns-query  detour 🌍 Default

dns.rules:
  vpnws.dot0.one            → bootstrap_dns
  hagezi/iran blocklists    → reject
  pc94179.glddns.com        → cloudflare_doh
  A/AAAA (any)              → fakeip-server
  *                         → cloudflare_doh

route.default_domain_resolver = cloudflare_doh     # was bootstrap_dns, changed 2026-04-24
route.rules[0] = port 53 → hijack-dns
route.rules[N] = tcp:853 → reject                  # added 2026-04-24 (DoT leak closure)
experimental.cache_file.store_fakeip = true
```

## What's working well

- **Clean chicken-and-egg story.** Hy2/Reality/ShadowTLS use raw IPs → no DNS to bootstrap. Only WS-CDN needs `vpnws.dot0.one`, and that's isolated to bootstrap_dns.
- **Fakeip is the right default for A/AAAA.** Instant startup, zero pre-tunnel DNS traffic, real resolution at egress, cache_file persists the reverse map.
- **Privacy-aligned routing.** `pc94179.glddns.com` (home DDNS) + everything else → DoH through tunnel; ISP never sees them.
- **Blocklists fire before anything else** (rule 2 in DNS block), so ad/malware domains never trigger an outbound at all.
- **Hijack-dns route rule is correctly ordered first** — every port-53 query anywhere in the device (including apps that pin their own DNS) gets pulled into sing-box's DNS stack.

## Issues & fixes

### 1. [APPLIED] Android Private DNS leaks DoT traffic (172.16.0.2:853)

**Severity:** cosmetic, latent footgun.

Android's system Private DNS (if Auto or set) emits DoT on TCP:853 to whatever the system resolver thinks is right. Because the TUN captures default routes, those packets re-enter route.rules, hit `ip_is_private → Direct`, and direct outbound tries to dial 172.16.0.2:853 on cellular → 5s timeout on every startup. Cluttered the log and added 5s latency spam during tunnel boot.

**Fix applied:** added `{network: tcp, port: 853, action: reject}` to route.rules (same tier as the UDP/443 reject). Forces any DoT-emitting app (including Android system) back to port 53, which the hijack-dns rule then catches. No capability loss — DoT-speaking apps drop to Do53 and get captured by sing-box's DNS stack.

### 2. [APPLIED] default_domain_resolver was plaintext-by-default

**Severity:** low today, latent footgun.

When a route rule matches on domain (none do today — all rules are `rule_set`/`domain_suffix`/`ip_cidr`), sing-box would resolve that domain via `default_domain_resolver`. Was set to `bootstrap_dns` (plaintext 1.1.1.1). Today's rules don't trigger it, but the first time someone adds `{domain: ["foo.com"], outbound: ...}` to a route rule, the lookup would go plaintext to 1.1.1.1.

**Fix applied:** changed to `cloudflare_doh` (DoH through tunnel). The explicit `vpnws.dot0.one` DNS rule still names bootstrap_dns by tag, so the bootstrap path is unaffected.

### 3. [SKIPPED] dns.strategy pin

Originally suggested `dns.strategy: "prefer_ipv4"` to cut cellular v6-probe latency. **Skipped** — the existing comment in `frag_dns` (render.py:416-421) already documented the right reasoning: because fakeip short-circuits all A/AAAA (the hot path), strategy only affects rare non-A/AAAA flows (TXT/SRV/HTTPS records), and a prior `ipv4_only` attempt caused iOS double-lookups. The author's decision to leave it unset stands.

### 4. [APPLIED — Option B] bootstrap_dns is plaintext to 1.1.1.1 — fragile in CN/IR

**Severity:** high for users in censored countries, zero for resident users.

**Fix applied 2026-04-24:** per-user `bootstrap_country` opt-in field. See full discussion below.

### 5. No fallback mechanism (acknowledged in existing memory)

If `cloudflare_doh` goes down (CF outage, or the `🌍 Default` chain breaks), every non-A/AAAA query hangs — no retry, no alternate server. Previously country DoH existed, removed 2026-04-21 for the same no-fallback reason (country DoH failures were worse because they were per-country).

Nothing to do until sing-box ships a `fallback_server` field upstream. Known risk.

---

# #4 deep-dive: CN/IR bootstrap fragility

## What "bootstrap" actually does in our config

`bootstrap_dns` is queried for exactly **one** thing: resolving `vpnws.dot0.one` before any tunnel outbound is up. That resolution has to happen in cleartext — the very tunnel that would protect the lookup (WS-CDN) needs the lookup to **establish**. Chicken-and-egg.

Everything else (the home DDNS, all other hostnames) goes through `cloudflare_doh` via the tunnel. Hy2/Reality/ShadowTLS servers are hard-coded IPs, so they need zero DNS at all.

So "bootstrap" = "the first 500ms of WS-CDN cold-start". If the user never picks WS-CDN, bootstrap never matters.

## Why it's fragile in CN/IR specifically

`1.1.1.1:53` is:

- **CN:** transparently hijacked by GFW DNS poisoning — queries to 1.1.1.1 either time out, get NXDOMAIN, or get poisoned answers. Always has been.
- **IR:** filtered at the ISP level; queries to 1.1.1.1 also fail under the national firewall's DNS interception.

For resident users (Eric household, CA/FR/IT) this never matters. For a traveller or in-country user hitting WS-CDN, the bootstrap fails → WS-CDN outbound can't connect → they fall through to hy2/Reality/ShadowTLS. As long as **one** of those survives, the tunnel still comes up.

## Why WS-CDN matters at all

WS-CDN exists as the **censorship-survival path of last resort**: it's CloudFlare-fronted HTTPS — indistinguishable from ordinary web traffic. If the adversary is willing to block all Cloudflare to kill it, they're breaking the open web. Practical value:

- Carrier blocks UDP/443 wholesale → Hy2 dies → WS-CDN is your survivor
- Aggressive SNI filtering of Reality's `cloud.oracle.com` cover SNI → Reality dies → WS-CDN survives under CF's SNI pooling
- ShadowTLS passive detection improves → ShadowTLS dies → WS-CDN survives

So WS-CDN **is** the path most likely to still work in a worst-case censorship scenario — which is exactly the scenario where the bootstrap is most likely to fail. That's the problem.

## Options, concrete

### Option A — "accept the risk"

Do nothing. Household users in CA/FR/IT are fine. Maxim (RU) has AmneziaWG as primary and reality/hy2 as backup; WS-CDN is a third-line fallback for him. Carol (CN, if she's in-country) is the real risk, but she might have other paths that already work.

Zero code change. Takes "what's the actual probability a household user depends on WS-CDN while in CN/IR while Reality/Hy2 are both blocked?" — probably low.

### Option B — per-country bootstrap override

Add to `profiles.yaml` per country:

```yaml
countries:
  cn:
    bootstrap_dns: "223.5.5.5"    # Alibaba — fast, not blocked inside CN
  ir:
    bootstrap_dns: "178.22.122.100" # Shecan — reachable inside IR
  default: "1.1.1.1"
```

Renderer picks the user's primary country's override. Small change (~15 lines in `frag_dns`). Tradeoff: each country bootstrap reveals `vpnws.dot0.one` to that country's DNS operator (Alibaba/Shecan). For residents that's irrelevant; for users actively dodging surveillance, you're trading "ISP sees CF IP lookup" for "Alibaba sees CF IP lookup" — probably neutral-to-slightly-better, since CF IPs are pooled so the lookup reveals little.

### Option C — DoT/DoH bootstrap via a fixed encrypted server that's reachable in censored regions

e.g. `udp://9.9.9.9:53` (Quad9 — less targeted than CF) or DoH to a Tor-fronted resolver. Adds encryption but complexity grows, and no censor-resistant resolver is uniformly reachable across CN/IR/RU.

### Option D — Hardcode Cloudflare's IPs for `vpnws.dot0.one`

Skip DNS entirely: put CF edge IPs (`104.16.0.0/12`, `172.64.0.0/13`) directly in the WS-CDN outbound config. No bootstrap query, ever. But CF's edge IPs rotate, and hardcoding ties the config to a specific edge that may become suboptimal or (if CF shuffles IPs) outright unreachable. Renderer would need periodic refresh.

## Recommendation

**Option B if you expect any household member to ever be in CN/IR with Reality/Hy2 blocked**, otherwise **Option A**. B is cheap, reversible, and has no downside for residents (they just stay on 1.1.1.1). A is honest about the fact that for this household, WS-CDN-in-censored-country is a theoretical rather than actual scenario.

**The deciding question:** Who in the household actually uses or might use WS-CDN while physically in a censored region? That determines it — everything else is speculation.

## Option B — implemented 2026-04-24

### Shape

Each censored-country entry in `COUNTRY` (render.py:194-) now carries an optional `bootstrap_dns` IP:

```python
'cn': {..., 'bootstrap_dns': '223.5.5.5'}     # Alibaba
'ru': {..., 'bootstrap_dns': '77.88.8.8'}     # Yandex
'ir': {..., 'bootstrap_dns': '178.22.122.100'} # Shecan
```

Selection is **inferred automatically** from a signal already present in the manifest: **`len(user.countries) == 1 and not has_home` ⇒ the user is physically in that country.** This is exactly the same condition the renderer already uses to pick `cloud_detour` and the `🚨 Restricted` selector's default — a single-country no-home profile is tuned end-to-end for living inside that country (country-TLD traffic defaults to the country-alias Direct, CDN egress stays local, etc.), which isn't meaningful unless the user is actually there.

Multi-country users (Eric: `[cn,ru,ir]` — carries routing for all three regions but is physically in the EU) and home-equipped users (have a WG tunnel back to a resident IP, so they're definitionally extra-territorial) stay on 1.1.1.1.

### First pass: explicit opt-in — retired

Initial implementation required a manual `bootstrap_country: cn` field per user. Retired same-day in favor of inference because the signal was already present: the Restricted selector default and `cloud_detour` both already encode "is this user single-country no-home" — copying that condition into the bootstrap selection reuses an existing well-understood signal instead of adding a parallel field that could drift out of sync with the rest of the profile.

The escape-hatch field was dropped too (2026-04-24). If the inference ever becomes wrong (e.g. a traveller temporarily in CN with a multi-country profile), the right fix is to reshape their `countries` list to match reality, not to add a parallel override.

### Current state (inferred)

| user     | manifest `countries` | has_home | bootstrap server |
| -------- | -------------------- | -------- | ---------------- |
| Maxim    | `[ru]`               | no       | 77.88.8.8 (Yandex)  |
| Carol    | `[cn]`               | no       | 223.5.5.5 (Alibaba) |
| Olivier  | `[cn]`               | no       | 223.5.5.5 (Alibaba) |
| Sean     | `[cn]`               | no       | 223.5.5.5 (Alibaba) |
| Eric     | `[cn,ru,ir]`         | yes      | 1.1.1.1 (default)   |

### Verification

After render, confirm with:

```bash
for p in /opt/docker/apps/singbox-profiles/srv/p/*/singbox-mobile.json; do
  python3 -c "import json; d=json.load(open('$p')); \
    print('$p', [s for s in d['dns']['servers'] \
    if s['tag']=='bootstrap_dns'][0]['server'])"
done
```

### Current state

All five users (eric, carol, olivier, sean, maxim) render with `bootstrap_dns.server = 1.1.1.1` — field is opt-in and none have it set yet. Activate per-user only when someone is known to be physically connecting from inside CN/IR (or if RU interference worsens).

---

# Changes committed this session

- `render.py frag_route`:
  - Added `{network: tcp, port: 853, action: reject}` after the UDP/443 reject (DoT leak closure).
  - Moved `PROXY_SERVER_IPS → Direct` above the UDP/443 reject earlier in the session (hy2 server-IP bypass must precede UDP/443 reject — see `project_singbox_route_order_hy2.md`).
- `render.py frag_route` return block:
  - `default_domain_resolver: "bootstrap_dns"` → `"cloudflare_doh"`.
- `render.py frag_dns`:
  - `bootstrap_dns` changed from `type: local` to `type: udp` / `1.1.1.1:53` / `detour: ➡️ Direct` (earlier this session, eric's DoT loop fix).
- `render.py frag_ntp`:
  - Removed `detour: ➡️ Direct` (sing-box 1.12+ rejects detour to bare direct outbound).
- `render.py frag_outbounds`:
  - `➡️ Direct` outbound now carries `udp_fragment: false` so it's non-empty and other features can legally detour to it.
- Goldens regenerated (`./tests/test_render.py --update`), all 9 configs pass `./render.py --validate`, OneDrive synced for all users.
- `apps/singbox-server/safe-restart.sh`:
  - Added fallback `docker restart singbox-server` when `up -d` was a no-op (bind-mount inode refresh).
- `render.py COUNTRY` + `frag_dns` + `compose`:
  - Per-country `bootstrap_dns` override (CN→223.5.5.5 / RU→77.88.8.8 / IR→178.22.122.100).
  - Selection by inference: single-country user with no home ⇒ physically in that country ⇒ use country override. Multi-country / home-equipped users stay on 1.1.1.1. No manifest field — reshape `countries` / `home` to change.
- `profiles.yaml`:
  - Schema comment documenting the inference rule and the escape-hatch field.
