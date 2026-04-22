"""Resolve effective role + reseller once per request so views don't repeat
the lookup. Sits after AuthenticationMiddleware in `MIDDLEWARE`.

Adds three attributes to `request`:
  - `request.effective_role`     — string (one of `accounts.permissions.ROLE_*`, or '')
  - `request.effective_reseller` — Reseller instance or None
  - `request.staff_member`       — StaffMember instance for staff users, else None
"""
from accounts.permissions import (
    effective_role, effective_reseller, effective_staff_member,
)


class EffectiveResellerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        request.effective_role = effective_role(user)
        request.effective_reseller = effective_reseller(user)
        request.staff_member = effective_staff_member(user)
        return self.get_response(request)
