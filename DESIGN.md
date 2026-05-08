# Design System — SabiWiFi

> Source of truth for visual decisions. Read this before touching UI. Deviations
> require explicit user approval. Applies to reseller dashboard, operator panel,
> captive portal, and all transactional surfaces (emails, invoices, PDFs).

---

## Product Context

- **What this is:** Multi-tenant WiFi reseller management platform for Nigeria. Resellers deploy MikroTik hotspots and sell data plans; subscribers connect via captive portal.
- **Who it's for:** Three surfaces, three postures.
  - **Reseller** — small-business operator, desktop, needs density + control
  - **Subscriber** — end user on 360px phone in a hostel/café, needs 30-second flow
  - **Operator** — platform staff, internal tools, table-first
- **Space/industry:** WISP/ISP billing + captive portal, African emerging markets
- **Reference class:** African fintech (Paystack, Flutterwave), **not** ISP tooling (Splynx, Sonar, WISPHub). Users benchmark against tools they use daily, not competitors.
- **Memorable thing:** Serious infra, not a toy.

---

## Aesthetic Direction

- **Direction:** Refined Fintech — Paystack/Stripe editorial posture applied to WISP tooling
- **Decoration level:** Intentional (typography does most of the work; subtle hairlines, warm blacks, document-feel over CRM-feel)
- **Mood:** A well-set statement of account. Rigorous, warm, quietly confident. Never cute, never brutalist-cold, never utilitarian-enterprise.
- **Posture vs category:** The WISP category defaults to utilitarian blue enterprise dashboards. SabiWiFi deliberately breaks from that — users' daily reference is Paystack, so the platform should feel fintech-grade.
- **Deliberate departures from category:**
  1. Serif display face (Fraunces) — no WISP competitor uses serif
  2. Deep Forest platform accent instead of the category-default blue
  3. Hairline-only tables (bank-statement aesthetic) instead of zebra stripes

---

## Typography

- **Display/Hero:** **Fraunces** (variable, optical sizing on)
  - Used for: hero numerics, auth page titles, marketing headers, big currency values on receipts
  - Load: `https://fonts.bunny.net/css?family=fraunces:400,500,600,700&display=swap`
  - Rationale: editorial warmth, instantly distinctive in category, handles variable optical sizing for small-to-display use

- **Body/UI:** **General Sans** (Fontshare, free)
  - Used for: dashboard body, form labels, nav, buttons, paragraph copy
  - Load: `https://api.fontshare.com/v2/css?f[]=general-sans@400,500,600,700&display=swap`
  - Rationale: not Inter, not Roboto, not Space Grotesk — distinctive geometric sans with personality that still reads professional at 14px. Dodges the "AI template" convergence trap.

- **Data/Tables:** **Geist** with `font-variant-numeric: tabular-nums`
  - Used for: every money cell, data tables, router health logs, stat card numbers
  - Load: `https://fonts.bunny.net/css?family=geist:400,500,600&display=swap`
  - Rationale: tabular-nums so amounts align; slightly more neutral than General Sans for dense tabular scanning

- **Code:** **JetBrains Mono**
  - Used for: router configs, RADIUS group names, API keys, webhook payloads, copyable tokens
  - Load: `https://fonts.bunny.net/css?family=jetbrains-mono:400,500&display=swap`

- **Font blacklist (never use):** Inter, Roboto, Arial, Helvetica, Open Sans, Lato, Montserrat, Poppins, Space Grotesk, system-ui as display, Papyrus, Comic Sans, Lobster, Impact, Raleway, Clash Display.

- **Modular scale (1.25 ratio from 16):**
  - `text-xs` 12px — metadata, captions, table headers
  - `text-sm` 14px — dashboard body (default for most UI)
  - `text-base` 16px — portal body, forms
  - `text-lg` 18px — subsection headings
  - `text-xl` 21px — page section headings
  - `text-2xl` 28px — page titles (dashboard)
  - `text-3xl` 36px — auth titles, portal hero
  - `text-4xl` 48px — stat numbers on overview
  - `text-5xl` 64px — receipt totals, marketing hero

- **Weight policy:** 400 body, 500 labels/nav, 600 buttons/headings, 700 reserved for hero display only. Never italicize body copy (save italics for marketing display only).

---

## Color

**Approach:** Balanced — a restrained neutral system with one signature brand color (Deep Forest) and strict semantic tokens. Reseller primary color is injected per-tenant as `--color-primary` into portal templates; the platform palette below is the neutral frame it lives inside.

### Platform tokens

| Token | Hex | Usage |
|-------|-----|-------|
| `--ink` | `#0A0A0B` | Primary text. Warm near-black, never pure `#000`. |
| `--ink-muted` | `#52525B` | Secondary text, labels, metadata, help text. Passes WCAG AA on warm-white surface. |
| `--ink-subtle` | `#71717A` | Placeholder text, disabled labels. Decorative-only — never required reading. |
| `--surface` | `#FFFFFF` | Cards, primary surface |
| `--surface-alt` | `#FAFAF9` | Page background, zebra-free table background |
| `--surface-subtle` | `#F5F5F4` | Input backgrounds, hover fills |
| `--border` | `#E7E5E4` | Hairline dividers, card borders, input borders |
| `--border-strong` | `#D6D3D1` | Table grid lines where emphasis needed |

### Brand

| Token | Hex | Usage |
|-------|-----|-------|
| `--brand` | `#0E3B2E` | Deep Forest. SabiWiFi platform accent. Operator panel CTAs, transactional email headers, invoice totals, marketing pages, auth screens. Resellers override via `--color-primary` on portals. |
| `--brand-soft` | `#0E3B2E` @ 6% | Active nav background, selected row fill |
| `--brand-ink` | `#F5F5F4` | Text on brand surfaces |

### Semantic

| Token | Hex | Usage |
|-------|-----|-------|
| `--success` | `#157F4C` | Paid, active, online, confirmed |
| `--success-soft` | `#E7F4ED` | Success badge background |
| `--warning` | `#B85A00` | Expiring soon, setup incomplete, pending |
| `--warning-soft` | `#FBEBD8` | Warning banner background |
| `--danger` | `#B42318` | Failed payment, offline router, destructive CTA |
| `--danger-soft` | `#FBE9E6` | Danger banner background |
| `--info` | `#1849A9` | Info notices, neutral informational states |
| `--info-soft` | `#E6EEFA` | Info banner background |

### Reseller-injected (portal only)

```html
<style>
  :root {
    --color-primary: {{ reseller.branding.primary_color|default:'#0E3B2E' }};
    --color-primary-dark: /* computed 10% darker */;
  }
</style>
```

All `primary` tokens on portal surfaces resolve to `--color-primary`. If the reseller hasn't set one, fall back to Deep Forest.

### Dark mode strategy

Redesign surfaces, don't invert. Reduce saturations ~15%. Do NOT auto-enable in v1.
- `--ink` → `#F5F5F4`
- `--surface` → `#0A0A0B`
- `--surface-alt` → `#1A1A1B`
- `--border` → `#2A2A2B`
- Semantic colors: shift to desaturated variants (`--success` → `#4FA97A`, etc.)

### Rules

- **Never** use pure `#000` or pure `#FFFFFF` for text or primary surfaces.
- **Never** layer gradients on CTAs. Solid fill only.
- **Never** introduce a new color token without updating this file.
- Money values get `--success` on positive delta, `--danger` on negative. Zero delta is `--ink-muted`.
- Status pills use `--*-soft` as background, `--*` as text.

---

## Spacing

- **Base unit:** 4px
- **Density:** Comfortable (Paystack-level, not Notion-compact, not marketing-site-airy)
- **Scale:** `2 4 8 12 16 24 32 48 64 96` (Tailwind `0.5 1 2 3 4 6 8 12 16 24`)
- **Card interior padding:** 20px (dashboard), 16px (portal mobile), 12px (operator panel dense tables)
- **Section gap:** 24px between cards, 16px between form fields
- **Container max-widths:**
  - Dashboard shell: 1440px
  - Single-column form: 640px
  - Portal viewport: 420px (mobile-first, centered on larger screens)
  - Operator panel tables: full width of shell

---

## Layout

- **Approach:** Hybrid — grid-disciplined for app surfaces, lightly editorial for auth/marketing/receipt surfaces

### Dashboard (reseller)
- Persistent left sidebar (220px), collapsible on <1024px
- Top bar with page title, primary CTA on right
- 12-col grid inside content area, 24px gutter
- Stat cards: 1/2/3/4 across based on importance (overview uses 4-up on desktop, 2-up on tablet)
- Tables: full-width within container, hairline row dividers, no zebra stripes

### Captive portal (subscriber)
- Single column, max-width 420px, centered
- Logo + reseller brand lockup top, 32px vertical spacing
- Primary CTA always visible without scrolling on 667px viewport height (iPhone SE baseline)
- Stacked cards with 12px interior padding, 8px radius
- No sidebar, no nav — linear flow only

### Operator panel
- Same shell as dashboard, denser table padding, fewer cards
- Deep Forest header stripe signals "internal tool"

### Auth screens (all surfaces)
- Centered 400px card on `--surface-alt` background
- Fraunces h1, General Sans body, single-field-per-row
- Transactional brand below form, never above

### Border radius scale
- `radius-sm` 4px — inputs, small buttons, badges
- `radius-md` 8px — cards, modals, large buttons
- `radius-lg` 12px — prominent marketing cards only
- `radius-full` 9999px — avatars, status pills
- **Never** apply `radius-full` to rectangular CTAs.

---

## Motion

- **Approach:** Intentional. Motion clarifies state; it does not decorate.
- **Easing:**
  - Enter: `cubic-bezier(0.16, 1, 0.3, 1)` (ease-out-expo)
  - Exit: `cubic-bezier(0.7, 0, 0.84, 0)` (ease-in-quart)
  - Move: `cubic-bezier(0.45, 0, 0.55, 1)` (ease-in-out)
- **Duration:**
  - `micro` 120ms — hover/focus state shifts
  - `short` 200ms — dropdowns, modals, tooltips
  - `medium` 300ms — portal page transitions, drawer slides
  - `long` 500ms — reserved for onboarding moments, receipt generation reveal
- **Rules:**
  - No bounce, no spring, no elastic easing
  - No scroll-driven animation on dashboard or operator panel
  - Portal may use one `fadeIn(300ms)` on page load; nothing more
  - Respect `prefers-reduced-motion: reduce` — disable all non-essential motion

---

## Icons

Current state: emoji inline. Keep for now (zero bundle, renders reliably across mobile browsers). When a proper icon set is introduced, use **Lucide** (stroke 1.5px, 20px default, 16px dense contexts). Never mix emoji + icon library in the same view.

---

## Component-level patterns

These derive from the tokens above. When building new components, match these; don't invent.

- **Button (primary):** `bg-brand`, `text-brand-ink`, `radius-md`, `14px/General Sans 600`, padding `10px 16px`, hover `opacity-90`, focus `ring-2 ring-brand/30`. Never gradient.
- **Button (secondary):** `surface`, `1px border`, `text-ink`, same sizing as primary.
- **Button (ghost):** transparent background, `text-ink-muted`, hover `bg-surface-subtle`.
- **Input:** `surface-subtle` background, `1px border-border`, `radius-sm`, `14px`, padding `10px 12px`, focus `border-brand ring-2 ring-brand/20`.
- **Card:** `surface`, `1px border-border`, `radius-md`, `p-5`. No shadow by default; shadow only on floating elements (modals, popovers).
- **Table:** no container border, hairline row dividers only, `text-sm`, money columns right-aligned with `tabular-nums`. Header row: `text-xs`, `font-medium`, `text-ink-muted`, `uppercase`, `tracking-wider`, no background.
- **Stat card:** label `text-xs uppercase tracking-wider text-ink-muted`, number `text-4xl font-semibold Fraunces tabular-nums`, delta below in `text-sm` semantic color.
- **Status pill:** `radius-full`, `text-xs`, `font-medium`, 4px vertical / 8px horizontal padding, semantic soft background + semantic text.

---

## Anti-patterns (never ship)

1. Purple/violet gradient accents
2. Three-column feature grid with icons in colored circles (the "SaaS landing" cliché)
3. Centered-everything uniform spacing
4. Uniform bubbly `rounded-2xl` on every element
5. Gradient CTAs
6. Stock-photo hero images
7. `system-ui` or `-apple-system` as display or body font
8. "Built for X" / "Designed for Y" marketing copy patterns
9. Zebra-striped tables (we're not a spreadsheet)
10. Emoji in operator-facing analytical reports (keep emoji for friendly surfaces only: portal connected state, broadcast type selection, setup prompts)

---

## Portal theme relationship

The three existing portal themes (`modern`, `bold`, `minimal`) are pre-existing product choices resellers pick from. This design system sets the **structural frame** all three must respect:

- Typography scale, spacing scale, motion system, border-radius scale — all three themes MUST use these
- Each theme expresses differently through: font weight choices within the scale, border-radius selection within the scale (minimal uses `sm`, bold uses `md`, modern uses `md`), density of whitespace, presence of decorative elements
- Reseller `--color-primary` injection works identically across all three
- None of the three may introduce tokens outside this file

---

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-24 | Reference class = African fintech (Paystack), not WISP tools | User benchmark is daily-use tools, not competitors. Eureka from /gstack-design-consultation. |
| 2026-04-24 | Fraunces display face | Single biggest differentiator in category; no WISP competitor uses serif. |
| 2026-04-24 | Deep Forest `#0E3B2E` platform accent | Signals money/trust/Africa without clichéd green or category-default blue. |
| 2026-04-24 | Hairline-only tables, tabular-nums | Bank-statement aesthetic; reinforces "serious infra" posture. |
| 2026-04-24 | General Sans body over Inter/Space Grotesk | Dodge AI-template convergence; distinctive at small sizes. |
| 2026-04-24 | Initial system created | /gstack-design-consultation run on branch `pr-c-router-name-edit-and-reports`. Existing `docs/UXUI_DESIGN.md` retained as implementation reference; this file is the enforcement source. |
