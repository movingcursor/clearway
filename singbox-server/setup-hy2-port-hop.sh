#!/usr/bin/env bash
# Add the iptables NAT redirect that backs Hysteria2 port-hopping.
# Clients with `server_ports: ["20000:30000"]` dial random ports from
# that range; this rule collapses them back to the single port that
# sing-box's hy2 inbound actually listens on. Without it, clients would
# hit closed ports.
#
# Why we do this: the GFW marks IPs that send sustained high-volume
# UDP traffic on a fixed port and drops *all* incoming UDP from the
# overseas side for ~hour windows (apernet/hysteria#1157). Spreading
# packets across thousands of ports keeps any single 5-tuple flow under
# the volume threshold while the aggregate gets through. See
# docs/hazards.md.
#
# Idempotent. Saves the resulting ruleset via netfilter-persistent so
# it survives reboot.
#
# Env (with defaults matching the production manifest):
#   HY2_LISTEN_IP    IP that sing-box's hy2 inbound binds to
#                    (must match defaults.reality.server in profiles.yaml)
#   HY2_LISTEN_PORT  port sing-box listens on
#   HY2_PORT_RANGE   range to redirect from (must match the value of
#                    defaults.hysteria2.server_ports[0] in profiles.yaml)

set -euo pipefail

HY2_LISTEN_IP="${HY2_LISTEN_IP:-10.0.0.220}"
HY2_LISTEN_PORT="${HY2_LISTEN_PORT:-443}"
HY2_PORT_RANGE="${HY2_PORT_RANGE:-20000:30000}"

if ! command -v iptables >/dev/null 2>&1; then
    echo "iptables not found" >&2
    exit 1
fi

if ! iptables -t nat -C PREROUTING \
        -d "$HY2_LISTEN_IP" -p udp --dport "$HY2_PORT_RANGE" \
        -j REDIRECT --to-ports "$HY2_LISTEN_PORT" 2>/dev/null; then
    iptables -t nat -A PREROUTING \
        -d "$HY2_LISTEN_IP" -p udp --dport "$HY2_PORT_RANGE" \
        -j REDIRECT --to-ports "$HY2_LISTEN_PORT"
    echo "Added: UDP $HY2_LISTEN_IP:$HY2_PORT_RANGE -> :$HY2_LISTEN_PORT"
else
    echo "Already present: UDP $HY2_LISTEN_IP:$HY2_PORT_RANGE -> :$HY2_LISTEN_PORT"
fi

if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null
    echo "Persisted via netfilter-persistent."
else
    echo "WARNING: netfilter-persistent not installed — rule will not survive reboot." >&2
fi
