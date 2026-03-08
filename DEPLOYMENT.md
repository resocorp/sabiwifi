# SabiWiFi — Deployment & FreeRADIUS Guide

## Architecture Overview

```
┌──────────────────────────────────────────────────┐
│  Ubuntu 24.04 VPS                                │
│                                                  │
│  ┌──────────────┐   ┌────────────────────────┐   │
│  │ PostgreSQL   │   │  Django (Gunicorn)      │   │
│  │  :5432       │◄──│  :8000                  │   │
│  │  - app tables│   │  - REST API             │   │
│  │  - rad* tbls │   │  - Dashboard            │   │
│  └──────┬───────┘   │  - Captive portal       │   │
│         │           └────────────────────────┘   │
│  ┌──────┴───────┐                                │
│  │ FreeRADIUS 3 │   ┌──────────────────────┐     │
│  │  :1812/udp   │   │  Nginx :80/:443      │     │
│  │  :1813/udp   │   └──────────────────────┘     │
│  │  (rlm_sql)   │                                │
│  └──────────────┘   ┌──────────────────────┐     │
│                     │  WireGuard :51820/udp │     │
│                     └──────────────────────┘     │
└──────────────────────────────────────────────────┘
```

**4 running processes:** PostgreSQL, Django/Gunicorn, FreeRADIUS, Nginx
**Plus:** WireGuard (kernel module, not a process)

---

## 1. Server Setup (Ubuntu 24.04)

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install base dependencies
sudo apt install -y python3 python3-pip python3-venv postgresql postgresql-contrib \
    nginx certbot python3-certbot-nginx wireguard freeradius freeradius-postgresql git
```

## 2. PostgreSQL Setup

```bash
# Create database and user
sudo -u postgres psql <<EOF
CREATE DATABASE sabiwifi;
CREATE USER sabiwifi WITH PASSWORD 'your-strong-password-here';
GRANT ALL PRIVILEGES ON DATABASE sabiwifi TO sabiwifi;
ALTER USER sabiwifi CREATEDB;  -- needed for Django tests
\c sabiwifi
GRANT ALL ON SCHEMA public TO sabiwifi;
EOF
```

## 3. FreeRADIUS Setup

This is the critical piece. FreeRADIUS reads from the same PostgreSQL database as Django.

### 3.1 Install FreeRADIUS + PostgreSQL module

```bash
sudo apt install -y freeradius freeradius-postgresql
```

### 3.2 Create RADIUS tables in PostgreSQL

FreeRADIUS provides a schema file. Import it:

```bash
sudo -u postgres psql sabiwifi < /etc/freeradius/3.0/mods-config/sql/main/postgresql/schema.sql
```

This creates: `radcheck`, `radreply`, `radusergroup`, `radgroupcheck`, `radgroupreply`, `radacct`, `radpostauth`, `nas`

Grant permissions:

```bash
sudo -u postgres psql sabiwifi <<EOF
GRANT ALL ON ALL TABLES IN SCHEMA public TO sabiwifi;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO sabiwifi;
EOF
```

### 3.3 Configure FreeRADIUS to use PostgreSQL

Enable the SQL module:

```bash
cd /etc/freeradius/3.0/mods-enabled/
sudo ln -sf ../mods-available/sql sql
```

Edit `/etc/freeradius/3.0/mods-available/sql`:

```
sql {
    dialect = "postgresql"
    driver = "rlm_sql_postgresql"

    server = "localhost"
    port = 5432
    login = "sabiwifi"
    password = "your-strong-password-here"
    radius_db = "sabiwifi"

    read_clients = yes

    # Standard table names (match Django models)
    client_table = "nas"

    accounting {
        reference = "%{tolower:type.%{Acct-Status-Type}.query}"
    }

    post-auth {
        reference = ".query"
    }
}
```

### 3.4 Configure FreeRADIUS sites

Edit `/etc/freeradius/3.0/sites-enabled/default`:

In the `authorize` section, add:
```
sql
```

In the `authenticate` section, ensure:
```
Auth-Type PAP {
    pap
}
```

In the `accounting` section, add:
```
sql
```

In the `post-auth` section, add:
```
sql
```

### 3.5 Enable NAS client reading from SQL

In `/etc/freeradius/3.0/radiusd.conf`, ensure:
```
# Comment out or remove the file-based clients
# $INCLUDE clients.conf
```

FreeRADIUS will read NAS clients from the `nas` table (which Django populates when routers are provisioned).

### 3.6 Configure MikroTik dictionary

```bash
# MikroTik vendor attributes should already be included
# Verify:
grep -r "Mikrotik" /etc/freeradius/3.0/dictionary*
# If missing, add to /etc/freeradius/3.0/dictionary:
# $INCLUDE dictionary.mikrotik
```

### 3.7 Test FreeRADIUS

```bash
# Start in debug mode first
sudo freeradius -X

# In another terminal, test with radtest:
# (after Django has created a test user in radcheck)
radtest testuser testpassword localhost 0 testing123
```

### 3.8 Start FreeRADIUS as service

```bash
sudo systemctl enable freeradius
sudo systemctl start freeradius
sudo systemctl status freeradius
```

### 3.9 How FreeRADIUS + Django interact

```
Django writes to:              FreeRADIUS reads from:
─────────────────              ──────────────────────
radcheck (auth credentials)  → authorize section (SQL)
radusergroup (user→plan map) → authorize section (SQL)
radgroupreply (speed limits) → authorize section (SQL)
radgroupcheck (device limit) → authorize section (SQL)
nas (router secrets)         → client lookup (SQL)

FreeRADIUS writes to:          Django reads from:
──────────────────────         ─────────────────
radacct (session data)       → usage stats, data cap checks
radpostauth (auth log)       → debug/audit
```

No HTTP API between them. Pure shared SQL.

---

## 4. Django Application Setup

```bash
# Clone project
cd /opt
sudo mkdir sabiwifi && sudo chown $USER:$USER sabiwifi
cd sabiwifi
git clone <your-repo-url> .

# Create virtualenv
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env
# Edit .env with production values:
#   SECRET_KEY=<generate with: python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())">
#   DEBUG=False
#   DB_NAME=sabiwifi
#   DB_USER=sabiwifi
#   DB_PASSWORD=your-strong-password-here
#   PAYSTACK_SECRET_KEY=sk_live_xxx
#   PAYSTACK_PUBLIC_KEY=pk_live_xxx
#   TERMII_API_KEY=your-termii-key
#   SERVER_IP=your-server-public-ip
#   SERVER_WG_PUBLIC_KEY=your-wg-public-key
#   PLATFORM_DOMAIN=sabiwifi.ng

# Run migrations (uses same DB as FreeRADIUS)
DJANGO_SETTINGS_MODULE=config.settings.prod python manage.py migrate

# Create superuser (operator)
DJANGO_SETTINGS_MODULE=config.settings.prod python manage.py createsuperuser

# Collect static files
DJANGO_SETTINGS_MODULE=config.settings.prod python manage.py collectstatic --noinput
```

## 5. Gunicorn Setup

```bash
# /etc/systemd/system/sabiwifi.service
[Unit]
Description=SabiWiFi Django Application
After=network.target postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/sabiwifi
Environment="DJANGO_SETTINGS_MODULE=config.settings.prod"
ExecStart=/opt/sabiwifi/venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable sabiwifi
sudo systemctl start sabiwifi
```

## 6. Nginx Configuration

```nginx
# /etc/nginx/sites-available/sabiwifi
server {
    listen 80;
    server_name sabiwifi.ng www.sabiwifi.ng;

    location /static/ {
        alias /opt/sabiwifi/staticfiles/;
    }

    location /media/ {
        alias /opt/sabiwifi/media/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/sabiwifi /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# SSL with Certbot
sudo certbot --nginx -d sabiwifi.ng -d www.sabiwifi.ng
```

## 7. WireGuard Setup

```bash
# Generate server keys
wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key

# /etc/wireguard/wg0.conf
[Interface]
Address = 10.99.0.1/16
PrivateKey = <contents of server_private.key>
ListenPort = 51820
```

Router peers are added dynamically by Django when routers are provisioned:
```bash
# Django calls this (or uses wg set) when a router is assigned:
wg set wg0 peer <router_public_key> allowed-ips <router_tunnel_ip>/32
```

```bash
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0
```

## 8. Cron Jobs

```bash
# /etc/cron.d/sabiwifi
DJANGO_SETTINGS_MODULE=config.settings.prod

*/15 * * * * www-data cd /opt/sabiwifi && venv/bin/python manage.py check_expiry
*/5  * * * * www-data cd /opt/sabiwifi && venv/bin/python manage.py check_routers
*/5  * * * * www-data cd /opt/sabiwifi && venv/bin/python manage.py check_alerts
*/15 * * * * www-data cd /opt/sabiwifi && venv/bin/python manage.py send_expiry_reminders
0    8 * * * www-data cd /opt/sabiwifi && venv/bin/python manage.py daily_summary
0    3 * * * www-data cd /opt/sabiwifi && venv/bin/python manage.py cleanup_otp
```

## 9. Firewall

```bash
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP
sudo ufw allow 443/tcp     # HTTPS
sudo ufw allow 51820/udp   # WireGuard
# Do NOT expose 1812/1813 to the internet — RADIUS runs over WireGuard only
sudo ufw enable
```

---

## Testable Workflows

### Reseller Workflows
1. **Signup** — `/signup/` → create account → redirect to dashboard
2. **Login** — `/login/` → email + password → dashboard
3. **Add Router** — Dashboard → enter serial → pending provision
4. **Router Provisioning** — Router phones home → gets .rsc → comes online
5. **Create Free Plan** — Dashboard → Plans → Create → free plan allowed
6. **Create Paid Plan (blocked)** — Without bank → error message
7. **Bank Setup** — Settings → select bank → enter account → resolve → save → verified
8. **Create Paid Plan (allowed)** — With bank verified → success
9. **Edit Plan** — Update speed/price/duration
10. **Disable Plan** — Soft-disable, RADIUS attributes cleared
11. **View Subscribers** — List, search, filter by status
12. **View Subscriber Detail** — Plan, usage, payment history
13. **View Payments** — Transaction list, monthly summary, earnings
14. **Update Branding** — Template, colors, title, logo upload, bg upload
15. **Update Account** — Business name, email, phone, location
16. **View Routers** — List with status, last seen

### Subscriber Workflows
17. **Signup (captive portal)** — Phone + email → OTP → verify → set PIN
18. **Login (captive portal)** — Phone + PIN → auth token → MikroTik login
19. **Login (self-service)** — `/account/` → phone + PIN (no serial needed)
20. **Select Free Plan** — Plan list → select → activated immediately
21. **Select Paid Plan** — Plan list → Paystack payment → webhook → activated
22. **View Account** — Current plan, usage, expiry, devices
23. **Renew Plan** — Expired → renew same plan
24. **Change Plan** — Switch to different plan
25. **Change PIN** — Current PIN + new PIN
26. **Reset PIN** — Forgot PIN → OTP → new PIN
27. **Disconnect Session** — Force disconnect

### Operator Workflows
28. **Django Admin Login** — `/admin/` → manage all models
29. **Operator Overview** — `/operator/overview/` → platform metrics
30. **Register Router Serial** — Admin → Routers → Add → serial + status=available
31. **Manage Resellers** — Suspend/activate, edit commission
32. **Manage PlatformSettings** — Commission %, fee bearer, notification config
33. **View Audit Trail** — django-simple-history on all models

### Automated Workflows (Cron)
34. **Plan Expiry** — `check_expiry` → expire subscriptions, clear RADIUS
35. **Router Health** — `check_routers` → ping WG IPs, update status
36. **Alerts** — `check_alerts` → offline routers, payment failures, free limits
37. **Expiry Reminders** — `send_expiry_reminders` → SMS 24h before expiry
38. **Daily Summary** — `daily_summary` → operator SMS at 8am
