# SabiWiFi — Product Requirements Document

**Version**: 1.0
**Date**: March 2026
**Status**: Live — Based on Deployed System

---

## 1. Product Overview

> **Positioning note**: See `docs/POSITIONING.md` for how SabiWiFi presents itself publicly. In short — SabiWiFi the **ISP brand** is the public face (consumer-facing `sabiwifi.com`); SabiWiFi the **platform** is the white-label multi-tenant backend that powers partners on any backbone. Partners are invisible to consumers; they run under their own brand.

SabiWiFi is a consumer ISP brand backed by a multi-tenant operator platform. The public face (`sabiwifi.com`) presents as a standalone ISP serving end customers; the underlying platform enables partner operators to deploy, manage, and monetise their own WiFi hotspots using MikroTik (and other) hardware without requiring technical expertise. The platform handles the full lifecycle: router provisioning, subscriber access control, payment processing, and customer communication.

### 1.1 Problem Statement

Small WiFi operators in Nigeria (estates, hostels, hotels, business centres, lounges) lack affordable tooling to:
- Manage subscriber access and enforce plan limits automatically
- Accept online payments and split revenue with a platform operator
- Communicate with subscribers at scale (OTP, reminders, broadcasts)
- Monitor router health without a dedicated NOC team

### 1.2 Solution

A SaaS platform where:
- **Platform Operator** onboards resellers, provisions routers, and earns a commission on all payments
- **Resellers** manage their own subscriber base, plans, and branding through a dashboard
- **Subscribers** connect, pay, and manage their accounts through a branded captive portal

### 1.3 Core Value Propositions

| Persona | Value |
|---------|-------|
| Reseller | Launch a branded WiFi business in under an hour. Accept payments automatically. No RADIUS expertise required. |
| Subscriber | Connect, pay, and manage your WiFi from your phone. Receive reminders before your plan expires. |
| Platform Operator | Earn a configurable commission on every transaction across all resellers. |

---

## 2. Users & Personas

### 2.1 Platform Operator (Staff)
- One entity — the company running SabiWiFi
- Manages all resellers, routers, and platform settings
- Accesses the operator panel (staff-only Django view)
- Configures global commission %, fee bearer rules, SMS/payment credentials
- Monitors platform-wide revenue and router health

### 2.2 Reseller
- A business owner deploying WiFi in their location (estate, hostel, hotel, etc.)
- Signs up independently via the web
- Manages plans, subscribers, payments, and branding through the dashboard
- Connects their WhatsApp number for subscriber messaging
- Has their own Paystack subaccount for split payouts
- Receives operational alerts (new subscribers, payments, router health)

### 2.3 Subscriber
- End user connecting to a partner's WiFi hotspot (and, on partners running under the SabiWiFi brand, a SabiWiFi customer)
- Interacts through the partner-branded captive portal when connecting
- Can also recharge via the public `sabiwifi.com` landing widget — that flow renders SabiWiFi-branded end-to-end; the partner stays invisible to the customer (split-payment routing is unchanged)
- Phone number is the primary identifier (unique per partner). Cross-partner lookup is disambiguated at the API layer when needed.
- Manages their account at `/account/` from any device
- Opts into notification categories (alerts, promos, marketing)

---

## 3. System Architecture

### 3.1 Technology Stack

| Layer | Technology |
|-------|-----------|
| Web Framework | Django 5.1 (Python) |
| API Layer | Django REST Framework |
| Database | PostgreSQL 16 (shared with FreeRADIUS) |
| Cache / Sessions | Redis |
| WSGI Server | Gunicorn + Gevent (2 async workers) |
| Reverse Proxy | Nginx |
| WiFi Auth | FreeRADIUS 3 (rlm_sql backend) |
| Router Hardware | MikroTik RouterOS |
| VPN Tunnelling | WireGuard |
| Payment Gateway | Paystack (split payments + subaccounts) |
| SMS | Termii (OTP + notifications) |
| WhatsApp | Baileys/Node.js sidecar (one session per reseller) |
| Background Jobs | Systemd timers + cron |

### 3.2 Multi-Tenant Isolation

Each reseller is a tenant with isolated:
- Plans (`ServicePlan.reseller`)
- Subscribers (`Subscriber.reseller`) — phone uniqueness is per-reseller
- Payment records and subaccount
- RADIUS groups (`{reseller_slug}-{plan_slug}`)
- Portal branding
- WhatsApp session
- Notification config and templates

### 3.3 Data Planes

**Control Plane (this server)**:
- Portal, dashboard, API, RADIUS, WireGuard management
- Does not carry user WiFi traffic

**Data Plane (MikroTik routers)**:
- User WiFi traffic flows directly through the router
- Speed, data, and time limits enforced locally by RouterOS using RADIUS attributes

---

## 4. Feature Specifications

### 4.1 Reseller Onboarding

**FR-01: Reseller Signup**
- Reseller submits: business name, owner name, email, phone, password
- Email and phone must be unique across the platform
- Phone is normalised to international format on save
- Account is created with status = `setup`
- Returns authentication token on success

**FR-02: Getting Started Flow**
- New resellers land on a checklist-style overview page
- Steps: Create Plan → Claim Router → Set Up Bank Account → Connect Portal
- Progress tracked by reseller status (`setup` → `pending_router` → `active`)

**FR-03: Reseller Bank Account**
- Reseller submits bank code, account number
- Platform operator verifies and creates Paystack subaccount
- Sets `payment_verified = True` — unlocks paid plan creation
- Reseller without `payment_verified` can only create free plans (max 5 subscribers by default)

### 4.2 Plan Management

**FR-04: Plan Creation**
- Fields: name, download/upload speed (Mbps), duration (days or hours), data cap (GB or unlimited), max devices, price (NGN), trial flag
- RADIUS group created/updated automatically on save (`sync_plan_to_radius`)
- Plan slug is auto-generated from name, unique per reseller
- Free plans (price = 0) available to all resellers
- Paid plans require `payment_verified = True`

**FR-05: Plan Editing**
- All fields editable
- RADIUS group attributes updated on save (existing subscribers get new limits on next reconnect)

**FR-06: Plan Lifecycle**
- Enable / disable (soft toggle, no new subscribers on disabled plan)
- Delete (existing active subscriptions unaffected until natural expiry)

**FR-07: Plan RADIUS Attributes**
- Rate limit → `Mikrotik-Rate-Limit` (e.g., "2048k/10240k")
- Session timeout → `Session-Timeout` (duration_days × 86400 or duration_hours × 3600)
- Data cap → `Mikrotik-Total-Limit` (GB × 1024 × 1024)
- Simultaneous use → `Simultaneous-Use` (max_devices)
- Accounting interval → `Acct-Interim-Interval` (300 seconds)

### 4.3 Captive Portal

**FR-08: Portal Routing**
- MikroTik router redirects unauthenticated clients to `/portal/?r=<reseller_slug>`
- Reseller slug determines branding and plan list shown
- Portal works over HTTP (MikroTik hotspot intercepts before HTTPS)

**FR-09: Subscriber Signup**
- Subscriber enters phone number (and optional email)
- OTP sent via SMS or WhatsApp (4-digit code, 10-minute validity)
- Rate limits enforced: 3 OTPs/hour per phone, 15 OTPs/hour per IP, 60-second cooldown
- On OTP verify: subscriber account created, `auth_token` returned
- Subscriber then sets a 4-digit PIN
- Welcome notification sent to subscriber (if configured by reseller)
- Reseller and admin contacts notified of new subscriber

**FR-10: Subscriber Login**
- Phone + 4-digit PIN authentication
- Returns `auth_token` used as `X-Auth-Token` header for all subsequent portal API calls
- Token is permanent until PIN changed

**FR-11: Plan Selection & Free Access**
- Subscriber sees all active plans (name, speed, duration, data cap, price)
- Free plan: subscriber assigned immediately (no payment)
- Paid plan: subscriber initiates Paystack payment flow

**FR-12: Portal Connected Page**
- Shown after successful authentication (RADIUS Accept)
- Displays plan name, expiry, and data remaining
- Link to `/account/` for full account management

**FR-13: Subscriber Account (Self-Service)**
- Accessible at `/account/` with phone + PIN login
- Shows: current subscription, speed, expiry, data usage
- Actions: change plan, change PIN, reset PIN, disconnect all devices
- Notification preferences (alerts, promos, marketing opt-in)

### 4.4 Payment Processing

**FR-14: Payment Initialisation**
- Subscriber selects paid plan → POST `/api/portal/initiate-payment/`
- System calculates split:
  - `commission = price × commission_pct / 100` (reseller override or platform default 15%)
  - `reseller_amount = price − commission`
  - `gateway_fee` calculated based on `fee_bearer` setting
- Creates Payment record (status = pending)
- Calls Paystack to initialise transaction with subaccount split
- Returns Paystack authorisation URL

**FR-15: Paystack Checkout**
- Subscriber completes payment on Paystack (card, bank transfer, USSD)
- Paystack splits settlement:
  - Platform account receives commission
  - Reseller subaccount receives remainder (minus fee if `fee_bearer = subaccount`)

**FR-16: Payment Confirmation**
- Paystack sends webhook to `/api/billing/webhook/` (HMAC-SHA512 verified)
- On success: Payment status → `success`, Subscription created, subscriber assigned to RADIUS group
- Subscriber and reseller notified
- Browser callback page shows confirmation

**FR-17: Payment Audit Trail**
- Each Payment record snapshots `commission_pct_applied`, `fee_bearer_applied`, `platform_amount_ngn`, `reseller_amount_ngn`, `gateway_fee_ngn` at transaction time
- Immutable — historical records unaffected by config changes

### 4.5 Router Management

**FR-18: Router Registration (Operator)**
- Operator imports router serial numbers via CSV or admin UI
- Router status = `available`

**FR-19: Router Claiming (Reseller)**
- Reseller claims a router by serial number from dashboard
- System generates:
  - WireGuard keypair (Curve25519)
  - Tunnel IP (auto-allocated from 10.99.0.0/16)
  - NAS shared secret (RADIUS)
  - RouterOS API credentials
- Adds server-side WireGuard peer
- Writes NAS entry in FreeRADIUS
- Status → `pending_provision`

**FR-20: Router Provisioning**
- Router phones home and receives a RouterOS `.rsc` script
- Script configures: WireGuard tunnel, RADIUS server IP + secret, hotspot profile, API user
- After successful import: status → `provisioned` → `online`

**FR-21: Router Health Monitoring**
- Heartbeat: router POSTs every 30 seconds
- Health check command runs every 2 minutes: ICMP ping to WireGuard tunnel IP
- If unreachable or heartbeat stale >10 min: status → `offline`, `offline_since` set
- On recovery: status → `online`, `offline_since` cleared
- All transitions logged in `RouterHealthLog`
- Reseller notified of offline/recovery events (if configured)

**FR-22: Router Remote Management**
- Reseller can change SSID via dashboard (RouterOS API call)
- Adjust WiFi security settings
- View health log (online/offline history)

### 4.6 Notifications

**FR-23: Notification Channels**
- SMS (via Termii) — primary channel, always available
- WhatsApp (via Baileys/Node.js) — available if reseller has connected their number
- Per-event channel selection: SMS / WhatsApp / Both

**FR-24: Automated Subscriber Notifications**
- Plan expiry 3-day warning (`send_plan_expiry_3d`)
- Plan expiry 1-day warning (`send_plan_expiry_1d`)
- Plan expired notice (`send_plan_expired`)
- Welcome message on signup (`send_welcome`)
- Payment confirmation (when payment processed)
- Each toggle is per-reseller in `ResellerNotificationConfig`

**FR-25: Reseller Operational Alerts**
- New subscriber signup (`recv_new_subscriber`)
- Payment received (`recv_payment`)
- Router went offline (`recv_router_offline`)
- Router recovered (`recv_router_recovered`)
- Each toggle is per-reseller in `ResellerNotificationConfig`

**FR-26: Admin Contacts**
- Reseller can add multiple admin contacts (name, phone, channel)
- Per-contact toggles: router alerts, new subscriber, payment summary
- Contacts receive copies of operational events

**FR-27: Custom Message Templates**
- Reseller can override default message body per event type
- Template variables use `{{var}}` syntax (e.g., `{{name}}`, `{{plan}}`, `{{expiry_date}}`)
- 8 event types: welcome, plan_expired, expiry_3d, expiry_1d, payment_received, new_subscriber, router_offline, router_recovered
- Each template can be individually enabled/disabled

**FR-28: Broadcast Campaigns**
- Reseller composes broadcast message
- Selects type: Alert / Promo / Marketing
- Selects channel: SMS / WhatsApp / Both
- System previews recipient count (respects subscriber opt-in prefs)
- On send: broadcast queued, `process_broadcasts` command sends in batches (30 SMS/run)
- Live progress bar shown in dashboard
- Reseller can cancel a queued broadcast

**FR-29: Subscriber Opt-In**
- Subscribers can opt in/out per category:
  - Alerts (default: ON)
  - Promos (default: OFF)
  - Marketing (default: OFF)
- Opt-in preferences managed in subscriber account page
- Respected by `process_broadcasts` and `notify_subscriber()`

**FR-30: WhatsApp Session Management**
- Reseller connects WhatsApp by scanning QR code in dashboard
- QR polled every 3 seconds from Baileys Node service
- Session persisted to disk; survives service restarts
- Reseller can disconnect at any time
- One WhatsApp session per reseller (keyed by reseller slug)
- Test message feature to verify connection

**FR-31: Notification Audit Log**
- Every notification attempt logged: channel, recipient, event type, status, error
- Reseller can view last 50 log entries from dashboard

### 4.7 Reseller Branding

**FR-32: Portal Themes**
- 3 pre-built themes: Modern, Bold, Minimal
- Each theme has login, signup, connected, and account page variants
- Theme selected per reseller in branding settings

**FR-33: Visual Customisation**
- Portal title (displayed in header)
- Welcome text (sub-heading on login page)
- Logo (uploaded image file)
- Background image (uploaded image file)
- Primary colour (hex colour picker)
- All customisations applied dynamically at render time

**FR-34: Media Hosting**
- Reseller logos and background images uploaded to `/media/resellers/`
- Served at `/media/` URL
- Stored on server disk (no CDN currently)

### 4.8 Dashboard Analytics

**FR-35: Overview Metrics**
- Total active subscribers
- Total revenue (all time and this month)
- Active routers vs total routers
- Recent activity feed (new subscribers, payments, router events)

**FR-36: Subscriber Management**
- Search by phone number
- Filter by status (active, expired)
- View individual subscriber: profile, current subscription, payment history, subscription history
- Manually suspend or manage subscriber (via admin)

**FR-37: Payment History**
- List of all payments (date, subscriber, plan, amount, status)
- Breakdown: platform commission, reseller amount, gateway fee
- Filter by status (success, pending, failed)

**FR-38: Router Dashboard**
- List of all claimed routers
- Online/offline status with time since last seen
- Health log per router (online/offline history)
- Quick actions: add router, change SSID

### 4.9 Subscription Lifecycle

**FR-39: Subscription Activation**
- On plan assignment (free) or payment success (paid)
- `start_date = now()`, `expiry_date = start_date + duration`
- RADIUS entries created: `radusergroup` maps subscriber phone to plan group
- RADIUS group has all speed/data/time attributes

**FR-40: Subscription Expiry**
- `expire_plans` command runs every 5 minutes
- Finds subscriptions past `expiry_date` with status = `active`
- Sends RADIUS CoA Disconnect-Message (terminates active sessions)
- Removes from `radusergroup` (prevents new auth)
- Marks Subscription status = `expired`
- Sends expiry notification to subscriber

**FR-41: Plan Change**
- Subscriber can switch plans from account page
- Existing subscription cancelled, new one created
- RADIUS group updated (next auth uses new plan's attributes)
- Current session disconnected (reconnect required with new rates)

---

## 5. Non-Functional Requirements

### 5.1 Performance

| Metric | Target |
|--------|--------|
| Portal page load | < 500ms |
| RADIUS auth response | < 100ms |
| Payment webhook processing | < 2s |
| API p95 response time | < 300ms |
| Concurrent portal users | 40–80 (current hardware) |

### 5.2 Security

- All reseller passwords hashed (Django default PBKDF2)
- Subscriber PINs hashed with Django `make_password` (PBKDF2)
- `auth_token` is 64-byte random (URL-safe)
- Paystack webhooks verified with HMAC-SHA512
- WireGuard webhook auth with shared `WA_API_KEY`
- CSRF protection on all form views
- Rate limiting on OTP endpoints (Redis-backed)
- Reseller tokens use DRF `ResellerTokenAuthentication`
- Subscriber auth is stateless (`X-Auth-Token` header)
- No subscriber PINs transmitted in logs

### 5.3 Availability

- Single-server deployment (current)
- Redis sessions survive Gunicorn worker restarts
- Gunicorn workers recycle after 500 requests (`--max-requests 500`)
- MikroTik routers cache active sessions — authenticated users remain connected during server restarts
- No current HA/failover setup

### 5.4 Data Integrity

- All business models use `SimpleHistory` for full audit trails
- Payment amounts are snapshotted at transaction time (immutable)
- Subscriber phone normalisation applied on write
- OTP codes stored in Redis with TTL (not in DB)
- RADIUS tables shared directly with FreeRADIUS (no sync delay)

### 5.5 Scalability Limits (Current Hardware)

| Resource | Current | Bottleneck at |
|----------|---------|---------------|
| RAM | 1 GB (76% at idle) | ~5 concurrent WA sessions |
| Gunicorn workers | 2 gevent | ~40 concurrent portal users |
| PostgreSQL connections | 100 max | ~50 gunicorn workers |
| Redis | 128 MB cap (LRU) | Never (eviction configured) |

---

## 6. Background Job Schedule

| Job | Frequency | Purpose |
|-----|-----------|---------|
| `expire_plans` | Every 5 min | Mark expired subscriptions, CoA disconnect, RADIUS cleanup |
| `check_routers` | Every 2 min | Ping routers, update online/offline status, send alerts |
| `process_broadcasts` | Every 1 min | Send queued broadcast messages (batch 30 SMS/run) |
| `send_expiry_reminders` | Daily at 08:00 | 3-day and 1-day plan expiry warnings |
| `daily_summary` | Daily at 23:00 | Platform stats summary for operator |
| `cleanup_otp` | Hourly | Remove expired OTP cache keys |
| `clearsessions` | Daily at 00:00 | Django session table cleanup |

---

## 7. External Dependencies

| Service | Purpose | Failure Impact |
|---------|---------|----------------|
| Paystack | Payment processing, subaccounts | Paid plan purchases fail; existing access unaffected |
| Termii | SMS (OTP + notifications) | Signup blocked; WA fallback if reseller connected |
| Baileys/Node.js | WhatsApp messaging | WA notifications fail; SMS fallback if configured |
| FreeRADIUS | WiFi authentication | All new WiFi logins fail; existing sessions persist |
| WireGuard | Router management tunnel | Router provisioning and remote management unavailable |
| MikroTik RouterOS | Hotspot hardware | No WiFi service (hardware-level failure) |

---

## 8. Operator Panel Features

- Platform-wide view: all resellers, active subscribers, routers, total revenue
- Manage PlatformSettings (singleton): commission %, fee bearer, subscriber limits, API keys
- View per-reseller breakdown
- Router serial import (CSV/manual)
- Access to Django admin for low-level data management

---

## 9. Known Gaps & Future Considerations

| Item | Priority | Notes |
|------|----------|-------|
| Database backups | High | No automated backups currently configured |
| CDN for portal static assets | Medium | Currently served from origin |
| PgBouncer connection pooler | Medium | Required before scaling past 5 Gunicorn workers |
| Managed Postgres (DO) | Medium | Automated failover + point-in-time recovery |
| Flutterwave / Monnify payment gateways | Low | Fields exist in Reseller model, not implemented |
| 2 GB RAM droplet upgrade | High | Current server at 76% idle RAM |
| Multi-router WireGuard redundancy | Low | Single WireGuard interface on server |
| Subscriber email auth | Low | Email field exists, OTP only via phone currently |
| Data usage dashboard (live) | Medium | RADIUS accounting data available, no UI widget yet |
| Reseller mobile app | Low | Current dashboard is mobile-responsive web |
| White-label operator reselling | Low | Architecture supports it; not implemented |
