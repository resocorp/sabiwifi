# SabiWiFi — Positioning

**Status**: Active
**Last updated**: 2026-05-01

---

## The model

SabiWiFi has two distinct assets that should be priced, marketed, and protected separately:

1. **The brand** — a consumer-facing ISP. Public face on `sabiwifi.com`. Today serves Awka; Benin next. Customers know SabiWiFi as the ISP they pay for internet.
2. **The platform** — a multi-tenant operator backend (RADIUS, billing, vouchers, captive portal, WhatsApp/SMS, dashboards). Sold to operators on any backbone. **White-label by default.** Partners run under their own brand — the SabiWiFi name does not appear on their captive portals or public surfaces.

Partners are invisible to consumers at the acquisition layer. The brand earns equity through consumer-facing convenience (easy recharge, support, reliability where SabiWiFi owns the last mile), not through forcing partners to wear the brand.

## Public surface (`sabiwifi.com`)

- Frames SabiWiFi as a standalone ISP. Consumer copy. No reseller-acquisition copy on the main page.
- Quick recharge widget on the landing page: customer enters phone, gets OTP, recharges. End-to-end SabiWiFi-branded. The customer never sees the partner identity unless their phone matches multiple partners (disambiguation step).
- Backend revenue split is unchanged — the partner whose subscriber recharged still gets their Paystack subaccount payout via the existing split logic.
- Shop stays public, mixed audience (consumer + operator gear).
- Partner signup lives at `/partners` — a quiet, operator-focused B2B page linked discreetly from the main footer.

## Partner experience

- Captive portals on partners' networks render fully partner-branded (logo, colors, name) — already the case in `templates/portal/{modern,bold,minimal}/`.
- Partners self-serve at `/partners/signup/` (no operator approval gate currently).
- Partners pay platform fees via the existing commission split. No tier system, no "Brand Licensee" gate. The simpler ISP-public-face model supersedes the earlier three-layer (ISP / platform / brand-licensee) idea.

## What this rules out

- **Brand contamination via partner networks**: partners do not get the SabiWiFi name on their public-facing surfaces. A flaky partner network does not degrade the brand because consumers don't see the brand on it.
- **"Powered by SabiWiFi" marks on partner portals**: not used. Partners are fully white-label.
- **Three-layer Brand Licensee tier**: deferred / dropped. The simpler model is: SabiWiFi is the ISP brand; everyone else is a white-label platform customer.

## Phone uniqueness across partners

`Subscriber.phone` remains unique *per reseller* (no model migration). The recharge widget handles the rare cross-partner case at the API layer: lookup returns 0/1/N matches, frontend asks "which network are you on?" only when N>1.

## When to revisit

- If/when SabiWiFi's brand earns enough independent demand that operators in other regions actively want to license it (the original "SabiWiFi Launch" idea), revisit and add a brand-licensee tier on top of this model. Until then: do not.
