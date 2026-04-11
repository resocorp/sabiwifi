# Refactor Report — 2026-04-11

## Baseline
- 337 tests passing, 0 failures, 1 skipped (431s)
- Full baseline recorded in `REFACTOR_BASELINE.md`

## Post-Refactor
- 337 tests passing, 0 failures, 1 skipped (435s)
- No regressions introduced

## Changes (5 commits, net -103 lines)

### Bug Fixes

1. **Removed dead `update_or_create` in wa_webhook** (`notifications/views.py`)
   - `WhatsappSession.objects.update_or_create(reseller__slug=slug, defaults={})` created orphaned records with no reseller FK
   - The correct `get_or_create(reseller=reseller)` was already called 4 lines below

2. **Fixed race condition in broadcast counter** (`notifications/views.py`)
   - Replaced Python-level `getattr(Broadcast.objects.get(...), field) + 1` with atomic `F(field) + 1`
   - The old pattern fetched the count, incremented in Python, then saved — concurrent webhook callbacks could lose updates

3. **Fixed potential socket leak in `_coa_disconnect`** (`radius/utils.py`)
   - Moved `sock.settimeout(3)` inside the `try/finally` block so the socket is always closed

### Deduplication / Extraction

4. **Extracted `activate_subscription()` into `plans/services.py`** (new file)
   - 6 call sites in `portal/views.py`, `billing/views.py`, `billing/wallet.py`, `vouchers/radius.py` had copy-pasted "expire → create → RADIUS sync" logic
   - Also fixed an inconsistency: some sites used 365 days as the unlimited-plan fallback, others used 36500 — now standardised on 36500 (100 years)
   - `calculate_plan_expiry()` also exported for callers that need just the date (e.g. voucher batch validity)

5. **Fixed N+1 query in `subscribers_list`** (`dashboard/views.py`)
   - Replaced per-subscriber `Subscription.objects.filter(...).first()` loop with a single `Prefetch('subscriptions', to_attr='active_subscriptions')` query
   - For a reseller with 100 subscribers, this reduces 101 queries to 2

6. **Consolidated Paystack key getters** (`billing/providers/paystack.py`)
   - Canonical `get_paystack_keys()` added to `billing/providers/paystack.py`
   - Thin wrappers in `billing/views.py`, `portal/views.py`, `shop/views.py` now delegate to it

7. **Deduplicated `MockRequest` class** (`dashboard/views.py`)
   - Two identical inner `MockRequest` classes merged into module-level `_SerializerRequest`

## Deliberately Skipped

- **`billing/providers/paystack.py` PaystackProvider.__init__** still uses `settings.PAYSTACK_SECRET_KEY` directly (will raise `AttributeError` if not in settings). The class-based provider is used differently from the function-based getter — changing it would alter its error semantics.
- **Test-only `Subscription.objects.create` calls** in test files were not changed (they're test fixtures, not application logic).
- **Portal `_create_subscription` kept as thin wrapper** — it still handles payment record creation and notification dispatch which are portal-specific concerns.

## Files Changed

| File | Change |
|------|--------|
| `plans/services.py` | **NEW** — `activate_subscription()`, `calculate_plan_expiry()` |
| `notifications/views.py` | Removed dead code, fixed race condition |
| `billing/views.py` | Uses `activate_subscription()`, delegates key getter |
| `billing/wallet.py` | Uses `activate_subscription()` in both purchase and renew paths |
| `billing/providers/paystack.py` | Added canonical `get_paystack_keys()` |
| `portal/views.py` | Uses `activate_subscription()`, delegates key getters |
| `dashboard/views.py` | Prefetch fix, `_SerializerRequest` extraction |
| `shop/views.py` | Delegates to `get_paystack_keys()` |
| `vouchers/radius.py` | Uses `activate_subscription()` with custom `expiry_date` |
| `radius/utils.py` | Socket leak fix |

## Follow-ups (not in scope)

- Add pagination to `subscribers_list` (currently loads all subscribers into memory)
- The `broadcast_create` view builds recipient lists in Python instead of a single query — could use `Exists` subquery
- `portal/views.py` (1443 → ~1370 lines) is still the largest file; could be split into separate modules per concern
