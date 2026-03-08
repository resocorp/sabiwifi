from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from plans.models import ServicePlan
from plans.serializers import ServicePlanSerializer


class IsResellerOwner(permissions.BasePermission):
    """Ensure the reseller only accesses their own plans."""
    def has_object_permission(self, request, view, obj):
        return obj.reseller == request.user.reseller


class ServicePlanViewSet(viewsets.ModelViewSet):
    """CRUD for reseller's service plans."""
    serializer_class = ServicePlanSerializer
    permission_classes = [permissions.IsAuthenticated, IsResellerOwner]

    def get_queryset(self):
        if hasattr(self.request.user, 'reseller'):
            return ServicePlan.objects.filter(reseller=self.request.user.reseller)
        return ServicePlan.objects.none()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """Soft-disable a plan (subscribers keep access until expiry)."""
        plan = self.get_object()
        plan.is_active = False
        plan.save()
        return Response({'status': 'Plan disabled.'})

    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """Re-enable a disabled plan."""
        plan = self.get_object()
        plan.is_active = True
        plan.save()
        return Response({'status': 'Plan enabled.'})
