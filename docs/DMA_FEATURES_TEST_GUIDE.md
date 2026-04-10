# DMA Feature Parity — Physical Test Guide

Manual QA checklist for testing all 6 phases on real hardware. Assumes you have:
- A reseller account on the dashboard (`app.sabiwifi.com`)
- A MikroTik router connected via WireGuard tunnel
- A phone/laptop to connect to the hotspot
- Access to Django admin (`/admin/`) or Django shell for advanced fields

---

## Prerequisites

### Create test plans via Django shell

The dashboard plan form does not yet expose advanced fields (burst, caps, quotas, fallback, IP pool). Create them via shell:

```bash
cd /opt/sabiwifi && DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py shell
```

```python
from accounts.models import Reseller
from plans.models import ServicePlan

reseller = Reseller.objects.get(slug='YOUR_SLUG')

# Basic plan for voucher testing
basic = ServicePlan.objects.create(
    reseller=reseller,
    name='Voucher Test 1hr',
    download_mbps=5, upload_mbps=5,
    duration_days=0, duration_hours=1,
    price_ngn=100, is_active=True
)

# Burst plan
burst = ServicePlan.objects.create(
    reseller=reseller,
    name='Burst Plan',
    download_mbps=2, upload_mbps=2,
    burst_download_mbps=10, burst_upload_mbps=10,
    burst_threshold_download_mbps=2, burst_threshold_upload_mbps=2,
    burst_time_seconds=30,
    priority=1,
    duration_days=1, price_ngn=200, is_active=True
)

# Fallback plan (throttled speed, no expiry concerns)
fallback = ServicePlan.objects.create(
    reseller=reseller,
    name='Throttled Fallback',
    download_mbps=1, upload_mbps=1,
    duration_days=30, price_ngn=0, is_active=False  # not purchasable
)

# Plan with daily quota + fallback
daily_quota = ServicePlan.objects.create(
    reseller=reseller,
    name='Daily Quota Test',
    download_mbps=5, upload_mbps=5,
    duration_days=7, price_ngn=500,
    daily_total_mb=50,  # 50 MB daily cap
    daily_fallback_plan=fallback,
    is_active=True
)

# Plan with separate DL/UL caps
capped = ServicePlan.objects.create(
    reseller=reseller,
    name='Separate Caps',
    download_mbps=5, upload_mbps=5,
    download_cap_gb=1,  # 1 GB download cap
    upload_cap_gb=0.5,   # 500 MB upload cap
    duration_days=7, price_ngn=300, is_active=True
)

# Plan with IP pool
pooled = ServicePlan.objects.create(
    reseller=reseller,
    name='VIP Pool Plan',
    download_mbps=10, upload_mbps=10,
    ip_pool_name='vip-pool',
    duration_days=30, price_ngn=1000, is_active=True
)

# Plan with cumulative time limit + fallback
time_limited = ServicePlan.objects.create(
    reseller=reseller,
    name='10hr Time Limit',
    download_mbps=5, upload_mbps=5,
    online_time_limit_minutes=600,  # 10 hours total
    fallback_plan=fallback,
    duration_days=30, price_ngn=400, is_active=True
)

# Auto-renewable plan
renewable = ServicePlan.objects.create(
    reseller=reseller,
    name='Auto-Renew Weekly',
    download_mbps=5, upload_mbps=5,
    duration_days=7, price_ngn=500,
    allow_auto_renew=True,
    is_active=True
)
```

---

## Phase 1: Voucher / Prepaid Card System

### 1.1 Create a Voucher Batch (Dashboard)

1. Login to dashboard
2. Navigate to **Vouchers** in sidebar
3. Click **Create Batch**
4. Fill in:
   - Name: "Test Batch Jan"
   - Plan: select "Voucher Test 1hr"
   - Quantity: 10
   - PIN length: 8
   - Prefix: "SW"
   - Validity: "From Activation", 1 day
   - Simultaneous use: 1
5. Submit

**Expected**: Batch created, redirected to batch list showing "Test Batch Jan" with 10/0/10 (total/used/available)

### 1.2 View Batch Detail & Export CSV

1. Click on the batch name to view detail
2. Verify 10 vouchers listed with status "unused"
3. Note down 3 PIN codes for testing
4. Click **Export CSV**

**Expected**: CSV downloads with `id;pin` format, 10 rows

### 1.3 Voucher Login on Captive Portal

1. Connect a device to the MikroTik hotspot WiFi
2. Captive portal loads (or navigate to the hotspot login page)
3. Click the **Voucher** tab at the top of the login form
4. Enter one of the PINs from step 1.2 (include the "SW" prefix)
5. Click **Activate & Connect**

**Expected**:
- Success response, internet access granted
- Back in dashboard: batch detail shows that voucher as "active" with subscriber assigned
- Subscriber list shows a new entry with phone = the voucher PIN (e.g. "SW12345678"), marked as voucher user

### 1.4 Voucher Re-login

1. Disconnect from WiFi, reconnect
2. Use the same voucher PIN again

**Expected**: Re-authenticated successfully, same subscriber, internet restored

### 1.5 Voucher Expiry

1. In Django shell, set a voucher's `expires_at` to the past:
   ```python
   from vouchers.models import Voucher
   from django.utils import timezone
   v = Voucher.objects.get(pin='SW........')
   v.expires_at = timezone.now() - timezone.timedelta(hours=1)
   v.save()
   ```
2. Run: `venv/bin/python manage.py expire_vouchers`

**Expected**: Voucher marked "expired", subscriber's subscription expired, RADIUS cleaned up, device disconnected

### 1.6 Disabled Batch

1. In dashboard, go to Vouchers list
2. Click **Disable** on the test batch
3. Try to login with an unused PIN from that batch on the captive portal

**Expected**: Error "This voucher batch has been disabled."

### 1.7 Re-enable Batch

1. Click **Enable** on the batch
2. Login with the unused PIN

**Expected**: Works again

---

## Phase 2: Wallet & Refill Cards

### 2.1 Create a Refill Card Batch (Dashboard)

1. Navigate to **Refill Cards** in sidebar
2. Click **Create Batch**
3. Fill in:
   - Name: "500 Naira Cards"
   - Value: 500
   - Quantity: 5
   - PIN length: 10
   - Prefix: "RC"
4. Submit

**Expected**: Batch created, 5 cards generated

### 2.2 Export Refill Card CSV

1. Click export CSV on the batch

**Expected**: CSV with `id;pin;value` format, 5 rows

### 2.3 Redeem Refill Card (API)

First, you need an authenticated subscriber. Use a phone subscriber or a voucher subscriber.

```bash
# Using a subscriber's auth_token
curl -X POST https://app.sabiwifi.com/api/portal/redeem-refill/ \
  -H "X-Auth-Token: SUBSCRIBER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pin": "RC1234567890"}'
```

**Expected**: Response `{"message": "500.00 NGN added to wallet.", "balance": "500.00"}`

### 2.4 Check Wallet Balance (API)

```bash
curl https://app.sabiwifi.com/api/portal/wallet/ \
  -H "X-Auth-Token: SUBSCRIBER_AUTH_TOKEN"
```

**Expected**: `{"balance": "500.00", "transactions": [{"amount": "500.00", "type": "refill_card", ...}]}`

### 2.5 Purchase Plan from Wallet (API)

```bash
curl -X POST https://app.sabiwifi.com/api/portal/wallet/purchase/ \
  -H "X-Auth-Token: SUBSCRIBER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plan_id": PLAN_ID}'
```

**Expected**: Plan activated, wallet debited, subscription created. Verify in dashboard subscriber detail.

### 2.6 Insufficient Balance

```bash
# Try to buy a plan costing more than the remaining balance
curl -X POST https://app.sabiwifi.com/api/portal/wallet/purchase/ \
  -H "X-Auth-Token: SUBSCRIBER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plan_id": EXPENSIVE_PLAN_ID}'
```

**Expected**: `{"error": "Insufficient wallet balance."}`

### 2.7 Redeem Used Card

```bash
# Re-use the same refill PIN
curl -X POST https://app.sabiwifi.com/api/portal/redeem-refill/ \
  -H "X-Auth-Token: SUBSCRIBER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pin": "RC1234567890"}'
```

**Expected**: `{"error": "This refill card has already been used."}`

### 2.8 Auto-Renewal

1. Create a subscriber with auto-renew enabled:
   ```python
   from accounts.models import Subscriber
   sub = Subscriber.objects.get(phone='08012345678')
   sub.auto_renew_enabled = True
   sub.save()
   ```

2. Give the subscriber wallet balance >= plan price:
   ```python
   from billing.wallet import credit_wallet
   credit_wallet(sub, 500, 'reseller_credit', 'test', 'Test top-up')
   ```

3. Assign subscriber to the "Auto-Renew Weekly" plan and set subscription to expire now:
   ```python
   from plans.models import Subscription
   from django.utils import timezone
   sub_plan = Subscription.objects.filter(subscriber=sub, status='active').first()
   sub_plan.expiry_date = timezone.now() - timezone.timedelta(minutes=1)
   sub_plan.save()
   ```

4. Run: `venv/bin/python manage.py expire_plans`

**Expected**:
- Old subscription marked "expired"
- New subscription created (same plan, new dates)
- Wallet debited by plan price
- Output includes "auto-renewed" message

### 2.9 Auto-Renewal Failure (Insufficient Balance)

1. Set wallet balance to 0:
   ```python
   from billing.wallet import get_or_create_wallet
   w = get_or_create_wallet(sub)
   w.balance_ngn = 0
   w.save()
   ```
2. Set subscription to expire, run `expire_plans`

**Expected**: Subscription expires normally (no renewal), "auto_renewal_failed" notification sent

---

## Phase 3: Enhanced Service Plans

### 3.1 Burst Speed

1. Assign a device to the "Burst Plan" (2 Mbps base, 10 Mbps burst for 30 seconds)
2. Connect to hotspot, authenticate
3. Run a speed test immediately (within 30 seconds)
4. Wait 30+ seconds, run speed test again

**Verify on MikroTik** (Winbox or terminal):
```
/queue simple print
```
Look for the queue entry — it should show burst parameters.

**Expected**:
- First speed test: ~10 Mbps (burst speed)
- Second speed test: ~2 Mbps (base speed after burst expires)

**Verify RADIUS attribute** (Django shell):
```python
from radius.models import RadGroupReply
RadGroupReply.objects.filter(
    groupname__contains='burst',
    attribute='Mikrotik-Rate-Limit'
).values_list('value', flat=True)
```
Should show: `2048k/2048k 10240k/10240k 2048k/2048k 30/30 1`

### 3.2 Separate Download/Upload Caps

1. Assign a device to the "Separate Caps" plan (1 GB DL, 500 MB UL)
2. Verify RADIUS attributes:
   ```python
   RadGroupReply.objects.filter(
       groupname__contains='separate',
       attribute__in=['Mikrotik-Recv-Limit', 'Mikrotik-Xmit-Limit']
   ).values_list('attribute', 'value')
   ```
   **Expected**: `Mikrotik-Recv-Limit` = 1073741824, `Mikrotik-Xmit-Limit` = 536870912

3. Download a large file — after ~1 GB, MikroTik should disconnect the session
4. Upload should still work if under 500 MB

### 3.3 IP Pool Assignment

1. On MikroTik, create the IP pool first:
   ```
   /ip pool add name=vip-pool ranges=10.8.100.1-10.8.100.254
   ```
2. Assign a device to the "VIP Pool Plan"
3. Check RADIUS:
   ```python
   RadGroupReply.objects.filter(
       groupname__contains='vip',
       attribute='Mikrotik-Address-Pool'
   ).values_list('value', flat=True)
   ```
   **Expected**: `vip-pool`
4. Connect — device should get an IP from 10.8.100.x range

### 3.4 Cumulative Time Limit + Fallback

1. Assign a device to the "10hr Time Limit" plan
2. In Django shell, simulate accumulated time by checking radacct:
   ```python
   # To speed up testing, manually set a short time limit
   from plans.models import ServicePlan
   plan = ServicePlan.objects.get(name='10hr Time Limit')
   plan.online_time_limit_minutes = 2  # 2 minutes for testing
   plan.save()
   ```
3. Connect and stay online for >2 minutes
4. Run: `venv/bin/python manage.py check_usage`

**Expected**: Subscriber switched to "Throttled Fallback" plan (1 Mbps), speed drops

5. Reset the time limit back to 600 after testing.

### 3.5 Fallback Plan (Data Cap Exceeded)

1. Create a plan with a small data cap + fallback:
   ```python
   ServicePlan.objects.create(
       reseller=reseller, name='Tiny Cap Test',
       download_mbps=5, upload_mbps=5,
       data_cap_gb=0.01,  # 10 MB
       fallback_plan=fallback,
       duration_days=1, price_ngn=50, is_active=True
   )
   ```
2. Assign to subscriber, connect, download >10 MB
3. MikroTik should disconnect the session (Mikrotik-Total-Limit)
4. On re-auth, if fallback is configured, they get the fallback plan speed

### 3.6 PPPoE Provisioning

1. Set a router to PPPoE mode:
   ```python
   from routers.models import Router
   r = Router.objects.get(name='YOUR_ROUTER')
   r.service_mode = 'both'
   r.save()
   ```

2. Generate and inspect the provision script:
   ```python
   from routers.provision import generate_provision_rsc
   script = generate_provision_rsc(r)
   print(script)
   ```

3. **Verify the script contains**:
   - `/radius add service=hotspot,ppp ...` (not just `hotspot`)
   - PPPoE server block: `/interface pppoe-server server add ...`
   - PPP profile: `/ppp profile set default use-radius=yes ...`
   - PPPoE IP pool: `/ip pool add name=pppoe-pool ranges=10.9.0.2-10.9.255.254`

4. Apply the provision script to the router (paste in Winbox terminal or SSH)

5. Configure a PPPoE client on a test device:
   - Username: subscriber's phone number
   - Password: subscriber's auth_token
   
6. Connect via PPPoE

**Expected**: PPPoE connection established, subscriber gets internet, RADIUS accounting records appear in radacct

7. Check speed is limited per the assigned plan

### 3.7 Hotspot-only Router (no PPPoE)

1. Set router to `service_mode='hotspot'`
2. Generate provision script
3. **Verify**: RADIUS service line says `service=hotspot` only, no PPPoE block

---

## Phase 4: Daily Quotas

### 4.1 Daily Quota Enforcement

1. Assign a device to the "Daily Quota Test" plan (50 MB daily)
2. Connect and download >50 MB of data
3. Run: `venv/bin/python manage.py check_usage`

**Expected**:
- DailyUsageSnapshot created with `quota_exceeded=True`
- Subscriber switched to "Throttled Fallback" plan
- Speed drops to 1 Mbps

### 4.2 Daily Quota Reset

1. After quota exceeded, run: `venv/bin/python manage.py reset_daily_quotas`

**Expected**:
- Subscriber restored to original "Daily Quota Test" plan
- Full speed (5 Mbps) restored
- DailyUsageSnapshot `quota_exceeded` reset to False

### 4.3 Daily Quota with No Fallback

1. Create a plan with daily quota but NO fallback:
   ```python
   ServicePlan.objects.create(
       reseller=reseller, name='No Fallback Daily',
       download_mbps=5, upload_mbps=5,
       daily_total_mb=50,
       daily_fallback_plan=None,  # no fallback
       duration_days=7, price_ngn=300, is_active=True
   )
   ```
2. Exceed quota, run `check_usage`

**Expected**: Subscriber disconnected entirely (no fallback = hard cutoff)

---

## Phase 5: Online Users & Session Management

### 5.1 Online Users Page

1. Have one or more devices connected to the hotspot
2. In dashboard, navigate to **Online Users**

**Expected**: Table shows connected users with:
- Username (phone number or voucher PIN)
- Router name
- Session time
- Data used (DL/UL)
- IP address

### 5.2 Auto-Refresh

1. Stay on the Online Users page
2. Connect a new device to the hotspot
3. Wait ~30 seconds

**Expected**: New user appears without manual page refresh

### 5.3 Disconnect a User

1. On the Online Users page, click **Disconnect** next to a connected user
2. Confirm the action

**Expected**:
- User disappears from online users list
- Device loses internet access immediately
- User must re-authenticate on the captive portal to reconnect

---

## Phase 6: Reports & Analytics

### 6.1 Traffic Report

1. Navigate to **Reports** > **Traffic** tab
2. Select different time ranges (7, 30, 90 days)

**Expected**: Table shows per-subscriber: username, download, upload, total data, time online, session count

### 6.2 Traffic CSV Export

1. Click **Export CSV** on the traffic report

**Expected**: CSV downloads with columns matching the table

### 6.3 Session Report

1. Navigate to **Reports** > **Sessions** tab
2. Enter a phone number in the filter field

**Expected**: Session history table showing: user, router, start time, duration, data used, disconnect reason

### 6.4 Financial Report

1. Navigate to **Reports** > **Financial** tab

**Expected**:
- Summary cards: Total Revenue, Your Earnings
- Revenue by Plan breakdown
- By Payment Method breakdown (paystack, wallet, refill_card)
- Recent payments table

### 6.5 Financial CSV Export

1. Click **Export CSV** on the financial report

**Expected**: CSV downloads with payment records

### 6.6 Nightly Aggregation (Simulated)

1. Run: `venv/bin/python manage.py aggregate_daily_traffic`

**Expected**: Output like "Aggregated 2026-04-07: X created, Y updated."
Verify in Django admin: DailyUsageSnapshot records created for yesterday's active subscribers.

---

## Cross-Cutting: RADIUS Attribute Verification

For any plan, verify the full attribute set written to FreeRADIUS:

```python
from radius.models import RadGroupReply
group = 'RESELLER_SLUG-PLAN_SLUG'
for attr in RadGroupReply.objects.filter(groupname=group):
    print(f'{attr.attribute} = {attr.value}')
```

### Expected attributes by plan type

| Plan Feature | Attribute | Example Value |
|---|---|---|
| Base speed | `Mikrotik-Rate-Limit` | `5120k/5120k` |
| Burst speed | `Mikrotik-Rate-Limit` | `5120k/5120k 10240k/10240k 5120k/5120k 30/30 1` |
| Session timeout | `Session-Timeout` | `86400` |
| Total data cap | `Mikrotik-Total-Limit` | `1073741824` |
| Download cap | `Mikrotik-Recv-Limit` | `1073741824` |
| Upload cap | `Mikrotik-Xmit-Limit` | `536870912` |
| Max devices | `Simultaneous-Use` | `1` |
| Acct interval | `Acct-Interim-Interval` | `300` |
| IP pool | `Mikrotik-Address-Pool` | `vip-pool` |

---

## Cron Job Verification

Confirm all management commands are registered and work:

```bash
cd /opt/sabiwifi
DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py expire_plans
DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py expire_vouchers
DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py check_usage
DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py reset_daily_quotas
DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py aggregate_daily_traffic
```

All should run without errors. Add to `/etc/cron.d/sabiwifi`:

```
*/5 * * * * root cd /opt/sabiwifi && DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py expire_vouchers >> /var/log/sabiwifi-cron.log 2>&1
*/5 * * * * root cd /opt/sabiwifi && DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py check_usage >> /var/log/sabiwifi-cron.log 2>&1
0 0 * * * root cd /opt/sabiwifi && DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py reset_daily_quotas >> /var/log/sabiwifi-cron.log 2>&1
0 2 * * * root cd /opt/sabiwifi && DJANGO_SETTINGS_MODULE=config.settings.prod venv/bin/python manage.py aggregate_daily_traffic >> /var/log/sabiwifi-cron.log 2>&1
```

---

## Quick Smoke Test Checklist

Run through this minimal path to verify the core flow end-to-end:

- [ ] Create voucher batch in dashboard (10 vouchers, prefix "SW")
- [ ] Export CSV, note a PIN
- [ ] Connect to hotspot WiFi, open captive portal
- [ ] Switch to Voucher tab, enter PIN, click Activate
- [ ] Internet works, speed test matches plan
- [ ] Dashboard shows voucher as "active"
- [ ] Online Users page shows the connected device
- [ ] Disconnect the user from dashboard — device loses internet
- [ ] Create refill card batch (500 NGN cards)
- [ ] Redeem a refill card via API — wallet credited
- [ ] Purchase a plan from wallet via API — plan activated
- [ ] Traffic report shows data for the connected subscriber
- [ ] Financial report shows the wallet payment
- [ ] Run `expire_vouchers` — expired vouchers cleaned up
- [ ] Run `check_usage` — no errors on live data

---

## Known Limitations

1. **Advanced plan fields not in dashboard UI**: Burst, separate caps, IP pool, daily quotas, fallback plan, and cumulative time limit must be configured via Django admin (`/admin/plans/serviceplan/`) or Django shell. The dashboard plan form only exposes basic fields (name, speed, duration, data cap, price, max devices).

2. **Router service_mode not in dashboard UI**: PPPoE mode must be set via Django admin (`/admin/routers/router/`) or Django shell.

3. **Wallet/refill card UI not in portal templates**: The redeem-refill, wallet info, and wallet purchase endpoints are API-only. The portal HTML templates don't have wallet UI components yet — test via curl/API calls.

4. **Daily quota check is cron-based**: Quotas are enforced every 5 minutes via `check_usage`, not in real-time. A subscriber could exceed their quota between checks.

5. **PPPoE testing requires a PPPoE client**: Unlike hotspot (browser-based), PPPoE requires configuring a PPPoE client on the test device (Windows/Linux PPPoE dialer, or another router as a PPPoE client).
