#!/bin/sh
# Firewall for the comment server (installed as /usr/local/sbin/tingposten-debatt-brannmur,
# run at boot by tingposten-debatt-brannmur.service after Docker).
#
# The containers (172.30.53.0/24) may reach the internet (Tailscale, e-mail, Jev), but not
# the home networks, other Docker networks, the tailnet (100.64.0.0/10: traffic there would
# leave through the Pi's own Tailscale node, as the Pi) or the Pi itself. Safe to run again:
# a rule that is already there is not added twice.
set -eu
NETT=172.30.53.0/24
regel() { iptables -C "$@" 2>/dev/null || iptables -I "$@"; }
for maal in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
  regel DOCKER-USER -s "$NETT" -d "$maal" -j DROP
done
regel INPUT -s "$NETT" -j DROP
echo "brannmur for $NETT er på plass"
