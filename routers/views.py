import secrets
import logging
from django.conf import settings
from django.http import HttpResponse
from rest_framework import status, permissions
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from routers.models import Router
from routers.serializers import RouterAddSerializer, RouterSerializer, RouterSSIDSerializer
from routers.provision import generate_provision_rsc
from routers.bootstrap import generate_bootstrap_rsc, generate_generic_bootstrap_rsc, generate_phonehome_setup_rsc
from routers.serial_utils import is_valid_mikrotik_serial
from routers.wg_utils import generate_keypair, add_peer, remove_peer, WireGuardError

logger = logging.getLogger(__name__)


class ProvisionRateThrottle(AnonRateThrottle):
    """Rate limit for the unauthenticated provision endpoint (router phones home every 30s)."""
    rate = '240/hour'


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
    """Generate real WireGuard keys, NAS secret, and API credentials for a router."""
    wg_private_key, wg_public_key = generate_keypair()

    router.wg_public_key = wg_public_key
    router.wg_private_key = wg_private_key
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

    # Generate credentials (real WireGuard keys)
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

    # Add WireGuard peer on the server
    try:
        add_peer(router.wg_public_key, router.wg_tunnel_ip)
    except WireGuardError as e:
        logger.error(f"Failed to add WG peer for router {serial}: {e}")
        router.status = 'failed'
        router.save(update_fields=['status'])
        return Response(
            {'error': 'Router claimed but WireGuard setup failed. Contact support.'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # Store the private key temporarily for provision endpoint
    from django.core.cache import cache
    cache.set(f'wg_privkey_{router.serial_number}', wg_private_key, timeout=86400)

    logger.info(f"Router {serial} assigned to reseller {reseller.name}")

    return Response({
        'status': 'Router added successfully.',
        'router': RouterSerializer(router).data,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([ProvisionRateThrottle])
def router_provision(request, serial):
    """
    Phone-home endpoint: router requests its config.
    Serial-only auth with rate limiting. Returns .rsc file.
    """
    serial = serial.strip().upper()

    from django.utils import timezone

    try:
        router = Router.objects.get(serial_number=serial)
    except Router.DoesNotExist:
        # Auto-register if it looks like a valid MikroTik serial
        if not is_valid_mikrotik_serial(serial):
            return HttpResponse('# not found', content_type='text/plain')

        router, created = Router.objects.get_or_create(
            serial_number=serial,
            defaults={'status': 'available'},
        )
        if created:
            logger.info(f"Auto-registered router {serial} from phone-home")

    # Update last_seen on every phone-home attempt
    Router.objects.filter(pk=router.pk).update(last_seen=timezone.now())

    if router.reseller is None:
        # Router exists but not claimed by a reseller yet
        return HttpResponse('# not ready', content_type='text/plain')

    # Use stored credentials, or generate if first time
    wg_private_key = router.wg_private_key
    if not wg_private_key or not router.wg_public_key:
        # First provision or factory reset — generate new credentials
        old_public_key = router.wg_public_key
        try:
            if old_public_key:
                remove_peer(old_public_key)
        except WireGuardError as e:
            logger.warning(f"Failed to remove old WG peer for {serial}: {e}")

        wg_private_key = _generate_credentials(router)
        router.save()

        # Update NAS entry for FreeRADIUS
        from radius.models import Nas
        Nas.objects.update_or_create(
            shortname=router.serial_number,
            defaults={
                'nasname': str(router.wg_tunnel_ip),
                'type': 'other',
                'secret': router.nas_secret,
                'description': f'SabiWiFi router {router.serial_number}',
            }
        )

        # Add new WG peer
        try:
            add_peer(router.wg_public_key, router.wg_tunnel_ip)
        except WireGuardError as e:
            logger.error(f"Failed to add new WG peer for {serial}: {e}")

    # Generate provision script
    rsc_content = generate_provision_rsc(router, wg_private_key)

    # Update router status
    router.status = 'provisioned'
    router.provision_count += 1
    router.save()

    logger.info(f"Provision script delivered for router {serial} (count: {router.provision_count})")

    return HttpResponse(rsc_content, content_type='text/plain')


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([ProvisionRateThrottle])
def router_heartbeat(request, serial):
    """
    Lightweight heartbeat endpoint. Called every 2 min by provisioned routers.
    Updates last_seen and sets status to online.
    """
    serial = serial.strip().upper()
    from django.utils import timezone

    updated = Router.objects.filter(serial_number=serial).update(
        last_seen=timezone.now(),
        status='online',
    )
    if updated:
        return HttpResponse('# ok', content_type='text/plain')
    return HttpResponse('# unknown', content_type='text/plain')


@api_view(['GET'])
@permission_classes([IsAdminUser])
def router_bootstrap(request, serial):
    """
    Generate and download a bootstrap .rsc script for a router.
    Staff-only. The bootstrap is the minimal config flashed via Netinstall/USB.
    """
    serial = serial.strip().upper()

    try:
        router = Router.objects.get(serial_number=serial)
    except Router.DoesNotExist:
        return Response({'error': 'Router not found.'}, status=404)

    platform_domain = settings.PLATFORM_DOMAIN
    rsc_content = generate_bootstrap_rsc(serial, platform_domain)

    response = HttpResponse(rsc_content, content_type='text/plain')
    response['Content-Disposition'] = f'attachment; filename="bootstrap-{serial}.rsc"'
    return response


@api_view(['GET'])
@permission_classes([IsAdminUser])
def router_bootstrap_generic(request):
    """
    Download a generic bootstrap .rsc script (no serial embedded).
    Staff-only. The script reads the hardware serial at runtime.
    Flash this to any MikroTik via Netinstall or USB.
    """
    platform_domain = settings.PLATFORM_DOMAIN
    rsc_content = generate_generic_bootstrap_rsc(platform_domain)

    response = HttpResponse(rsc_content, content_type='text/plain')
    response['Content-Disposition'] = 'attachment; filename="bootstrap-generic.rsc"'
    return response


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([ProvisionRateThrottle])
def router_phonehome_setup(request):
    """
    Stage 2 bootstrap: returns phonehome-setup.rsc.
    Called by the Stage 1 setup script on the router via /tool/fetch + /import.
    Unauthenticated (router has no credentials at this stage).
    """
    platform_domain = settings.PLATFORM_DOMAIN
    rsc_content = generate_phonehome_setup_rsc(platform_domain)
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

    new_ssid = serializer.validated_data['ssid']

    from routers.routeros_utils import set_ssid, RouterOSError
    try:
        set_ssid(router, new_ssid)
    except RouterOSError as e:
        logger.error(f"SSID change failed for router {router.serial_number}: {e}")
        return Response(
            {'error': f'Failed to update SSID: {e}'},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    logger.info(f"SSID updated for router {router.serial_number}: {new_ssid}")
    return Response({'status': f'SSID updated to "{new_ssid}".'})


@api_view(['GET'])
def router_stats(request, pk):
    """
    Fetch live stats from a router via REST API over WireGuard.
    Returns: CPU, memory, uptime, WAN traffic, connected devices, WiFi info.
    Called via AJAX from the dashboard.
    """
    reseller = request.user.reseller
    try:
        router = Router.objects.get(pk=pk, reseller=reseller)
    except Router.DoesNotExist:
        return Response({'error': 'Router not found.'}, status=404)

    if router.status not in ('online', 'provisioned'):
        return Response({
            'error': 'Router is not online.',
            'status': router.status,
        }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    from routers.routeros_utils import get_router_stats as fetch_stats, RouterOSError
    try:
        stats = fetch_stats(router)
        stats['serial_number'] = router.serial_number
        stats['router_status'] = router.status
        return Response(stats)
    except RouterOSError as e:
        logger.error(f"Stats fetch failed for router {router.serial_number}: {e}")
        return Response(
            {'error': f'Cannot reach router: {e}'},
            status=status.HTTP_502_BAD_GATEWAY,
        )


@api_view(['POST'])
def router_wifi(request, pk):
    """
    Update WiFi settings (SSID and/or password) on a specific interface.
    Body: { "ssid": "...", "password": "...", "interface": "wifi1" }
    Password empty string = open network (remove WPA).
    """
    reseller = request.user.reseller
    try:
        router = Router.objects.get(pk=pk, reseller=reseller)
    except Router.DoesNotExist:
        return Response({'error': 'Router not found.'}, status=404)

    if router.status != 'online':
        return Response(
            {'error': 'Router is not online.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from routers.routeros_utils import set_wifi_ssid, set_wifi_password, RouterOSError

    iface = request.data.get('interface', 'wifi1')
    new_ssid = request.data.get('ssid', '').strip()
    new_password = request.data.get('password', None)  # None = don't change

    results = []

    if new_ssid:
        if len(new_ssid) > 32:
            return Response({'error': 'SSID must be 1-32 characters.'}, status=400)
        try:
            set_wifi_ssid(router, new_ssid, iface)
            results.append(f'SSID updated to "{new_ssid}"')
        except RouterOSError as e:
            return Response({'error': f'SSID update failed: {e}'}, status=502)

    if new_password is not None:
        try:
            set_wifi_password(router, new_password, iface)
            if new_password:
                results.append('WiFi password updated')
            else:
                results.append('WiFi password removed (open network)')
        except RouterOSError as e:
            return Response({'error': f'Password update failed: {e}'}, status=502)

    if not results:
        return Response({'error': 'No changes requested.'}, status=400)

    return Response({'status': '. '.join(results) + '.'})
