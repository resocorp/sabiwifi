# SabiWiFi — UX/UI Design Document

**Version**: 1.0
**Date**: March 2026
**Status**: Based on Deployed System

---

## 1. Design Philosophy

SabiWiFi serves two very different user groups with fundamentally different contexts:

1. **Resellers** — operators managing a business. They use the dashboard on desktop in a comfortable setting. They need clarity, density, and control.
2. **Subscribers** — end users in a public space (hostel corridor, hotel lobby, café) who just want to get online quickly. They're on mobile, likely impatient. They need speed and simplicity.

**Design principles derived from this context**:

- **Subscribers first, mobile first**: The captive portal must work on a 360px screen in 30 seconds. Every extra tap is a dropout.
- **Resellers: clarity over density**: Dashboard pages show one thing at a time. No modals stacked on modals.
- **Branding-aware**: The portal renders reseller colours/logos — the design system must accommodate arbitrary primary colours gracefully.
- **Offline-tolerant feedback**: Users on unstable connections need clear loading, error, and retry states.
- **Low literacy tolerance in portal**: Subscribers may have low reading comfort. Keep copy short, direct, and action-oriented.

---

## 2. Design System

### 2.1 CSS Framework

Tailwind CSS (utility classes, compiled via CDN in current deployment). No custom build step. Component patterns are defined by recurring utility class combinations in templates.

### 2.2 Typography

| Role | Usage |
|------|-------|
| `font-heading` | Headlines, section titles, nav items, plan names |
| `font-sans` (default) | Body text, form labels, data cells |
| `text-sm` (14px) | Most body text in dashboard |
| `text-xs` (12px) | Labels, captions, metadata |
| `text-lg / text-xl` | Page headings |
| `font-bold / font-semibold` | Emphasis, CTAs, stat values |
| `tracking-wider + uppercase` | Section headers (e.g., "NEW BROADCAST", "HISTORY") |

### 2.3 Colour Tokens

| Token | Class | Default Hex | Usage |
|-------|-------|-------------|-------|
| Primary | `text-primary`, `bg-primary`, `border-primary` | Reseller-defined | CTAs, active nav, highlights |
| Primary dark | `bg-primary-dark` | Darker shade | Button hover states |
| Primary muted | `bg-primary/10`, `bg-primary/5` | 5–10% opacity | Active nav backgrounds, preview bg |
| Text secondary | `text-text-secondary` | #6b7280 (gray-500) | Labels, metadata, help text |
| Border | `border-border` | #e5e7eb (gray-200) | Card borders, dividers, inputs |
| Danger | `text-danger`, `bg-danger` | #ef4444 (red-500) | Errors, destructive actions |
| Success | `text-green-700`, `bg-green-50` | Green tones | Success states, active badges |
| Warning | `text-amber-800`, `bg-amber-50` | Amber tones | Incomplete setup banners |

**Reseller-defined primary colour** is injected as a CSS custom property into portal templates:
```html
<style>:root { --color-primary: {{ reseller.branding.primary_color|default:'#0ea5e9' }}; }</style>
```

All `primary` tokens resolve to this variable, so the entire portal adapts to the reseller's brand colour.

### 2.4 Spacing & Layout

- **Card pattern**: `bg-white rounded-xl border border-border p-5` — used for every content section
- **Page container**: `max-w-2xl` on forms/single-column; `max-w-4xl` on lists
- **Section spacing**: `space-y-5` between cards, `mb-4` between form fields
- **Grid**: 2-column (`grid-cols-2`) for stat pairs; 3-column (`grid-cols-3`) for type selectors
- **Gap**: `gap-3` standard; `gap-2` tight (badges, icon+text combos)

### 2.5 Interactive States

- **Button hover**: `hover:bg-primary-dark` (primary) / `hover:bg-gray-50` (ghost)
- **Disabled**: `disabled:opacity-40 disabled:cursor-not-allowed`
- **Focus ring**: `focus:outline-none focus:ring-2 focus:ring-primary/30`
- **Checked state** (radio cards): `peer-checked:border-primary peer-checked:bg-primary/5`
- **Loading spinner**: `animate-spin rounded-full h-5 w-5 border-2 border-primary border-t-transparent`
- **Transitions**: `transition` on all interactive elements (150ms Tailwind default)

### 2.6 Icons

Emoji used throughout as inline icons. No icon library dependency. This keeps the bundle zero and renders consistently:
- `📊` Overview / analytics
- `📋` Plans
- `👥` Subscribers
- `💳` Payments
- `📡` Routers
- `📢` Broadcasts
- `⚙️` Settings
- `🚪` Logout
- `🔔` Alert broadcast type
- `🎁` Promo broadcast type
- `📣` Marketing broadcast type
- `💰` Bank account setup prompt
- `✓` Success / completed steps

---

## 3. Reseller Dashboard

### 3.1 Layout

**Structure**:
```
┌─────────────────────────────────────────────────────────┐
│ Desktop Sidebar (md:w-56, fixed)  │  Main Content Area  │
│ ─────────────────────────────     │  (ml-56, p-4 md:p-6) │
│  SabiWiFi logo                    │                      │
│  Nav: Overview, Plans,            │  [Bank Banner]       │
│       Subscribers, Payments,      │  [Django Messages]   │
│       Routers, Broadcasts,        │  [Page Content]      │
│       Settings                    │                      │
│  [Logout]                         │                      │
└─────────────────────────────────────────────────────────┘

Mobile (< md):
┌──────────────────────────┐
│  [Page Content]          │
│                          │
│  pb-20 (safe area)       │
└──────────────────────────┘
┌──────────────────────────┐  ← fixed bottom
│  📊 📋 👥 💳 ⚙️          │
│  Home Plans Subs Pay Set │
└──────────────────────────┘
```

**Desktop sidebar**: Fixed 56-unit wide, white, right border. Active nav item highlighted with `bg-primary/10 text-primary`. Inactive items are `text-text-secondary`.

**Mobile bottom tab bar**: 5 most important tabs (Overview, Plans, Subscribers, Payments, Settings). Routers and Broadcasts are desktop-only in the bottom bar (accessible from sidebar on desktop). Active tab uses `text-primary`.

**Bank banner**: Amber alert strip below main header. Shown when `reseller.payment_verified = False`. Dismissible with ×. Links to Settings.

### 3.2 Overview Page

**New Reseller State (Getting Started)**:
Shown when reseller has no active subscribers yet. Displays a step-by-step checklist:

```
┌─────────────────────────────────┐
│  Getting Started                │
│  ─────────────────────────────  │
│  ✓  Create your first plan      │  ← green if done
│  →  Claim your router           │  ← highlighted if next
│  ○  Add your bank account       │
│  ○  Your portal is live at...   │
└─────────────────────────────────┘
```

Each step shows: icon, title, description, action button. Completed steps show a green checkmark. The next incomplete step is subtly highlighted.

**Active Reseller State**:
4 stat cards in a 2×2 grid:
- Total Subscribers (count)
- Monthly Revenue (₦ formatted)
- Active Routers (count / total)
- Total Revenue (all-time ₦)

Below stats: Recent Activity feed (last 10 events — signups, payments, router status changes).

### 3.3 Plans Page

**Layout**: Single-column list of plan cards. Each card shows:
```
┌─────────────────────────────────────────────┐
│  Plan Name              [Active] [Edit]     │
│  ─────────────────────────────────────────  │
│  ₦500 · 30 days · 5 Mbps ↓ / 2 Mbps ↑    │
│  Data: 10 GB · Max devices: 2               │
│  12 active subscribers                       │
└─────────────────────────────────────────────┘
```

Badge colours: `Active` = green, `Disabled` = gray, `Free` = blue, `Trial` = yellow.

Inline edit/disable buttons. "+ New Plan" button top right.

**Plan Form (Create / Edit)**:
Card with labelled form fields in vertical stack:
- Plan name (text input)
- Price (number input with ₦ prefix)
- Speed section: two side-by-side inputs (Download / Upload Mbps)
- Duration: radio toggle between "Days" and "Hours" with number input
- Data cap: number input with "Unlimited" toggle
- Max devices: number input (default 1)
- Trial plan: toggle
- Active: toggle

Validation errors shown inline below each field. Submit button full-width at bottom.

### 3.4 Subscribers Page

**Layout**: Search bar at top. Paginated table below.

Table columns: Phone | Plan | Status | Joined | Actions

Status badges: `Active` (green), `Expired` (gray), `No Plan` (yellow).

**Subscriber Detail Page**:
Two-card layout:
1. Profile card: phone, email, join date, current subscription details (plan name, expiry, speed, data cap)
2. History card: timeline of subscriptions + payments

### 3.5 Payments Page

Stats bar at top: Total Revenue (₦), Successful payments (count), Pending (count), Failed (count).

Table: Date | Subscriber | Plan | Amount | Status | Split details (collapsed, expand on click).

Status badges: `success` (green), `pending` (yellow), `failed` (red).

### 3.6 Routers Page

**Router cards** in vertical list:
```
┌─────────────────────────────────────────┐
│  🟢 Router-A2F3  ·  Ikeja Lagos        │
│  Last seen: 2 mins ago                  │
│  Serial: HM20B2HFCY2                    │
│  [Health Log]  [Change SSID]            │
└─────────────────────────────────────────┘
```
Offline routers show 🔴 and `Offline for 2h 30m`.

"+ Add Router" button triggers a serial-number input form.

### 3.7 Broadcasts Page

**Compose section** (card):

Type selector — 3 radio cards in a grid:
```
┌──────────┐ ┌──────────┐ ┌──────────┐
│  🔔      │ │  🎁      │ │  📣      │
│  Alert   │ │  Promo   │ │ Marketing│
│ Service  │ │ Special  │ │ General  │
│  notice  │ │  offer   │ │          │
└──────────┘ └──────────┘ └──────────┘
```
Active card has primary colour border and tinted background.

Channel dropdown: SMS / WhatsApp / WhatsApp + SMS (WhatsApp options only shown if WA connected).

Message textarea: 500 char max. Live character counter. Variable hint (`{{name}}`).

Preview section (shown after Preview button): Recipient count + rendered sample message.

Send button disabled until Preview clicked.

**History section** (card below): List of past broadcasts with status badge, progress bar (for sending), sent/total count, cancel button (for queued).

### 3.8 Settings Page

Settings page is organised into visually distinct sections separated by headings:

**1. Portal Branding**
- Template selector (3 themed preview cards: Modern / Bold / Minimal)
- Portal title input
- Welcome text input
- Primary colour picker (`<input type="color">`)
- Logo upload (image, shows preview)
- Background image upload (shows preview)
- "Preview Portal →" link

**2. WhatsApp Connection**
- Status indicator: Connected (phone number shown) / Connecting (QR shown) / Disconnected
- QR code displayed as `<img src="data:image/png;base64,...">` — auto-refreshes every 3s while connecting
- "Connect WhatsApp" / "Disconnect" buttons
- Test message input (shows after connected)

**3. Notification Settings — To Subscribers**
Toggle row for each event:
```
[✓] Send plan expiry warning (3 days before)
[✓] Send plan expiry warning (1 day before)
[✓] Send plan expired notice
[✓] Send welcome message
        Channel: [SMS ▾]
```

**4. Notification Settings — Operational Alerts to Me**
Toggle row for each event:
```
[✓] New subscriber signup
[✓] Payment received
[✓] Router goes offline
[✓] Router comes back online
        Channel: [WhatsApp + SMS ▾]
```

**5. Admin Contacts**
List of existing contacts with delete button. "Add Contact" inline form: name, phone, channel, toggles.

**6. Message Templates**
Accordion or card per event type. Each shows:
- Event type label
- Textarea with `{{var}}` hints
- Enable/disable toggle
- Variable reference (small text: `Available: {{name}}, {{plan}}, {{expiry_date}}`)

**Save button** (primary, full-width) at bottom of page.

---

## 4. Captive Portal

### 4.1 Portal Context & Constraints

The captive portal operates under unique constraints:
- Loaded by MikroTik's HTTP proxy (no HTTPS initially)
- Must function on 2G/3G connections (low bandwidth)
- Must work on 5-year-old Android phones (Chrome 80+, Safari 14+)
- Target completion: under 60 seconds from first page load to connected
- No server-side session — all state in `auth_token` + localStorage

### 4.2 Theme Variants

Three pre-built themes share the same functional flow but differ in visual character:

| Theme | Character | Typography | Layout |
|-------|-----------|------------|--------|
| **Modern** | Clean, card-based, rounded | Medium weight, balanced | Centered card on white/image bg |
| **Bold** | High contrast, large type | Heavy weight, dramatic | Full-bleed, prominent CTA |
| **Minimal** | Stripped back, text-forward | Light weight, spacious | Minimal chrome, form-focused |

All themes:
- Apply reseller's primary colour
- Show reseller logo (if set)
- Show portal title and welcome text
- Use the same API endpoints

### 4.3 Login Page

**Purpose**: Returning subscribers authenticate. New users find the signup path.

**Layout (Modern theme example)**:
```
┌────────────────────────────────┐
│  [Logo]                        │
│  Welcome to {portal_title}     │  ← welcome text
│  ────────────────────────────  │
│                                │
│  ┌──────────────────────────┐  │
│  │  📱  Phone Number        │  │  ← E.164 hint
│  └──────────────────────────┘  │
│  ┌──────────────────────────┐  │
│  │  🔑  PIN                 │  │  ← type="tel" or type="password"
│  └──────────────────────────┘  │
│                                │
│  [  Connect  ]                 │  ← primary button, full width
│                                │
│  Don't have an account?        │
│  [Sign up]                     │  ← secondary link
└────────────────────────────────┘
```

**UX notes**:
- Phone input: `type="tel"` with `inputmode="numeric"` for numeric keyboard on mobile
- PIN input: `type="password"` with `inputmode="numeric"`, max 4 chars
- "Connect" button shows spinner while request in flight; disabled to prevent double-tap
- Error shown inline below form (not as browser alert)
- After login: POST to `/api/portal/login/`, on success redirect to `/portal/connected/`

### 4.4 Signup Page

**Purpose**: New subscriber creates account via OTP.

**Flow — 3 steps, single page (state-machine)**:

**Step 1: Enter Phone**
```
┌────────────────────────────────┐
│  Create Account                │
│  ────────────────────────────  │
│  ┌──────────────────────────┐  │
│  │  📱  Phone Number        │  │
│  └──────────────────────────┘  │
│  ┌──────────────────────────┐  │
│  │  ✉️  Email (optional)    │  │
│  └──────────────────────────┘  │
│                                │
│  [  Send Code  ]               │
│  Already have account? Login   │
└────────────────────────────────┘
```

**Step 2: Enter OTP**
```
┌────────────────────────────────┐
│  Enter the code sent to        │
│  0812 345 ****                 │  ← masked phone
│  ────────────────────────────  │
│  ┌──┐ ┌──┐ ┌──┐ ┌──┐          │  ← 4 auto-advancing digit inputs
│  │  │ │  │ │  │ │  │          │
│  └──┘ └──┘ └──┘ └──┘          │
│                                │
│  Resend code (00:45)           │  ← countdown, becomes link on expire
└────────────────────────────────┘
```
OTP inputs: each is `type="tel"` maxlength=1 with auto-focus-next on keyup. Backspace moves focus back. Auto-submits on 4th digit.

**Step 3: Set PIN**
```
┌────────────────────────────────┐
│  Set your PIN                  │
│  This is how you log in        │
│  ────────────────────────────  │
│  ┌──────────────────────────┐  │
│  │  PIN (4 digits)          │  │
│  └──────────────────────────┘  │
│  ┌──────────────────────────┐  │
│  │  Confirm PIN             │  │
│  └──────────────────────────┘  │
│                                │
│  [  Create Account  ]          │
└────────────────────────────────┘
```

Step transitions are smooth (CSS class toggle showing/hiding sections, no page reload). Progress is implicit — users don't see a step counter (reduces cognitive load).

### 4.5 Connected Page

Shown after successful RADIUS authentication.

```
┌────────────────────────────────┐
│  [Logo]                        │
│                                │
│  ✅  You're Connected!         │
│                                │
│  Plan: {plan_name}             │
│  Speed: {download} Mbps ↓      │
│  Expires: {expiry_date}        │
│  Data: {remaining} remaining   │
│                                │
│  [  Manage Account  ]          │  ← links to /account/
│  [  Browse Plans   ]           │  ← links to plan list
└────────────────────────────────┘
```

Auto-redirects to internet after 5 seconds (or user clicks "Done").

### 4.6 Subscriber Account Page (`/account/`)

**Authentication gate**: Phone + PIN form (same as portal login, but reseller-agnostic).

**Authenticated state — sections**:

**1. Current Plan**
```
┌────────────────────────────────────┐
│  Active Plan                       │
│  ────────────────────────────────  │
│  Fiber Basic  ·  [Active]          │
│  5 Mbps ↓ / 2 Mbps ↑             │
│  Data: 8.3 GB remaining           │
│  Expires: 28 Apr 2026 (6 days)    │
│                                    │
│  [Change Plan]  [Disconnect All]   │
└────────────────────────────────────┘
```

**2. Available Plans** (shown when Change Plan tapped)
List of plan cards with price, speed, duration. "Get Plan" triggers payment or free assignment.

**3. Security**
- Change PIN (old PIN + new PIN + confirm)
- Reset PIN (sends OTP)

**4. Notification Preferences**
```
┌────────────────────────────────────┐
│  Notification Preferences          │
│  ────────────────────────────────  │
│  [✓] Service Alerts                │
│      Plan expiry reminders         │
│                                    │
│  [ ] Promotions                    │
│      Special offers & discounts    │
│                                    │
│  [ ] Marketing                     │
│      General updates & news        │
│                                    │
│               [  Save  ]           │
└────────────────────────────────────┘
```

---

## 5. Operator Panel

Minimal, functional. Designed for internal staff use only — no branding customisation.

**Overview page**:
- Platform-wide stats: total resellers, total subscribers, total revenue, total routers
- Per-reseller table: name, subscribers, revenue, router count, status
- Filter by reseller status (setup / active / suspended)
- Link to Django admin for detailed management

---

## 6. User Flows

### 6.1 Subscriber: First-Time Connection

```
Router WiFi Connected
        ↓
MikroTik redirects to /portal/?r=<slug>
        ↓
Login page loads (reseller branding applied)
        ↓
User taps "Sign up"
        ↓
Enters phone number → [Send Code]
        ↓  (OTP SMS or WA arrives in ~5s)
Enters 4-digit OTP (auto-advances digits)
        ↓
Sets 4-digit PIN
        ↓
Account created → auth_token stored
        ↓
Plan list shown
        ↓
Selects plan:
  Free → Assigned immediately
  Paid → Paystack checkout → Payment webhook → Subscription created
        ↓
/portal/connected/ — "You're Connected"
        ↓
Browsing internet (< 60 seconds total)
```

### 6.2 Subscriber: Returning Login

```
Opens browser → MikroTik redirects to portal
        ↓
Enters phone + 4-digit PIN
        ↓
POST /api/portal/login/ → auth_token
        ↓
MikroTik Hotspot login triggers RADIUS auth
        ↓
RADIUS returns: rate limit, session timeout
        ↓
/portal/connected/ → internet
(Total: ~10 seconds)
```

### 6.3 Subscriber: Plan Expiry Notification

```
send_expiry_reminders runs at 08:00
        ↓
Finds subs expiring in 0-24h
        ↓
Checks: reseller has send_plan_expiry_1d = True
        ↓
Checks: subscriber has transactional_enabled = True
        ↓
Renders template: "Hi {{name}}, your {{plan}} expires at {{expiry_date}}..."
        ↓
Sends via configured channel (SMS / WA / Both)
        ↓
Logs to NotificationLog
        ↓
Caches key (no duplicate within 24h)
```

### 6.4 Reseller: Connect WhatsApp

```
Dashboard → Settings
        ↓
Scrolls to "WhatsApp Connection"
        ↓
Status: Disconnected
Taps "Connect WhatsApp"
        ↓
POST /api/notifications/wa/connect/
→ Node service starts session
→ Status = "connecting"
        ↓
Dashboard polls /api/notifications/wa/status/ every 3s
→ Response includes base64 QR image
        ↓
Reseller scans QR with WhatsApp (on another phone)
        ↓
Node service detects auth
→ POSTs webhook to Django: { event: "connected", phone: "2348012345678" }
→ Django updates WhatsappSession: status = connected, wa_phone = "2348012345678"
        ↓
Dashboard poll returns: status = connected, phone shown
→ QR disappears, phone number displayed
→ Test message input shown
```

### 6.5 Reseller: Send Broadcast

```
Dashboard → Broadcasts
        ↓
Selects type: Promo
Selects channel: SMS
Writes message: "Hi {{name}}, 50% off this weekend only!"
        ↓
Taps "Preview"
→ POST /api/notifications/broadcast/create/ { _preview: true }
→ Server counts eligible subscribers (opt-in to promos)
→ Returns count, creates draft, then draft immediately cancelled
        ↓
Preview card shows: "Sending to 47 subscriber(s)"
Sample rendered: "Hi 08012345678, 50% off this weekend only!"
Send button enabled
        ↓
Taps "Send Broadcast"
→ Confirm dialog: "Send this promo to 47 subscriber(s) via SMS?"
→ POST /api/notifications/broadcast/create/ (real, no _preview)
→ Broadcast created: status = queued
        ↓
process_broadcasts runs within 1 minute
→ Sends up to 30 SMS, updates sent_count
→ Sets status = sending
→ Repeat until all sent → status = sent
        ↓
Dashboard polls broadcast progress every 3s
→ Progress bar fills
→ Final state: "47/47 sent" in green
```

---

## 7. Form Patterns & Micro-interactions

### 7.1 Form Validation

- Validation on submit (not on blur — less intrusive on mobile)
- Inline error message appears below the offending field
- Error div: `text-sm text-danger`, hidden by default, shown with `.classList.remove('hidden')`
- On successful re-submission: error divs cleared before request

### 7.2 Button Loading States

All primary action buttons follow this pattern:
```javascript
btn.disabled = true;
btn.textContent = 'Sending...'; // or 'Saving...', 'Connecting...'
// ... await response ...
btn.disabled = false;
btn.textContent = 'Original Label';
```
Prevents double-submission. Spinner not used on buttons (text is clearer on mobile).

### 7.3 CSRF on XHR Requests

All POST requests from JavaScript include:
```javascript
headers: {
    'X-CSRFToken': CSRF,   // set from {{ csrf_token }} in template
    'Content-Type': 'application/json'
}
```

### 7.4 Toasts & Feedback

- Success states: inline green banner (`bg-green-50 text-green-800 border border-green-200`)
- Error states: inline red banner (`bg-red-50 text-red-800 border border-red-200`)
- Django messages (server-side): rendered from `{% if messages %}` block in dashboard base
- Portal errors: inline `#error-div` near form (no page reload)

### 7.5 Polling Pattern

Used for: WA QR code status, broadcast progress:
```javascript
const interval = setInterval(() => {
    fetch(url)
    .then(r => r.json())
    .then(data => {
        updateUI(data);
        if (terminalState(data)) clearInterval(interval);
    })
    .catch(() => clearInterval(interval));
}, 3000);
```
Polling always clears on terminal state or error (no memory leaks).

---

## 8. Mobile Responsiveness

### 8.1 Breakpoints

Tailwind default breakpoints used:
- `md:` = 768px+ (desktop sidebar appears, bottom nav hidden)
- Below `md` = mobile layout (full width, bottom nav)

### 8.2 Mobile Dashboard

- Sidebar hidden; bottom tab bar shown (`fixed bottom-0`)
- Content has `pb-20` to avoid overlap with bottom nav
- All tables scroll horizontally (`overflow-x-auto`)
- Cards are full-width

### 8.3 Mobile Portal

- Full viewport height used (`min-h-screen`)
- Form cards are vertically centered with auto margins
- Inputs are full-width
- Buttons are full-width
- Font sizes never below `text-sm` (14px)
- Touch targets minimum 44px height (enforced by `py-2.5` on buttons)

---

## 9. Accessibility

Current state:
- Semantic HTML (`<label>` for inputs, `<button>` for actions, `<nav>` for navigation)
- `sr-only` class used for visually-hidden radio labels (broadcast type selector)
- Form labels explicitly linked to inputs via `for`/`id`
- Colour contrast: primary colour is reseller-defined — no enforcement currently
- No ARIA roles or `aria-live` regions on dynamic content (gap for future)

---

## 10. Page-by-Page Template Reference

| Template | Route | Auth | Description |
|----------|-------|------|-------------|
| `base.html` | — | — | Root layout: meta, Tailwind CDN, custom CSS vars |
| `landing.html` | `/` | None | Marketing landing page |
| `registration/login.html` | `/login/` | None | Reseller login |
| `registration/signup.html` | `/signup/` | None | Reseller signup |
| `dashboard/base.html` | — | Required | Dashboard chrome: sidebar, mobile nav, banners |
| `dashboard/overview.html` | `/dashboard/` | Required | Getting started OR live stats |
| `dashboard/plans_list.html` | `/dashboard/plans/` | Required | Plan cards list |
| `dashboard/plan_form.html` | `/dashboard/plans/create/` | Required | Create/edit plan form |
| `dashboard/subscribers_list.html` | `/dashboard/subscribers/` | Required | Subscriber table + search |
| `dashboard/subscriber_detail.html` | `/dashboard/subscribers/<id>/` | Required | Subscriber profile |
| `dashboard/payments.html` | `/dashboard/payments/` | Required | Payment history |
| `dashboard/routers.html` | `/dashboard/routers/` | Required | Router status list |
| `dashboard/settings.html` | `/dashboard/settings/` | Required | Branding + notifications config |
| `dashboard/broadcasts.html` | `/dashboard/broadcasts/` | Required | Broadcast composer + history |
| `portal/{theme}/login.html` | `/portal/` | None | Captive portal login |
| `portal/{theme}/signup.html` | `/portal/signup/` | None | Captive portal signup + OTP |
| `portal/{theme}/connected.html` | `/portal/connected/` | None | Post-auth success page |
| `portal/{theme}/account.html` | `/account/` | Token | Subscriber self-service account |
| `operator/overview.html` | `/operator/overview/` | Staff | Platform operator dashboard |

---

## 11. Known UX Gaps & Improvement Opportunities

| Gap | Impact | Suggestion |
|-----|--------|------------|
| No live data usage meter on subscriber account | Medium | Pull from `radacct` and show MB used / MB remaining progress bar |
| No reseller revenue chart on overview | Low | Simple 30-day bar chart from Payment records |
| OTP digit inputs have no paste support | Medium | Add `paste` event handler to split pasted 4-digit string across inputs |
| No empty state illustrations | Low | Add simple SVG or emoji-based empty states for plans/subscribers/payments |
| No feedback on test WhatsApp message success | Low | Show inline "✓ Message delivered" after test send |
| No colour contrast enforcement for reseller primary colour | Medium | Validate chosen hex in settings and warn if contrast ratio < 4.5:1 |
| Broadcast history doesn't persist across page reload if new | Low | `loadHistory()` is called on page load; already handled |
| No plan sorting/reordering for portal display | Medium | Add drag-and-drop or order field so reseller controls plan display order |
| Settings page is very long with no anchor nav | Medium | Add sticky section nav or tab-based layout for Settings sections |
| No confirmation email for reseller signup | Low | Send welcome email via Termii or SMTP on reseller account creation |
