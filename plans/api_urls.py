from django.urls import path, include
from rest_framework.routers import DefaultRouter
from plans.views import ServicePlanViewSet

router = DefaultRouter()
router.register('', ServicePlanViewSet, basename='plan')

urlpatterns = router.urls
