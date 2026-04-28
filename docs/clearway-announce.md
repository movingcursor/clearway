# Clearway — announcement drafts

Three variants below — pick by venue, not by length. URL is
`https://github.com/movingcursor/clearway` everywhere.

## Tiny — Twitter / Bluesky (≤280 chars)

> Open-sourced Clearway: a multi-protocol sing-box config generator
> + server stack for households in restrictive networks. Reality /
> Hysteria2 / ShadowTLS / VLESS-over-WS, urltest selector, per-user
> PSKs, optional WG home-egress. AGPL-3.0.
>
> https://github.com/movingcursor/clearway

(279 chars. Twitter shortens the URL to t.co/23 chars so effective
count there is ~262.)

## Short — Mastodon (≤500 chars)

> Just open-sourced **Clearway** — a multi-protocol sing-box config
> generator + server stack for households and small teams in restrictive
> networks. Per-user PSKs, per-user uTLS fingerprints, optional WG
> home-egress, urltest across 4 inbounds (Reality / Hysteria2 / ShadowTLS
> / VLESS-over-WS via Cloudflare). YAML manifest in, sing-box configs +
> docker compose out. AGPL-3.0.
>
> https://github.com/movingcursor/clearway

(Roughly 470 chars. Trim "small teams" and "via Cloudflare" if you need
the count tighter.)

## Longer — Reddit, Hacker News, forums (~250 words)

> **Clearway: a sing-box config generator for households and small teams
> in restrictive networks.** Open-sourced today after running it for a
> small household network for a while.
>
> The basic idea: write a YAML manifest describing your users, their
> devices, and which countries they live in or visit (CN/RU/IR
> supported). `./render.py` produces per-device sing-box configs
> (Android, iOS, Windows) plus a hardened docker-compose stack for the
> server side. Default mix is VLESS+Reality on TCP/443, Hysteria2 on
> UDP/443, ShadowTLS+Shadowsocks-2022 on TCP/8443, and VLESS-over-
> WebSocket fronted by Cloudflare — four orthogonal paths through DPI;
> the in-app urltest selector picks whichever is fastest right now.
>
> What's there:
>
> - Per-user PSKs / UUIDs, per-user uTLS fingerprint, optional per-user
>   ShadowTLS SNI override.
> - Optional WireGuard "home egress" so a traveller's home-country
>   traffic exits from a WG peer back home.
> - Server-side rotation grace (2h) so you can rotate creds without
>   coordinating with users.
> - A one-line PowerShell installer for Windows that sets up an
>   NSSM-managed service with hourly auto-update.
> - Stdlib-only golden-file test suite running on every push.
> - A docs/hazards.md collecting the silent-failure modes I've spent
>   real time root-causing — hy2 cert SAN requirement, ShadowTLS mobile
>   pooled-SNI break, smux-over-WS-through-CF EOFs, and so on.
>
> AGPL-3.0. Quickstart + architecture + hardening guide in `docs/`.
>
> https://github.com/movingcursor/clearway

## Notes for posting

- Pick the venue carefully. The natural audiences:
  - **Mastodon (infosec.exchange, fosstodon.org)** — receptive to
    privacy / circumvention tooling. Use the short version.
  - **Hacker News** ("Show HN: Clearway — sing-box config generator
    for…"). Use the longer version. Best window is weekday mornings
    US time.
  - **r/selfhosted** on Reddit — broad "self-hosted services" audience,
    likely a friendly first take. Mention the docker compose + the
    one-command Windows installer specifically; that audience cares.
  - **r/sing_box** if it exists / a sing-box GitHub Discussions thread
    — narrower but the right audience for the protocol-mix design
    choices.
- Don't post to the same audience twice in close succession; pick one
  high-traffic venue (HN or r/selfhosted) for first wave, others later.
- Be ready to answer "why not just use [X]?" in comments. The answer is
  in the README's "What's in the box" + threat-model sections — link
  back rather than retyping.
- Don't promise support beyond "issues welcome". This is small-team
  software; setting expectations early is worth it.

## Things NOT to include in the announcement

- Anything implying it's been audited. It hasn't.
- Specific user numbers. "small household network" is honest; "scaling
  to N users" is not.
- The production hostname (`0.dot0.one`). Public clearway should look
  generic; pointing at a specific live deployment invites probing.
