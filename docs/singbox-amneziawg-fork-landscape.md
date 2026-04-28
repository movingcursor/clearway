# sing-box + AmneziaWG: fork landscape

Notes from a scoping exercise on 2026-04-24, kept so the next person (or
the next session) doesn't re-propose wiring AmneziaWG into `render.py`.

## TL;DR

**Don't wire AmneziaWG into the sing-box profiles.** Official sing-box has
no AWG support, and no AWG-capable sing-box client is distributable via
the iOS App Store. Keep AWG on its own track via the AmneziaVPN app
consuming `onedrive:Desktop/Configs/<user>/awg-*.conf`. The server peer
list is rendered by `clearway/singbox-profiles/render.py` into
`clearway/awg-server/config/awg0.conf` (per-device peers, post-2026-04-27
migration; the legacy hand-managed `apps/amneziawg/wg0.conf` was retired).

## The three repos to know about

| Repo | Role | Latest (2026-04) | Binaries shipped |
|---|---|---|---|
| [`SagerNet/sing-box`](https://github.com/SagerNet/sing-box) | upstream | tracks `stable-next` | CLI + SFA (Play/F-Droid) + SFI (App Store) |
| [`amnezia-vpn/amnezia-box`](https://github.com/amnezia-vpn/amnezia-box) | official Amnezia fork — powers the AmneziaVPN desktop/mobile apps | lags sing-box to align with AmneziaVPN app cuts | Go CLI; consumed internally by AmneziaVPN clients |
| [`hoaxisr/amnezia-box`](https://github.com/hoaxisr/amnezia-box) | community rebase of the above, aggressive sync with upstream `stable-next` | `v1.14.0-alpha.7-awg2.0` (2026-03-30) | **Go CLI only** — no SFA APK, no SFI, no release-built Android app |

Upstream SagerNet has open feature requests going back years
([#1928](https://github.com/SagerNet/sing-box/issues/1928),
[#2276](https://github.com/SagerNet/sing-box/issues/2276),
[#3159](https://github.com/SagerNet/sing-box/issues/3159)) — all unmerged.
The maintainer's stance is that obfuscation wrappers belong in a fork, not
core. Don't expect this to change.

## Why "just use the fork" doesn't solve it for us

- **iOS (Carol, Maxim, Sean):** SFI is closed-source and Apple-signed.
  There is no iOS build of any AWG-capable sing-box in the App Store, and
  there's no realistic sideload path on iOS (TestFlight slots are scarce
  and fragile; AltStore needs a dev cert that expires every 7 days for
  free tiers). Hard wall.
- **Android (Eric):** SFA from Play / F-Droid is stock SagerNet. Getting
  AWG on Android means compiling SFA from source against
  `hoaxisr/amnezia-box` as the core and sideloading the resulting APK —
  doable but creates a bespoke APK that Watchtower-equivalent auto-update
  doesn't cover.
- **Windows (laptops):** we control the binary via the installer, so
  swapping to a fork build is tractable. But running a fork on Windows
  and stock on mobile = schema/feature drift + upstream-fix lag in one
  half of the fleet. Not worth it for a single protocol.

## AWG 2.0 parameters

If we ever standardise on a fork, note that `hoaxisr/amnezia-box` has
moved past AWG 1.0 (what our server speaks today):

| Param | AWG 1.0 (our server) | AWG 2.0 (hoaxisr) |
|---|---|---|
| `Jc` / `Jmin` / `Jmax` | scalar | scalar |
| `S1` / `S2` | scalar | scalar |
| `S3` / `S4` | — | new padding stages |
| `H1`–`H4` | fixed value | **ranges** (pick per-handshake from a range) |
| `I1`–`I5` | — | new obfuscation chains |

Moving the household to AWG 2.0 requires:
1. Upgrading `/opt/docker/apps/amneziawg/entrypoint.sh` / kernel module
   support (the Oracle 6.17 kernel shipped AWG 1.0 extended UAPI; 2.0 is
   newer and may need the userspace `amneziawg-go` implementation).
2. Rewriting every client `awg-*.conf` with matching params.
3. Coordinating a flag-day rollout — AWG params must match exactly, so
   there's no rolling upgrade window.

Don't chase this unless there's a concrete DPI-detection incident that
1.0 parameters can no longer evade.

## What we did do (2026-04-24)

- Fixed junk-packet param mismatches on Eric's and Maxim's mobile configs
  (client `Jc/Jmin/Jmax` didn't match server values).
- Removed unused `carolpc` peer, then added it back alongside new
  `olivier/phone`, `sean/iphone`, `carol/pc` peers. 7 live peers total.
- Updated `apps/amneziawg/README.md` Users table to reflect the new
  roster.
- Scoped `render.py` AWG integration → rejected as above.

## Sources

- <https://github.com/hoaxisr/amnezia-box>
- <https://github.com/hoaxisr/amnezia-box/releases>
- <https://github.com/amnezia-vpn/amnezia-box>
- <https://github.com/SagerNet/sing-box/issues/1928>
- <https://github.com/SagerNet/sing-box/issues/2276>
- <https://github.com/SagerNet/sing-box/issues/3159>
- <https://f-droid.org/packages/io.nekohasekai.sfa/>
- <https://play.google.com/store/apps/details?id=io.nekohasekai.sfa>
