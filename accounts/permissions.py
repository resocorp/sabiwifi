"""Role-based access control for the reseller dashboard.

Two kinds of users may log in:
  - Reseller owner — has `Reseller.user` (OneToOne). Effective role: 'owner'.
  - Staff member — has `StaffMember.user` (OneToOne). Effective role:
    one of `field_tech`, `office_care`, `dispatcher`, `manager`.

Capabilities are coarse-grained strings that gate either entire dashboard
sections or specific actions (e.g. `'plans'`, `'assign_ticket'`,
`'tickets_own'`). Each role has a set of capabilities. The owner role's
capability set is the literal `{'all'}` — `can(user, anything)` returns True.

A few capabilities express "owned" scope (e.g. `tickets_own`) — these signal
that the queryset for that endpoint must additionally filter to the staff
member's own rows. Use `scope_queryset(qs, user)` for that.

Calling code:
  - View decorator: `@require_cap('tickets', 'tickets_own')` — pass any of
    the capabilities that should grant access; the user needs at least one.
  - Template tag: `{% load permissions_tags %}{% if can 'plans' %}…{% endif %}`.
  - DRF: `permission_classes = [HasCapability]` + set `required_capability`
    on the view (see `accounts.api_permissions`).
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


# ---------------------------------------------------------------------------
# Roles + capability matrix (mirrors the user-confirmed plan)
# ---------------------------------------------------------------------------

ROLE_OWNER       = 'owner'
ROLE_MANAGER     = 'manager'
ROLE_DISPATCHER  = 'dispatcher'
ROLE_OFFICE_CARE = 'office_care'
ROLE_FIELD_TECH  = 'field_tech'

ALL_CAPS = {
    # Page-level
    'overview', 'plans', 'subscribers', 'payments', 'routers',
    'inbox_customer', 'inbox_field', 'inbox_field_own',
    'leads', 'tickets', 'tickets_read', 'tickets_own',
    'staff_crud', 'staff_read',
    'vouchers', 'broadcasts',
    'reports', 'reports_read',
    'ai_config', 'settings',
    'profile_own',
    # Action-level
    'assign_ticket',
}

ROLE_CAPS = {
    ROLE_OWNER: {'all'},
    ROLE_MANAGER: {
        'overview', 'plans', 'subscribers', 'routers',
        'inbox_customer', 'inbox_field',
        'leads', 'tickets', 'assign_ticket',
        'staff_crud',
        'vouchers', 'broadcasts',
        'reports',
        'profile_own',
    },
    ROLE_DISPATCHER: {
        'overview', 'subscribers',
        'inbox_customer', 'inbox_field',
        'leads', 'tickets', 'assign_ticket',
        'staff_read',
        'reports',
        'profile_own',
    },
    ROLE_OFFICE_CARE: {
        'overview', 'subscribers',
        'inbox_customer',
        'leads', 'tickets_read',
        'reports_read',
        'profile_own',
    },
    ROLE_FIELD_TECH: {
        'tickets_own', 'inbox_field_own',
        'profile_own',
    },
}


# ---------------------------------------------------------------------------
# Effective role / reseller resolution
# ---------------------------------------------------------------------------

def effective_role(user) -> str:
    """Return the role string for the logged-in user.

    Returns '' if the user can't access any dashboard surface (e.g. a Django
    superuser with no Reseller link, or an inactive staff record).
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return ''
    if hasattr(user, 'reseller'):
        return ROLE_OWNER
    staff = getattr(user, 'staff_member', None)
    if staff is not None and staff.active and staff.can_log_in:
        return staff.role or ''
    return ''


def effective_reseller(user):
    """Return the Reseller this user is scoped to (owner or staff)."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    if hasattr(user, 'reseller'):
        return user.reseller
    staff = getattr(user, 'staff_member', None)
    if staff is not None:
        return staff.reseller
    return None


def effective_staff_member(user):
    """Return the StaffMember row for a logged-in staff user, else None."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'staff_member', None)


# ---------------------------------------------------------------------------
# Capability check + decorator
# ---------------------------------------------------------------------------

def can(user, capability: str) -> bool:
    """True if `user`'s effective role grants `capability`."""
    caps = ROLE_CAPS.get(effective_role(user), set())
    return 'all' in caps or capability in caps


def require_cap(*caps):
    """View decorator. Grants access if any one of the listed capabilities
    is held by the user. Always runs through @login_required first."""
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            if not any(can(request.user, c) for c in caps):
                raise PermissionDenied(
                    f'Your role lacks any of: {", ".join(caps)}'
                )
            return view(request, *args, **kwargs)
        return wrapped
    return deco


# ---------------------------------------------------------------------------
# Queryset scoping for "owned" capabilities
# ---------------------------------------------------------------------------

def scope_queryset(qs, user):
    """Filter `qs` to the user's effective reseller, AND for field techs,
    further restrict to rows owned by their StaffMember.

    Recognised models (by name): Ticket, Conversation. Other models are
    only reseller-scoped — owned scoping is a no-op for them.
    """
    reseller = effective_reseller(user)
    if reseller is None:
        return qs.none()
    qs = qs.filter(reseller=reseller)
    if effective_role(user) == ROLE_FIELD_TECH:
        staff = effective_staff_member(user)
        if staff is None:
            return qs.none()
        model_name = qs.model.__name__
        if model_name == 'Ticket':
            qs = qs.filter(assigned_staff=staff)
        elif model_name == 'Conversation':
            qs = qs.filter(kind='tech', assigned_staff=staff)
    return qs
