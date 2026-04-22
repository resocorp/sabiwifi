"""DRF permission classes that gate by RBAC capability.

Two ways to use:

  1. Subclass with a fixed capability:
        class TicketsCanRead(HasCapability):
            capability = 'tickets'

  2. Set `required_capability` on the view (function-based or class-based):
        @api_view(['GET'])
        @permission_classes([HasCapability])
        def some_view(request):
            ...
        some_view.required_capability = 'tickets'

For endpoints that accept multiple capabilities (any-of), set
`required_capabilities = ('tickets', 'tickets_own')`.
"""
from rest_framework.permissions import BasePermission

from accounts.permissions import can


class HasCapability(BasePermission):
    """Generic capability gate. Reads `capability` (single string) or
    `required_capability` / `required_capabilities` from the view."""
    capability = ''

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        caps = []
        if self.capability:
            caps.append(self.capability)
        for attr in ('required_capability', 'required_capabilities'):
            val = getattr(view, attr, None)
            if isinstance(val, str):
                caps.append(val)
            elif val:
                caps.extend(val)
        if not caps:
            return False
        return any(can(request.user, c) for c in caps)
