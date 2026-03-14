from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from accounts.serializers import ResellerSignupSerializer, ResellerLoginSerializer


@api_view(['POST'])
@permission_classes([AllowAny])
def reseller_signup(request):
    """Register a new reseller account. Returns auth token on success."""
    serializer = ResellerSignupSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    reseller = serializer.save()
    token, _ = Token.objects.get_or_create(user=reseller.user)
    return Response({
        'token': token.key,
        'reseller': {
            'id': reseller.id,
            'name': reseller.name,
            'slug': reseller.slug,
            'email': reseller.email,
        }
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([AllowAny])
def reseller_login(request):
    """Authenticate reseller with email + password. Returns auth token."""
    serializer = ResellerLoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.validated_data['user']
    reseller = serializer.validated_data['reseller']
    token, _ = Token.objects.get_or_create(user=user)
    return Response({
        'token': token.key,
        'reseller': {
            'id': reseller.id,
            'name': reseller.name,
            'slug': reseller.slug,
            'email': reseller.email,
        }
    })


@api_view(['GET'])
def reseller_dashboard_data(request):
    """Dashboard metrics — adapts to reseller status + payment_verified."""
    reseller = request.user.reseller
    from plans.models import Subscription
    from routers.models import Router
    from billing.models import Payment
    from django.utils import timezone
    from django.db.models import Sum, Count

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Active subscriptions
    active_subs = Subscription.objects.filter(
        reseller=reseller, status='active'
    ).count()

    # Routers
    routers = Router.objects.filter(reseller=reseller)
    routers_online = routers.filter(status='online').count()
    routers_total = routers.count()

    # Revenue this month
    monthly_revenue = Payment.objects.filter(
        reseller=reseller,
        paystack_status='success',
        created_at__gte=month_start,
    ).aggregate(total=Sum('amount_ngn'))['total'] or 0

    # Today's stats
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_payments = Payment.objects.filter(
        reseller=reseller,
        paystack_status='success',
        created_at__gte=today_start,
    ).aggregate(total=Sum('amount_ngn'))['total'] or 0

    today_signups = reseller.subscribers.filter(
        created_at__gte=today_start
    ).count()

    # Pending payout (approximate: reseller's share of successful payments not yet settled)
    monthly_earnings = Payment.objects.filter(
        reseller=reseller,
        paystack_status='success',
        created_at__gte=month_start,
    ).aggregate(total=Sum('reseller_amount_ngn'))['total'] or 0

    # Recent activity
    recent_payments = Payment.objects.filter(
        reseller=reseller,
    ).select_related('subscriber', 'plan').order_by('-created_at')[:10]

    activity = []
    for p in recent_payments:
        activity.append({
            'time': p.created_at.isoformat(),
            'phone': p.subscriber.phone,
            'plan_name': p.plan.name if p.plan else 'Unknown',
            'amount': str(p.amount_ngn),
            'status': p.paystack_status,
        })

    return Response({
        'reseller': {
            'name': reseller.name,
            'location': reseller.location,
            'status': reseller.status,
            'payment_verified': reseller.payment_verified,
        },
        'stats': {
            'monthly_revenue': str(monthly_revenue),
            'active_subscribers': active_subs,
            'routers_online': routers_online,
            'routers_total': routers_total,
            'monthly_earnings': str(monthly_earnings),
            'today_payments': str(today_payments),
            'today_signups': today_signups,
        },
        'recent_activity': activity,
    })
