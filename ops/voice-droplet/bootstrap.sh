#!/usr/bin/env bash
# SabiWiFi voice droplet bootstrap
# Target: fresh Ubuntu 24.04 LTS (noble)
# Installs: 2G swap, ufw/fail2ban/unattended-upgrades, Docker + Compose,
#           Asterisk 20 LTS (with AudioSocket), clones avr-infra.
#
# Usage:
#   scp bootstrap.sh root@<droplet-ip>:/root/
#   ssh root@<droplet-ip> 'bash /root/bootstrap.sh'
#
# Post-install (YOU provide these):
#   - NG SIP carrier credentials  → /etc/asterisk/pjsip.conf (trunk + endpoints)
#   - ASR/LLM/TTS API keys        → /opt/avr-infra/.env
#   - Public FQDN                 → MY_DOMAIN in .env
#   - Narrow 5060/tcp+udp in ufw to carrier IP subnet (replace 0.0.0.0/0)
#
# First verified run: 2026-04-24 on 159.65.225.144 (1 vCPU / 961 MB / 24 GB).
# 961 MB is undersized per the approved plan (2 vCPU / 4 GB). 2G swap compensates
# for idle/install; resize before carrying real call traffic.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() { echo -e "\n\033[1;36m[bootstrap]\033[0m $*"; }

# ---------------------------------------------------------------------------
# 1. Swap (2 GB, swappiness=10 so kernel prefers RAM — voice is latency-sensitive)
# ---------------------------------------------------------------------------
log "swap"
if ! swapon --show 2>/dev/null | grep -q swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi

# ---------------------------------------------------------------------------
# 2. Hostname
# ---------------------------------------------------------------------------
log "hostname"
hostnamectl set-hostname sabiwifi-voice

# ---------------------------------------------------------------------------
# 3. Base packages + security
# ---------------------------------------------------------------------------
log "apt update + base packages"
apt-get -qq update
apt-get -qq -y upgrade
apt-get -qq -y install \
  ufw fail2ban unattended-upgrades \
  curl ca-certificates gnupg lsb-release software-properties-common \
  chrony jq git

# ---------------------------------------------------------------------------
# 4. Firewall
#    Leave 5060 open to 0.0.0.0 during bootstrap; narrow to carrier subnet
#    AFTER you've picked the NG SIP provider. Keeping 5060 world-open with an
#    Asterisk that has trunk credentials is an INVITE-bruteforce target.
# ---------------------------------------------------------------------------
log "ufw"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp          comment 'ssh'
ufw allow 5060/udp        comment 'sip-signalling (TODO narrow to carrier subnet)'
ufw allow 5060/tcp        comment 'sip-tcp (TODO narrow to carrier subnet)'
ufw allow 10000:20000/udp comment 'rtp-media'
echo 'y' | ufw enable >/dev/null

# ---------------------------------------------------------------------------
# 5. Services
# ---------------------------------------------------------------------------
log "fail2ban + unattended-upgrades + chrony"
systemctl enable --now fail2ban chrony unattended-upgrades >/dev/null 2>&1

# ---------------------------------------------------------------------------
# 6. Docker (official repo)
# ---------------------------------------------------------------------------
log "docker"
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get -qq update
apt-get -qq -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker >/dev/null 2>&1

# ---------------------------------------------------------------------------
# 7. Asterisk 20 LTS (Ubuntu noble ships 20.6.0 with AudioSocket modules)
#    Stopped + disabled on purpose — start only after dialplan + carrier config.
# ---------------------------------------------------------------------------
log "asterisk 20"
apt-get -qq -y install asterisk asterisk-modules asterisk-core-sounds-en
systemctl stop asterisk >/dev/null 2>&1 || true
systemctl disable asterisk >/dev/null 2>&1

if ! ls /usr/lib/x86_64-linux-gnu/asterisk/modules/ 2>/dev/null | grep -q audiosocket; then
  echo "[FATAL] app_audiosocket.so missing — unexpected on Ubuntu noble" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 8. avr-infra (clone only — do not bring up containers yet, env is unfilled)
# ---------------------------------------------------------------------------
log "avr-infra"
mkdir -p /opt
cd /opt
if [ ! -d avr-infra ]; then
  git clone --depth 1 https://github.com/agentvoiceresponse/avr-infra.git
fi
cd /opt/avr-infra
if [ ! -f .env ]; then
  {
    echo '# Populated by SabiWiFi bootstrap — fill TODOs before docker compose up.'
    echo '# Recommended Phase 2 stack: Anthropic LLM + Deepgram ASR + Cartesia TTS.'
    cat .env.example
  } > .env
fi

# ---------------------------------------------------------------------------
# 9. Summary
# ---------------------------------------------------------------------------
log "done — summary:"
echo "  asterisk:  $(asterisk -V 2>/dev/null || echo NOT_INSTALLED)"
echo "  docker:    $(docker --version 2>/dev/null || echo NOT_INSTALLED)"
echo "  compose:   $(docker compose version 2>/dev/null || echo NOT_INSTALLED)"
echo "  avr-infra: /opt/avr-infra (provider compose files ready)"
echo "  swap:      $(swapon --show --noheadings | awk '{print $3}' | head -1)"
echo "  ufw:       $(ufw status | head -1)"
echo
echo "NEXT (user):"
echo "  1. Pick NG SIP carrier, feed creds into /etc/asterisk/pjsip.conf"
echo "  2. Fill API keys in /opt/avr-infra/.env"
echo "  3. ufw route limit — swap 0.0.0.0 on 5060 to carrier /24 subnet"
echo "  4. Resize droplet to ≥2 vCPU / 4 GB before first live call"
echo "  5. systemctl enable --now asterisk  +  cd /opt/avr-infra && docker compose -f docker-compose-anthropic.yml up -d"
