# Hazards

A field guide to the silent-failure modes we've hit running this stack in
production. Most of these have a fix already baked into the renderer or the
keep-scripts; they're documented here so a future you (or a contributor)
doesn't spend a weekend re-deriving them.

If you hit something not listed, file an issue with the symptom + the smallest
reproducible config. The point of this doc is to make hazards cumulative.

---

## 1. hy2 self-signed cert must have `subjectAltName`, not just CN

**Symptom.** Hysteria2 outbound shows "no speed" / never picks up latency in
the client's urltest panel. Reality / ShadowTLS / WS-CDN on the same client
work normally. Server logs nothing (the *client* aborts the handshake after
receiving the cert). Mobile clients (iOS SFI, Android SFA) hit this most
often, but the failure is universal — desktop sing-box on Go ≥ 1.15 fails
the same way.

**Why.** Go 1.15 removed the CN hostname fallback (`GODEBUG=x509ignoreCN`
disappeared in 1.17). sing-box runs on a recent Go. The client does two
separate TLS checks: cert pinning via `tls.certificate` (passes — bytes
match) and hostname validation against `tls.server_name`. Hostname validation
reads `subjectAltName` *exclusively*; a cert with only `CN` returns "no DNS
names listed" and the handshake aborts. Pinning bypasses CA trust, **not**
hostname validation.

**Fix.** Always generate hy2.crt with both CN and a matching SAN. The
`singbox-server/rotate-hy2-cert.sh` script does this correctly — use it
rather than hand-rolled `openssl`. Manual equivalent (replace the SNI):

```sh
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout hy2.key -out hy2.crt -days 730 -nodes \
  -subj "/CN=cloud.example.com" \
  -addext "subjectAltName=DNS:cloud.example.com"
```

After regen, run `./render.py -y` from `singbox-profiles/` to re-inline the
new PEM into every client's `tls.certificate` pin, then `docker restart
singbox-server` (see hazard #5 below — `up -d` alone is not sufficient).

---

## 2. ShadowTLS on mobile sing-box breaks when SNI ≠ the server's handshake target

**Symptom.** Mobile sing-box (Android SFA, iOS SFI) shows a brief latency on
the `ShadowTLS` outbound on the first probe, then it goes dark — subsequent
probes time out, app stays at 0 b/s. Other outbounds on the same client are
fine. Desktop sing-box (Windows, Linux) on the *same* config doesn't hit it.
Server intermittently logs `client hello verify failed: unexpected session
id length` and `hmac mismatch` on the ShadowTLS port.

**Why (theory).** TLS 1.3 session resumption on the mobile sing-box TLS
stack reuses a session ticket whose original handshake was against the
virtual-hosted cover cert (e.g. `docs.cover.com` served via the configured
handshake target on :443). On the resumed ClientHello the `session_id`
field shape changes, breaking ShadowTLS v3's HMAC-in-`session_id` contract
(the server expects exactly 32 bytes with HMAC in the last 8). Root cause
not fully proven (capture the resumed ClientHello with tcpdump if you want
to confirm), but the workaround is trivial.

**Fix.** For every mobile user that uses ShadowTLS, set
`shadowtls_sni: <handshake-host>` under their `users.<name>:` block in
`profiles.yaml` so they use the canonical cover SNI rather than a
pool-rotated one. Desktop users can stay on `defaults.shadowtls.sni_pool`
(per-user JA3/JA4 decorrelation still works for them). Don't disable
`wildcard_sni: "authed"` server-side — the issue is client-side
session-cache × SNI-mismatch, not server policy. The renderer already
honours `users.<name>.shadowtls_sni` as an override; profiles.example.yaml's
`dave` archetype demonstrates the pattern.

---

## 3. smux on VLESS-over-WebSocket through Cloudflare is broken

**Symptom.** WS-CDN outbound completes the TLS handshake to Cloudflare,
upgrades to WebSocket cleanly, then dies on the first VLESS request. Server
logs `inbound/vless[vless-ws-in]: process multiplex stream: read multiplex
stream request: EOF` — the client closed the connection before a complete
multiplex stream request landed. Other outbounds (Reality, hy2, ShadowTLS)
work fine on the same client.

**Why.** Cloudflare's WebSocket proxy has tightened binary-frame buffering
and streaming, which interacts poorly with smux's framing. The same config
on direct WS-to-origin (bypassing Cloudflare) sometimes also reproduces, so
it may also be a sing-box-side smux regression that only surfaces under
CF-added latency. We didn't dig further because removal is the cheap fix.

**Fix.** Don't enable `multiplex` on a VLESS-over-WS outbound that goes
through Cloudflare. The renderer disables it in `frag_outbound_ws_cf` by
default with a block comment explaining why. The per-request handshake
overhead without multiplex is negligible on modern CF edges (a few ms per
new VLESS stream for the WS round-trip). Server-side `multiplex.enabled:
true` stays on harmlessly — it's an accept-all gate, not a mandate.

If you want to re-enable on the client side, render a single test user,
have them try, and tail `vless-ws-in` for `read multiplex stream request:
EOF` before rolling it out.

---

## 4. sing-box 1.12+ rejects `detour` to a bare direct outbound

**Symptom.** Client crashes at init with:

```
ERROR ntp: initialize time: detour to an empty direct outbound makes no sense
```

DNS never comes up, no outbound ever connects. Hits NTP first because that's
typically the first feature with a `detour:` field; the same trap applies to
any DNS server / service with `detour:`.

**Why.** sing-box 1.12+ validates that `detour` references do meaningful
routing. A detour pointing to a `{type: "direct"}` outbound with no other
fields is treated as a no-op and rejected.

**Fix.** Either omit `detour` entirely (the feature runs on the default
direct path, identical behaviour) or point it at an outbound that does
something. The renderer's "➡️ Direct" outbound carries `udp_fragment: false`
specifically so it's non-empty and can legally serve as a detour for
`bootstrap_dns` (which needs to dial UDP/53 outside the tunnel).

---

## 5. Single-file bind mounts pin the inode — `up -d` alone won't pick up edits

**Symptom.** You edit a bind-mounted single file (`config.json`, `hy2.crt`,
or similar), restart with `docker compose up -d`, and the container keeps
serving the old content. SIGHUP / config-reload doesn't help either. Symptom
on hy2: clients still pinned to the *new* cert can't handshake the server
that's still presenting the *old* cert; the failure mode is silent (TLS
abort at QUIC layer, no log line, zero packets visible).

**Why.** Docker resolves a single-file bind mount to an *inode* at container
start, not a path. Atomic-rename writers (`Edit`, `sed -i`, `mv`) replace
the path with a *new* inode; the container keeps the old inode mapped.
`docker compose up -d` is a no-op when compose.yaml/env haven't changed,
so it doesn't re-resolve the mount. SIGHUP only helps if the file was
modified in-place (same inode), which most edit tools don't do.

**Fix.**
- After editing a bind-mounted single file, run `docker restart
  <container>` to re-resolve the mount inode.
- Prefer bind-mounting a *directory* over a single file when the contents
  may change at runtime — directory mounts resolve paths inside on each
  access, so rename-replacements work transparently.
- The supplied `singbox-server/safe-restart.sh` already covers this: if
  `up -d` is a no-op (container ID unchanged), it follows up with `docker
  restart`.

To test whether the container sees your edit: `docker exec <ctr> cat
/path/inside` and compare to the host file.

---

## 6. TUN `auto_route + strict_route` captures hy2's own QUIC egress

**Symptom.** On a mobile client running TUN with `auto_route: true` and
`strict_route: true` (the renderer's default), Hysteria2 fails to establish.
Outer QUIC packets to the proxy server's UDP/443 never make it out — they
get pulled back into `route.rules`. If a `{network: udp, port: 443, action:
reject}` rule is present (to kill browser HTTP/3 and force TCP fallback),
hy2 hits it and dies.

**Why.** `strict_route` unconditionally routes every egress packet through
the TUN's `route.rules`, including packets sourced by sing-box's own
outbounds. First-match-wins, so a blanket UDP/443 reject can swallow hy2's
own outer transport before it reaches the link.

**Fix.** Order matters in `route.rules`. The proxy-server bypass — `{ip_cidr:
[<server-IPs>], outbound: "➡️ Direct"}` — must come **before** any UDP/443
reject. The renderer's `frag_route` does this when `defaults.proxy_server_ips`
is set; for single-host deployments where the proxy hostname only resolves
to one IP that's already excluded by another route rule, the list can be
empty. Same trap exists for any other port/network reject that could overlap
a proxy-server outbound's outer transport (TCP/853 DoT reject, etc. —
sequence them after the bypass).

---

## 7. sing-box has no per-user hy2 bandwidth caps

**Symptom.** You want "one stolen credential can't saturate the uplink" as
a defensive measure. sing-box's hy2 inbound `users[]` schema is exactly
`{name, password}` — `up_mbps` / `down_mbps` per user is rejected by
`sing-box check`. The inbound-level `up_mbps` / `down_mbps` exist but are
*advertisements for Hysteria2's brutal congestion control* (client trusts
the value and paces accordingly), not enforced rate limits.

**Fix.** Don't try to put per-user caps in the sing-box layer — it's an
upstream feature gap, not a config issue. If per-user rate limiting matters,
implement at the Linux layer: `tc` qdiscs keyed on source IP, or iptables
`--hashlimit` per source. Mapping source-IP → user is non-trivial since
QUIC uses one 5-tuple per session, so this is more involved than it sounds.

A cheaper mitigation: ban source IPs that show ≥10 auth failures in a short
window (the household ops version of this script lives outside the public
repo, but a 30-line shell script reading `docker logs singbox-server | grep
'authentication failed'` is enough to start with).

---

## 8. Android Private DNS leaks DoT through the TUN

**Symptom.** On Android with "Private DNS" enabled (system setting →
"Automatic" or a custom hostname), sing-box logs flood with `connection
to 172.16.0.2:853 timed out` at startup. Every DNS lookup the OS attempts
goes to the system's DoT target, gets pulled back into the TUN by
`auto_route + strict_route`, hits the TUN peer at port 853, and times out
after 5s before any proxy comes up.

**Why.** Android's resolver emits DoT (TCP/853) when Private DNS is on.
Under strict_route the TUN captures every egress packet — DoT included —
and the loop has no exit.

**Fix.** Add `{network: tcp, port: 853, action: reject}` to `route.rules`,
*after* the proxy-server bypass and *before* the catch-all. The renderer
does this in `frag_route`. Combined with the existing `port: 53,
action: hijack-dns` (which catches the resulting Do53 fallback queries),
DoT-speaking apps drop transparently to sing-box's own DNS stack with no
capability loss. Side benefit: the same rule shuts the door on apps that
sidestep the port-53 hijack by speaking DoT directly.

---

## 9. sing-box ANSI colors in logs are not suppressible at the source

**Symptom.** Logs from sing-box always carry ANSI color escapes
(`\x1b[31m...\x1b[0m`, etc.) regardless of TTY status. This clutters
`docker logs`, grep, and any log-aggregator ingest. Setting `tty: false`
in compose.yaml doesn't help; neither does `NO_COLOR=1` in the
environment.

**Why.** sing-box's log formatter doesn't honor `NO_COLOR` and there's no
config field to disable colors. `log.disable_color: true` returns
`json: unknown field "disable_color"` from `sing-box check`.

**Fix.** Strip downstream, not at the source.
- Promtail / Vector / Loki ingest pipeline: `replace` stage with regex
  `\x1b\[[0-9;]*m` → empty.
- Ad-hoc CLI: `docker logs singbox-server | sed 's/\x1b\[[0-9;]*m//g'`.

Don't try to wrap the container command with `sh -c '... | sed'` — that
breaks pid-1 signal semantics (sing-box no longer receives SIGTERM
cleanly), so `docker stop` waits the full grace period before SIGKILL.

## 10. GFW marks IPs sending sustained UDP/443 volume — drops *all* incoming UDP for ~1h

**Symptom.** Hysteria2 from CN works fine for a while, then a single
client (or many clients sharing the same overseas server IP) suddenly
loses *all* UDP connectivity to that server for tens of minutes to an
hour. TCP to the same server (Reality on TCP/443, ShadowTLS on TCP/8443)
keeps working. urltest demotes hy2; user notices nothing if they have
TCP fallbacks enabled.

**Why.** GFW maintains a per-IP heuristic that flags overseas hosts
generating sustained high-volume UDP/443 flows from inside CN. Once
flagged, all UDP from that host is dropped at the border for the
rest of an hourly window (apernet/hysteria#1157, Telegram-sourced
community report — not peer-reviewed but consistent enough that
Hysteria upstream ships port-hopping for exactly this case). Salamander
obfuscation does *not* help here; the heuristic is volume-based and
shape-agnostic. The SNI-classifier vector documented in USENIX Sec '25
(Zohaib et al.) is a separate mechanism that salamander *does* defeat.

**Fix.** Hysteria2 port-hopping. Server keeps listening on a single
port (e.g. UDP/443); a host iptables NAT redirect collapses a wide
port range (e.g. UDP/20000-30000) onto that single listen port. Clients
get `server_ports: ["20000:30000"]` instead of `server_port: 443` and
dial random ports from the range — packets spread across thousands of
5-tuples so no single flow accumulates enough volume to trip the
heuristic.

Three coordinated parts:

1. **Manifest.** Add `server_ports: ["20000:30000"]` to
   `defaults.hy2` in `profiles.yaml`. The renderer emits
   `server_ports` *instead of* `server_port` when this is set
   (sing-box's hy2 outbound dials `server_port` first if both are
   present, defeating the spread).

2. **Host iptables.** Run
   `singbox-server/setup-hy2-port-hop.sh` on the docker host. It adds:
   ```
   iptables -t nat -A PREROUTING -d <hy2_listen_ip> -p udp \
       --dport 20000:30000 -j REDIRECT --to-ports 443
   ```
   and persists via `netfilter-persistent save`. Idempotent — safe to
   re-run.

3. **Cloud firewall.** Open UDP `20000-30000` ingress on the VNIC
   that hy2 binds to (the one matching `defaults.reality.server` in
   `profiles.yaml`). On Oracle Cloud, this is one ingress rule on the
   VCN security list. Once port-hopping is rolled out and clients have
   updated, *remove* the UDP/443 rule — no client should hit it
   anymore, and removing it shrinks the attack surface.

The TCP/443 rule (Reality) stays — different protocol, different rule.

---

## Adding to this doc

If you hit a silent-failure mode that took >2h to debug, write it up here.
Format per entry:

- **What you saw** (the symptom — exact log line if you have one)
- **Why it happens** (the underlying mechanism, not the proximate cause)
- **The fix** (what to change in the renderer / scripts / config)

Keep it brief. The point is "future-you can grep this in 10s when the same
thing comes back," not a thorough write-up.
