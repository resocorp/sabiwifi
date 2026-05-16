# SabiWiFi — Rebuild From Scratch

Everything you need to bring the platform back up on a fresh droplet after this
one is destroyed. Companion to `DEPLOYMENT.md` (which walks the original
greenfield install in finer detail).

---

## TL;DR

1. **Before shutdown**, grab the secrets bundle (Step 1). The repo alone is not enough.
2. Provision a new Ubuntu 24.04 droplet.
3. `git clone` this repo to `/opt/sabiwifi`.
4. Run through `DEPLOYMENT.md` for OS-level installs (Postgres, FreeRADIUS, nginx, WireGuard, etc.).
5. Drop the snapshotted system configs from `ops/system/` into place.
6. Restore the DB from the dump.
7. Restore secrets (`.env`, WireGuard keys, WhatsApp session) onto the new droplet.
8. Restart services and verify.

---

## 1. Pre-shutdown checklist — pull these OFF the droplet

These do NOT live in git and CANNOT be regenerated from the repo. Copy them
somewhere safe (1Password, encrypted USB, second cloud bucket — your call)
**before you destroy this droplet**:

```bash
# From your laptop, with SSH access to root@<droplet-ip>:
mkdir -p ~/sabiwifi-secrets-$(date +%Y%m%d)
cd ~/sabiwifi-secrets-$(date +%Y%m%d)

# 1. App config (real API keys + DB password + Fernet key)
scp root@<droplet-ip>:/opt/sabiwifi/.env ./env

# 2. Fresh DB dump (run a fresh one first, see below)
ssh root@<droplet-ip> 'sudo -u postgres /opt/sabiwifi/scripts/backup_db.sh'
scp root@<droplet-ip>:/var/backups/sabiwifi/sabiwifi-*.dump ./db/

# 3. WireGuard server keypair — required if you want existing MikroTik
#    routers to keep working without re-provisioning every one of them.
scp root@<droplet-ip>:/etc/wireguard/wg0.conf ./wg0.conf

# 4. WhatsApp sidecar session state (logged-in Baileys auth)
scp -r root@<droplet-ip>:/opt/sabiwifi-wa/sessions ./wa-sessions

# 5. Let's Encrypt certs — optional. Certbot will re-issue on the new
#    droplet once DNS points at it, so only grab these if you want a
#    seamless switchover with no re-issue window.
scp -r root@<droplet-ip>:/etc/letsencrypt ./letsencrypt
```

**Pinned IPs to remember**: the MikroTik routers in the field are configured
with the public IP `209.97.138.138` and the server WireGuard public key
`IvUiuOdSP7iiCMa83v/bDfCKgjnxkWTOIL+flD0sgz4=`. See "Changing droplet IP" at
the bottom for the implications.

---

## 2. New droplet provisioning

```bash
# Ubuntu 24.04 LTS, 2 vCPU / 2 GB RAM minimum (1 GB was too tight on the old one).
# Open ports: 22, 80, 443, 51820/udp (WireGuard).
# Do NOT open 1812/1813 (RADIUS) — those ride inside the WireGuard tunnel.
```

Then run the base install from `DEPLOYMENT.md` § 1:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv postgresql postgresql-contrib \
    nginx certbot python3-certbot-nginx wireguard freeradius freeradius-postgresql git \
    redis-server nodejs npm
```

## 3. Clone the repo

```bash
sudo mkdir -p /opt/sabiwifi
sudo chown $USER:$USER /opt/sabiwifi
git clone git@github.com:resocorp/sabiwifi.git /opt/sabiwifi
cd /opt/sabiwifi
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 4. Restore secrets

```bash
# Drop the saved .env into place
cp ~/sabiwifi-secrets-*/env /opt/sabiwifi/.env
chown www-data:www-data /opt/sabiwifi/.env
chmod 600 /opt/sabiwifi/.env

# WireGuard keys
sudo cp ~/sabiwifi-secrets-*/wg0.conf /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0
```

## 5. Restore the database

```bash
# Create role and empty DB (matches what FreeRADIUS expects)
sudo -u postgres psql <<'EOF'
CREATE ROLE sabiwifi LOGIN PASSWORD 'your-strong-password-here';
CREATE DATABASE sabiwifi OWNER sabiwifi;
GRANT ALL PRIVILEGES ON DATABASE sabiwifi TO sabiwifi;
EOF

# Apply the FreeRADIUS schema (see DEPLOYMENT.md § 3.2 — single command)
sudo -u postgres psql sabiwifi < /etc/freeradius/3.0/mods-config/sql/main/postgresql/schema.sql

# Now restore the dump. --no-owner lets it land into the sabiwifi role.
sudo -u postgres pg_restore --no-owner --no-privileges -d sabiwifi \
    ~/sabiwifi-secrets-*/db/sabiwifi-*.dump
```

## 6. Drop system configs into place

The repo carries snapshots of every system file under `ops/system/`. Copy them
out to their canonical locations:

```bash
cd /opt/sabiwifi/ops/system

# systemd units + drop-ins
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/sabiwifi.service.d
sudo cp systemd/sabiwifi.service.d/*.conf /etc/systemd/system/sabiwifi.service.d/

# cron
sudo cp cron/sabiwifi /etc/cron.d/sabiwifi
sudo chmod 644 /etc/cron.d/sabiwifi

# nginx sites (replace certbot paths after you re-issue or restore certs)
sudo cp nginx/sabiwifi /etc/nginx/sites-available/sabiwifi
sudo cp nginx/openwisp /etc/nginx/sites-available/openwisp
sudo ln -sf /etc/nginx/sites-available/sabiwifi /etc/nginx/sites-enabled/sabiwifi
# (openwisp link only if you're also bringing OpenWISP back up)

# sudoers (NOPASSWD entries for wg + freeradius)
sudo cp sudoers/sabiwifi-wg /etc/sudoers.d/sabiwifi-wg
sudo cp sudoers/sabiwifi-freeradius /etc/sudoers.d/sabiwifi-freeradius
sudo chmod 440 /etc/sudoers.d/sabiwifi-*

# FreeRADIUS — see DEPLOYMENT.md § 3.3 for the SQL module wiring.
# The committed copies in freeradius/ are reference baselines.
```

## 7. Bring up the WhatsApp sidecar

```bash
sudo cp -r /opt/sabiwifi/ops/wa-sidecar /opt/sabiwifi-wa
cd /opt/sabiwifi-wa
sudo chown -R www-data:www-data /opt/sabiwifi-wa
sudo -u www-data npm install --omit=dev

# Restore the saved Baileys session so WhatsApp stays logged in
sudo cp -r ~/sabiwifi-secrets-*/wa-sessions /opt/sabiwifi-wa/sessions
sudo chown -R www-data:www-data /opt/sabiwifi-wa/sessions
```

## 8. TLS certificates

If you copied `letsencrypt/` from the old droplet AND DNS still points at the
old IP (no switchover yet), drop the cert tree in:

```bash
sudo cp -r ~/sabiwifi-secrets-*/letsencrypt /etc/letsencrypt
```

Otherwise re-issue once DNS resolves to the new droplet:

```bash
sudo certbot --nginx -d app.sabiwifi.com -d www.app.sabiwifi.com
```

## 9. Start everything

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now postgresql freeradius redis-server nginx wg-quick@wg0
sudo systemctl enable --now sabiwifi.service sabiwifi-rqworker.service sabiwifi-whatsapp.service
sudo systemctl enable --now sabiwifi-expire-plans.timer sabiwifi-check-routers.timer \
                            sabiwifi-process-broadcasts.timer sabiwifi-reconcile-wg.timer \
                            sabiwifi-field-pings.timer sabiwifi-backup.timer
sudo systemctl reload nginx

# Static + migrations (no-op on most pulls, idempotent)
cd /opt/sabiwifi
sudo -u www-data DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py migrate
sudo -u www-data DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart sabiwifi.service
```

## 10. Smoke tests

```bash
# Django up
curl -I https://app.sabiwifi.com/
# Should be 200 or 302.

# WireGuard up + peers attached
sudo wg show

# RADIUS up (test against a known subscriber)
radtest <username> <password> 127.0.0.1 0 testing123

# Background jobs scheduled
systemctl list-timers 'sabiwifi-*'

# WhatsApp sidecar
curl http://127.0.0.1:3001/health
```

---

## Changing droplet IP — read this carefully

Every deployed MikroTik router has a baked-in WireGuard peer config pointing at
the old droplet's `SERVER_IP` + `SERVER_WG_PUBLIC_KEY`. If you change either of
these on rebuild, the routers will NOT reconnect on their own.

Three options:

| Strategy | What to do |
|---|---|
| **Same IP + same keys** | Use a floating IP / reserved IP at your cloud provider so the new droplet inherits the old public IP. Restore `/etc/wireguard/wg0.conf` from the secrets bundle. Field routers reconnect automatically. |
| **Different IP, same keys** | Restore the saved `wg0.conf`, then update each router's WireGuard peer endpoint (RouterOS: `/interface wireguard peers set [find] endpoint-address=<new-ip>`). Scripted bulk update is doable via the RouterOS API since Django already stores router creds in the `Router` model. |
| **Different IP + new keys** | Treat existing field routers as decommissioned. Re-run the provisioning script (`routers/routeros_utils.py`) against each one in person. Only acceptable if the deployed fleet is small (it is — see field-count below). |

Current fleet at time of writing: **3 partners, 2 routers, 4 subscribers**.
Small enough that re-provisioning by hand is a real option.

---

## What's in the repo vs what isn't

| In repo (`git clone` gets you this) | NOT in repo (must copy from droplet) |
|---|---|
| All Django source | `.env` (real secrets) |
| `ops/system/*` (systemd, nginx, cron, sudoers, freeradius baseline) | DB contents (use a pg_dump) |
| `ops/wa-sidecar/*` (Node sidecar source) | WireGuard private key (`wg0.conf`) |
| `ops/voice-droplet/*` (AVR voice infra docker) | WhatsApp Baileys session (`/opt/sabiwifi-wa/sessions/`) |
| `DEPLOYMENT.md`, `REBUILD.md`, `CLAUDE.md`, `DESIGN.md` | Let's Encrypt certs (optional — can re-issue) |
| `requirements.txt`, `package.json` | `/etc/freeradius/3.0/mods-enabled/sql` symlink target (recreate per DEPLOYMENT.md § 3.3) |

---

## Companion services on other infrastructure

These run on different droplets and are out of scope for *this* rebuild:

- **OpenWISP** at `wisp.sabiwifi.com` — runs in Docker (`docker-openwisp` stack on this same droplet via a separate site config). If you also want to bring that back, the nginx vhost is at `ops/system/nginx/openwisp` and the Postgres `openwisp` DB lives in the same cluster — back it up the same way (`pg_dump openwisp`).
- **AVR voice droplet** at `159.65.225.144:8088` — separate droplet, Dockerised. Source under `ops/voice-droplet/`. See its own `bootstrap.sh` for setup.
