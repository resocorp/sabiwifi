import secrets
import logging
from django.http import HttpResponse
from rest_framework import status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from routers.models import Router
from routers.serializers import RouterAddSerializer, RouterSerializer, RouterSSIDSerializer
from routers.provision import generate_provision_rsc

logger = logging.getLogger(__name__)


def _allocate_tunnel_ip():
    """Allocate the next available WireGuard tunnel IP in the 10.99.0.0/16 range."""
    last_router = Router.objects.filter(
        wg_tunnel_ip__isnull=False
    ).order_by('-wg_tunnel_ip').first()

    if last_router and last_router.wg_tunnel_ip:
        parts = last_router.wg_tunnel_ip.split('.')
        last_octet = int(parts[3])
        third_octet = int(parts[2])
        last_octet += 1
        if last_octet > 254:
            last_octet = 2
            third_octet += 1
        return f'10.99.{third_octet}.{last_octet}'
    return '10.99.0.2'


def _generate_credentials(router):
    """Generate WireGuard keys, NAS secret, and API credentials for a router."""
    # WireGuard keys — in production, use actual WG key generation
    # For now, generate placeholder keys (real implementation needs wg genkey)
    wg_private_key = secrets.token_urlsafe(32)
    wg_public_key = secrets.token_urlsafe(32)  # Derived from private in production

    router.wg_public_key = wg_public_key
    router.wg_tunnel_ip = _allocate_tunnel_ip()
    router.nas_secret = secrets.token_urlsafe(24)
    router.api_username = 'sabiwifi'
    router.api_password = secrets.token_urlsafe(24)

    return wg_private_key


@api_view(['POST'])
def router_add(request):
    """Reseller submits a serial number to claim a router."""
    serializer = RouterAddSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    serial = serializer.validated_data['serial_number']
    router = Router.objects.get(serial_number=serial)
    reseller = request.user.reseller

    # Generate credentials
    wg_private_key = _generate_credentials(router)

    # Assign to reseller
    router.reseller = reseller
    router.status = 'pending_provision'
    router.save()

    # Write NAS entry for FreeRADIUS
    from radius.models import Nas
    Nas.objects.update_or_create(
        nasname=router.wg_tunnel_ip,
        defaults={
            'shortname': router.serial_number,
            'type': 'other',
            'secret': router.nas_secret,
            'description': f'SabiWiFi router {router.serial_number}',
        }
    )

    # Store the private key temporarily in the session for provision endpoint
    # In production, this would be stored encrypted and short-lived
    from django.core.cache import cache
    cache.set(f'wg_privkey_{router.serial_number}', wg_private_key, timeout=86400)

    logger.info(f"Router {serial} assigned to reseller {reseller.name}")

    return Response({
        'status': 'Router added successfully.',
        'router': RouterSerializer(router).data,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([AllowAny])
def router_provision(request, serial):
    """
    Phone-home endpoint: router requests its config.
    Serial-only auth. Returns .rsc file.
    """
    serial = serial.strip().upper()

    try:
        router = Router.objects.get(serial_number=serial)
    except Router.DoesNotExist:
        return HttpResponse('Not found', status=404, content_type='text/plain')

    if router.reseller is None:
        return HttpResponse('Not assigned', status=404, content_type='text/plain')

    # Retrieve WG private key from cache (or regenerate if needed)
    from django.core.cache import cache
    wg_private_key = cache.get(f'wg_privkey_{router.serial_number}')
    if not wg_private_key:
        # Regenerate if cache expired (factory reset scenario)
        wg_private_key = _generate_credentials(router)
        router.save()
        cache.set(f'wg_privkey_{router.serial_number}', wg_private_key, timeout=86400)

    # Generate provision script
    rsc_content = generate_provision_rsc(router, wg_private_key)

    # Update router status
    router.status = 'provisioned'
    router.provision_count += 1
    router.save()

    logger.info(f"Provision script delivered for router {serial} (count: {router.provision_count})")

    return HttpResponse(rsc_content, content_type='text/plain')


@api_view(['GET'])
def router_list(request):
    """List all routers for the authenticated reseller."""
    reseller = request.user.reseller
    routers = Router.objects.filter(reseller=reseller)
    serializer = RouterSerializer(routers, many=True)
    return Response(serializer.data)


@api_view(['GET'])
def router_status(request, pk):
    """Get router health status."""
    reseller = request.user.reseller
    try:
        router = Router.objects.get(pk=pk, reseller=reseller)
    except Router.DoesNotExist:
        return Response({'error': 'Router not found.'}, status=404)

    return Response(RouterSerializer(router).data)


@api_view(['POST'])
def router_ssid(request, pk):
    """Update WiFi SSID via RouterOS API over WireGuard."""
    reseller = request.user.reseller
    try:
        router = Router.objects.get(pk=pk, reseller=reseller)
    except Router.DoesNotExist:
        return Response({'error': 'Router not found.'}, status=404)

    serializer = RouterSSIDSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    if router.status != 'online':
        return Response(
            {'error': 'Router is not online. SSID change will be applied when it comes back online.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # TODO: Implement actual RouterOS API call over WireGuard
    # For now, return success placeholder
    new_ssid = serializer.validated_data['ssid']
    logger.info(f"SSID change requested for router {router.serial_number}: {new_ssid}")

    return Response({'status': f'SSID updated to "{new_ssid}".'})
