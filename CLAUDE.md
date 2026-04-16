# SabiWiFi — CLAUDE.md

Project-level context for Claude Code. Keep this updated when architecture, conventions, or key decisions change.

---

## What This Project Is

Multi-tenant WiFi reseller management platform (Nigeria). Three personas:
- **Platform Operator** (staff) — runs the platform, earns commission on all payments
- **Reseller** — business owner deploying WiFi hotspots via MikroTik routers
- **Subscriber** — end user connecting to a reseller's hotspot via captive portal

Full docs: `docs/PRD.md` (requirements), `docs/UXUI_DESIGN.md` (design).

---

## Stack

| Layer | Technology |
|-------|-----------|
| Framework | Django 5.1 + DRF |
| DB | PostgreSQL 16 (shared with FreeRADIUS via unmanaged ORM models) |
| Cache / Sessions | Redis (maxmemory 128mb, allkeys-lru) |
| WSGI | Gunicorn + Gevent (2 workers, max-requests 500) |
| Proxy | Nginx |
| WiFi Auth | FreeRADIUS 3 (rlm_sql, same Postgres DB) |
| Routers | MikroTik RouterOS (WireGuard tunnel + RADIUS) |
| Payments | Paystack (split payments, subaccounts) |
| SMS | Termii |
| WhatsApp | Baileys Node.js sidecar on port 3001 (`/opt/sabiwifi-wa/`) |
| Background | Systemd timers + cron (`/etc/cron.d/sabiwifi`) |

---

## Django Apps

```
accounts/       Reseller auth, Reseller model, Subscriber model, Country/phone normalisation
plans/          ServicePlan, Subscription — RADIUS group sync on save
billing/        Payment, Paystack integration, split payment logic
routers/        Router, RouterHealthLog, WireGuard peer management, RouterOS API
portal/         Captive portal pages + subscriber API (OTP, login, plans, payment)
dashboard/      Reseller dashboard (server-rendered HTML, @login_required)
notifications/  WhatsApp session, broadcast, templates, notification log, notify.py
radius/         Unmanaged ORM models for FreeRADIUS tables (radcheck, radreply, etc.)
operator_panel/ Staff-only platform overview, PlatformSettings singleton
```

---

## Key File Locations

| What | Where |
|------|-------|
| Settings (base) | `config/settings/base.py` |
| Settings (prod) | `config/settings/prod.py` |
| URL root | `config/urls.py` |
| Env file | `/opt/sabiwifi/.env` |
| Notification dispatch | `notifications/notify.py` |
| SMS send | `notifications/sms.py` |
| WA send | `notifications/notify.py` → `send_whatsapp()` → Node on :3001 |
| RADIUS utils | `radius/utils.py` |
| WireGuard utils | `routers/wg_utils.py` |
| Paystack provider | `billing/providers/paystack.py` |
| Portal page views | `portal/page_views.py` |
| Portal API views | `portal/views.py` |
| Plan RADIUS sync | `plans/signals.py` or `plans/views.py` — `sync_plan_to_radius()` |
| Node WA sidecar | `/opt/sabiwifi-wa/index.js` |
| Gunicorn service | `/etc/systemd/system/sabiwifi.service` |
| WA sidecar service | `/etc/systemd/system/sabiwifi-whatsapp.service` |
| Broadcast timer | `/etc/systemd/system/sabiwifi-process-broadcasts.timer` |
| Cron jobs | `/etc/cron.d/sabiwifi` |

---

## Models Cheat Sheet

```
Reseller          OneToOne → User. Has slug, status, branding (JSONField), paystack_subaccount_code
Subscriber        FK → Reseller. Phone unique per reseller. PIN hashed. auth_token for portal.
ServicePlan       FK → Reseller. Price, speed, duration, data_cap, RADIUS group name.
Subscription      FK → Subscriber + Plan. start_date, expiry_date, status.
Payment           FK → Subscriber + Plan. Paystack reference, split snapshot fields.
Router            FK → Reseller (nullable). WireGuard keys, tunnel IP, NAS secret, last_seen.
RouterHealthLog   FK → Router. event=online/offline, created_at.
WhatsappSession   OneToOne → Reseller. status, wa_phone.
ResellerNotificationConfig  OneToOne → Reseller. Per-event toggles + channel selection.
NotificationTemplate  FK → Reseller. event_type (8 types), body with {{var}} syntax.
Broadcast         FK → Reseller. type=alert/promo/marketing, status, progress tracking.
NotificationLog   FK → Reseller + Subscriber + Broadcast. Audit trail for all messages.
PlatformSettings  Singleton (pk=1). Commission %, API keys, platform domain.
```

---

## Auth Patterns

**Reseller (dashboard)**: Django session auth (`@login_required`). Token auth (`Authorization: Token`) for API.

**Subscriber (portal)**: Stateless `X-Auth-Token` header. Token stored in DB (`Subscriber.auth_token`). OTP via Termii/WA → verify → set PIN → token returned.

**Operator panel**: `@user_passes_test(lambda u: u.is_staff)`.

**WA webhook**: `X-WA-API-Key` header matched against `settings.WA_API_KEY` (in `.env`).

---

## Important Conventions

- **Naming (UI vs code)**: User-facing copy says **"Partner"**; code still uses `Reseller` (class names, FKs, URLs, slugs, RADIUS group prefix). When editing UI strings, use "Partner". When editing code, keep `reseller`. Full code rename deferred until product naming is final.
- **Phone normalisation**: Always normalise to E.164 on write. `Country.to_international()` / `normalize_to_local()`.
- **Reseller isolation**: Filter every query by `reseller`. Never return cross-tenant data.
- **RADIUS group name**: `{reseller.slug}-{plan.slug}` — must match between Django and FreeRADIUS.
- **Template variables**: `{{var}}` syntax (double curly, not Django's `{{ }}`). Rendered by `notify.py`'s `_render()`.
- **PlatformSettings fallback**: `reseller.get_commission_pct()` → reseller override → `PlatformSettings.load()`.
- **Unmanaged RADIUS models**: Never run migrations on `radius/` app. Schema owned by FreeRADIUS.
- **Payment snapshots**: Commission and fee fields on Payment are immutable snapshots. Don't recalculate from current config.
- **Portal branding**: Primary colour injected as `--color-primary` CSS var. All `text-primary`/`bg-primary` tokens resolve to it.

---

## Background Jobs

| Command | Frequency | What it does |
|---------|-----------|-------------|
| `expire_plans` | Every 5 min (systemd) | Mark expired subs, CoA disconnect, RADIUS cleanup |
| `check_routers` | Every 2 min (systemd) | Ping WireGuard IPs, update online/offline, notify |
| `process_broadcasts` | Every 1 min (systemd) | Send queued broadcast messages (30 SMS/run) |
| `send_expiry_reminders` | Daily 08:00 (cron) | 3-day and 1-day expiry warnings |
| `daily_summary` | Daily 23:00 (cron) | Platform stats for operator |
| `cleanup_otp` | Hourly (cron) | Remove expired OTP Redis keys |
| `clearsessions` | Daily 00:00 (cron) | Django session cleanup |

---

## External Service Integration Points

**Paystack**: `billing/providers/paystack.py`. `initialize_payment()`, `verify_payment()`, `create_subaccount()`. Webhook at `/api/billing/webhook/` (HMAC-SHA512 verified).

**Termii**: `notifications/sms.py`. `send_sms()`, `send_otp()`. Fallback to stdout if `TERMII_API_KEY=placeholder`.

**WhatsApp (Baileys)**: Node sidecar at `http://127.0.0.1:3001`. Django POSTs to `/send`. Node webhooks back to `/api/notifications/wa-webhook/` with `X-WA-API-Key`.

**FreeRADIUS**: Shared Postgres DB. `radius/utils.py` writes directly to `radcheck`, `radgroupreply`, `radusergroup`, `nas`. CoA disconnect via `pyrad`.

**WireGuard**: `routers/wg_utils.py`. Calls `sudo wg` and `sudo wg-quick`. Peer IPs from 10.99.0.0/16. Requires `/etc/sudoers.d/sabiwifi-wg` NOPASSWD.

**MikroTik RouterOS API**: `routers/routeros_utils.py`. `routeros_api` library. Credentials stored in `Router` model.

---

## Portal Themes

Three themes in `templates/portal/{modern,bold,minimal}/`. Each has `login.html`, `signup.html`, `connected.html`, `account.html`. Theme selected via `reseller.branding['template']`. Reseller's `primary_color` injected as CSS variable.

---

## Service Management

```bash
systemctl restart sabiwifi.service           # Django/Gunicorn
systemctl restart sabiwifi-whatsapp.service  # Node WA sidecar
systemctl restart nginx
systemctl restart redis-server
systemctl restart freeradius
systemctl restart postgresql@16-main

# Run management commands
cd /opt/sabiwifi && DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py <command>

# Tail logs
journalctl -u sabiwifi.service -f
journalctl -u sabiwifi-whatsapp.service -f
tail -f /var/log/sabiwifi-cron.log
```

---

## Current Server Specs & Limits

- 1 vCPU, 1 GB RAM (76% at idle), 33 GB disk — DigitalOcean Premium Intel
- **Upgrade to 2 GB droplet recommended** (RAM is the primary bottleneck)
- Redis capped at 128mb (configured)
- Gunicorn: 2 gevent workers, max-requests 500 (configured)
- No DB backups configured yet (priority action)
- No HA/failover (single server)

---

## Git / Deploy Workflow

- Branch: `main` → direct push to origin (no PR workflow currently)
- Remote: `github.com:resocorp/sabiwifi.git`
- No CI/CD — deploy = `git pull` on server + `systemctl restart sabiwifi.service`
- Static files: `venv/bin/python manage.py collectstatic --noinput`
- Migrations: `venv/bin/python manage.py migrate` (never touch `radius/` app migrations)
