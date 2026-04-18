import secrets
import logging
from django.conf import settings
from django.http import HttpResponse
from rest_framework import status, permissions
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from routers.models import Router
from routers.serializers import RouterAddSerializer, RouterSerializer, RouterSSIDSerializer
from routers.provision import generate_provision_rsc
from routers.bootstrap import generate_bootstrap_rsc, generate_generic_bootstrap_rsc, generate_phonehome_setup_rsc
from routers.serial_utils import is_valid_mikrotik_serial, is_valid_mac, normalize_mac
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
    """Reseller submits a serial number (or MAC for OpenWrt) to claim a router."""
    serializer = RouterAddSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    serial = serializer.validated_data['serial_number']
    router = Router.objects.get(serial_number=serial)
    reseller = request.user.reseller

    if router.device_type == 'openwrt':
        # OpenWrt devices generate their own WG keys on first boot.
        # The public key is sent via heartbeat header and already stored.
        if not router.wg_public_key:
            return Response(
                {'error': 'Router has not sent its WireGuard key yet. '
                          'Ensure it is powered on and connected to the internet, '
                          'then wait a few minutes and try again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Allocate tunnel IP and NAS secret (don't overwrite WG keys)
        if not router.wg_tunnel_ip:
            router.wg_tunnel_ip = _allocate_tunnel_ip()
        if not router.nas_secret:
            router.nas_secret = secrets.token_urlsafe(24)

        router.reseller = reseller
        router.status = 'pending_provision'
        router.save()

        # Add WG peer using the device's own public key
        try:
            add_peer(router.wg_public_key, router.wg_tunnel_ip)
        except WireGuardError as e:
            logger.error(f"Failed to add WG peer for OpenWrt {serial}: {e}")

        # Write NAS entry for FreeRADIUS and reload
        from radius.utils import upsert_nas_and_reload
        upsert_nas_and_reload(
            nasname=router.wg_tunnel_ip,
            shortname=router.serial_number,
            secret=router.nas_secret,
            description=f'SabiWiFi OpenWrt {router.serial_number}',
        )

        logger.info(f"OpenWrt {serial} assigned to reseller {reseller.name}, "
                     f"pending provision via heartbeat")

    else:
        # MikroTik: server generates WG keys and delivers via provision .rsc
        wg_private_key = _generate_credentials(router)

        router.reseller = reseller
        router.status = 'pending_provision'
        router.save()

        from radius.utils import upsert_nas_and_reload
        upsert_nas_and_reload(
            nasname=router.wg_tunnel_ip,
            shortname=router.serial_number,
            secret=router.nas_secret,
            description=f'SabiWiFi router {router.serial_number}',
        )

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

        # Update NAS entry for FreeRADIUS and reload
        from radius.utils import upsert_nas_and_reload
        upsert_nas_and_reload(
            nasname=str(router.wg_tunnel_ip),
            shortname=router.serial_number,
            secret=router.nas_secret,
            description=f'SabiWiFi router {router.serial_number}',
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
    Lightweight heartbeat endpoint. Called every 2 min by all routers.
    Updates last_seen, clears offline_since, logs recovery if coming back online.

    For OpenWrt devices (serial = MAC address):
      - Auto-creates the Router record on first heartbeat
      - Stores the WireGuard public key from X-WG-Public-Key header
      - Returns inline provision script when status is 'pending_provision'
    """
    serial = serial.strip().upper()
    from django.utils import timezone
    from routers.models import RouterHealthLog
    from routers.serial_utils import is_valid_mac, normalize_mac

    # Detect device type by identifier format
    if is_valid_mac(serial):
        mac = normalize_mac(serial)
        device_type = 'openwrt'
        lookup_serial = mac
    else:
        device_type = 'mikrotik'
        lookup_serial = serial

    # Feature flag: reject OpenWrt heartbeats until the subsystem ships.
    if device_type == 'openwrt' and not getattr(settings, 'OPENWRT_ENABLED', False):
        return HttpResponse('# openwrt disabled', content_type='text/plain')

    router = Router.objects.filter(serial_number=lookup_serial).first()

    # Auto-register OpenWrt device on first heartbeat (no phone-home needed)
    if not router and device_type == 'openwrt':
        router, created = Router.objects.get_or_create(
            serial_number=lookup_serial,
            defaults={
                'device_type': 'openwrt',
                'status': 'available',
            },
        )
        if created:
            logger.info(f'Auto-registered OpenWrt device {lookup_serial} via heartbeat')

    if not router:
        return HttpResponse('# unknown', content_type='text/plain')

    # Store WireGuard public key when sent by OpenWrt device
    wg_pub = request.META.get('HTTP_X_WG_PUBLIC_KEY', '').strip()
    update_fields = ['last_seen', 'status', 'offline_since']
    if wg_pub and wg_pub != router.wg_public_key:
        old_wg_pub = router.wg_public_key
        router.wg_public_key = wg_pub
        update_fields.append('wg_public_key')
        # Update server-side WG peer (e.g. after router reflash generates new keys)
        if router.wg_tunnel_ip:
            try:
                if old_wg_pub:
                    remove_peer(old_wg_pub)
                add_peer(wg_pub, router.wg_tunnel_ip)
            except WireGuardError as e:
                logger.error(f"Failed to update WG peer for {lookup_serial}: {e}")

    was_offline = router.status in ('offline', 'pending_provision')
    router.last_seen = timezone.now()
    # Don't overwrite pending_provision — the device still needs provisioning
    if router.status != 'pending_provision':
        router.status = 'online'
    router.offline_since = None
    router.save(update_fields=update_fields)

    if was_offline and router.status == 'online':
        RouterHealthLog.objects.create(router=router, event='online')
        logger.info(f'Router {lookup_serial} recovered via heartbeat')

    # Cache system stats from OpenWrt heartbeat query parameters
    if device_type == 'openwrt' and request.GET.get('cpu') is not None:
        _cache_openwrt_stats(request, router)

    # ── OpenWrt: return full provision script when pending ──
    # The heartbeat script detects "#!/bin/sh" and executes it.
    if device_type == 'openwrt' and router.reseller and router.status == 'pending_provision':
        from routers.openwrt_provision import generate_openwrt_provision
        script = generate_openwrt_provision(router)
        from django.db.models import F
        Router.objects.filter(pk=router.pk).update(
            status='provisioned',
            provision_count=F('provision_count') + 1,
        )
        logger.info(f"Provision script delivered to OpenWrt {lookup_serial} via heartbeat")
        return HttpResponse(script, content_type='text/plain')

    # ── MikroTik: return re-provision script when server knows the device ──
    # needs it (fresh claim, failed WG handshake flagged by check_routers, or
    # manual retrigger). The router's heartbeat script detects the
    # "# SABIWIFI-REPROVISION-v1" sentinel and /import-s the body.
    if (
        device_type == 'mikrotik'
        and router.reseller
        and (router.status == 'pending_provision' or router.needs_reprovision)
        and router.wg_private_key
    ):
        rsc = generate_provision_rsc(router, router.wg_private_key)
        from django.db.models import F
        Router.objects.filter(pk=router.pk).update(
            status='provisioned',
            provision_count=F('provision_count') + 1,
            needs_reprovision=False,
        )
        logger.info(
            f"Re-provision delivered to MikroTik {lookup_serial} via heartbeat "
            f"(was {router.status}, needs_reprovision={router.needs_reprovision})"
        )
        return HttpResponse(rsc, content_type='text/plain')

    return HttpResponse('# ok', content_type='text/plain')


def _cache_openwrt_stats(request, router):
    """Parse system stats from OpenWrt heartbeat query params and cache in Redis."""
    from django.core.cache import cache

    def _int(val, default=0):
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _fmt_bytes(nbytes):
        nbytes = _int(nbytes)
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if nbytes < 1024:
                return f'{nbytes:.1f} {unit}' if unit != 'B' else f'{nbytes} B'
            nbytes /= 1024
        return f'{nbytes:.1f} PB'

    uptime_secs = _int(request.GET.get('uptime'))
    days = uptime_secs // 86400
    hours = (uptime_secs % 86400) // 3600
    mins = (uptime_secs % 3600) // 60
    if days:
        uptime_str = f'{days}d {hours}h'
    elif hours:
        uptime_str = f'{hours}h {mins}m'
    else:
        uptime_str = f'{mins}m'

    stats = {
        'cpu_load': _int(request.GET.get('cpu')),
        'memory_pct': _int(request.GET.get('mem')),
        'uptime': uptime_str or '--',
        'connected_devices': _int(request.GET.get('wifi_clients')),
        'wan_rx': _fmt_bytes(request.GET.get('wan_rx')),
        'wan_tx': _fmt_bytes(request.GET.get('wan_tx')),
        'wifi': [
            {
                'name': 'guest',
                'band': '2.4 GHz',
                'ssid': '',
                'enabled': True,
                'running': True,
                'has_password': False,
                'clients': _int(request.GET.get('guest_clients')),
            },
            {
                'name': 'guest5',
                'band': '5 GHz',
                'ssid': '',
                'enabled': True,
                'running': True,
                'has_password': False,
                'clients': _int(request.GET.get('guest5_clients')),
            },
        ],
    }

    cache.set(f'openwrt_stats_{router.serial_number}', stats, timeout=300)


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
    Fetch live stats for a router. Called via AJAX from the dashboard.

    MikroTik: RouterOS REST API over WireGuard (existing path).
    OpenWrt:  OpenWISP monitoring API (CPU, memory, uptime, clients, traffic).
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

    if router.device_type == 'openwrt':
        return _router_stats_openwrt(router)

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


def _router_stats_openwrt(router):
    """Fetch stats for an OpenWrt router from heartbeat cache (Redis)."""
    from django.core.cache import cache

    stats = cache.get(f'openwrt_stats_{router.serial_number}')
    if not stats:
        return Response(
            {'error': 'No recent stats available. Router may not have reported yet.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Populate WiFi SSIDs from reseller branding
    wifi_ssid = 'SabiWiFi'
    wifi_ssid_5g = 'SabiWiFi-5G'
    if router.reseller and router.reseller.branding:
        custom = router.reseller.branding.get('ssid', '')
        if custom:
            wifi_ssid = custom
            wifi_ssid_5g = f'{custom}-5G'

    for entry in stats.get('wifi', []):
        if entry['band'] == '2.4 GHz':
            entry['ssid'] = wifi_ssid
        elif entry['band'] == '5 GHz':
            entry['ssid'] = wifi_ssid_5g

    stats['serial_number'] = router.serial_number
    stats['router_status'] = router.status
    return Response(stats)


@api_view(['POST'])
def router_wifi(request, pk):
    """
    Update WiFi settings (SSID and/or password) on a specific interface.
    Body: { "ssid": "...", "password": "...", "interface": "wifi1" }

    MikroTik: pushed immediately via RouterOS API over WireGuard.
    OpenWrt:  SSID updated via OpenWISP device context vars → config template
              re-pushed by openwisp-config on next poll (≤5 min).
              Passwords are not supported on OpenWrt (uspot guest network is open).
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

    if router.device_type == 'openwrt':
        return _router_wifi_openwisp(router, request.data)

    from routers.routeros_utils import set_wifi_ssid, set_wifi_password, RouterOSError

    iface = request.data.get('interface', 'wifi1')
    new_ssid = request.data.get('ssid', '').strip()
    new_password = request.data.get('password', None)

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


def _router_wifi_openwisp(router, data):
    """Update SSID for an OpenWrt router via OpenWISP device context vars."""
    new_ssid = data.get('ssid', '').strip()
    if not new_ssid:
        return Response({'error': 'SSID is required for OpenWrt routers.'}, status=400)
    if len(new_ssid) > 32:
        return Response({'error': 'SSID must be 1-32 characters.'}, status=400)

    try:
        from integrations.models import OpenWISPBinding
        from integrations.openwisp import OpenWISPClient, OpenWISPError
        binding = OpenWISPBinding.objects.filter(router=router).first()
        if not binding:
            return Response({'error': 'Router not registered in OpenWISP.'}, status=503)
        client = OpenWISPClient()
        client.update_device_context(str(binding.openwisp_id), {
            'ssid': new_ssid,
            'ssid_5g': f'{new_ssid}-5G',
        })
        # Persist SSID in reseller branding so it survives server restarts
        if router.reseller:
            branding = router.reseller.branding or {}
            branding['ssid'] = new_ssid
            router.reseller.branding = branding
            router.reseller.save(update_fields=['branding'])
        logger.info(f'SSID updated via OpenWISP for {router.serial_number}: {new_ssid}')
        return Response({'status': f'SSID updated to "{new_ssid}". Change will apply within 5 minutes.'})
    except Exception as e:
        logger.error(f'OpenWISP SSID update failed for {router.serial_number}: {e}')
        return Response({'error': f'SSID update failed: {e}'}, status=502)


@api_view(['GET'])
def router_health_log(request, pk):
    """Return last 20 health events (online/offline transitions) for a router."""
    reseller = request.user.reseller
    try:
        router = Router.objects.get(pk=pk, reseller=reseller)
    except Router.DoesNotExist:
        return Response({'error': 'Router not found.'}, status=404)

    from routers.models import RouterHealthLog
    logs = RouterHealthLog.objects.filter(router=router).order_by('-created_at')[:20]
    data = [
        {
            'event': log.event,
            'label': log.get_event_display(),
            'timestamp': log.created_at.strftime('%d %b %Y %H:%M'),
            'iso': log.created_at.isoformat(),
        }
        for log in logs
    ]
    return Response({'logs': data})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def router_service_mode(request, pk):
    """Update service mode (hotspot/pppoe/both) for a router."""
    reseller = request.user.reseller
    try:
        router = Router.objects.get(pk=pk, reseller=reseller)
    except Router.DoesNotExist:
        return Response({'error': 'Router not found.'}, status=404)

    mode = request.data.get('service_mode', '')
    if mode not in ('hotspot', 'pppoe', 'both'):
        return Response({'error': 'Invalid service mode.'}, status=400)

    router.service_mode = mode
    router.save(update_fields=['service_mode'])
    return Response({'status': f'Service mode set to {router.get_service_mode_display()}.'})


# ── OpenWISP Webhook ────────────────────────────────────────────────────────


@api_view(['POST'])
@permission_classes([AllowAny])
def openwisp_webhook(request):
    """
    Receives device status change webhooks from OpenWISP monitoring.

    Configure in OpenWISP admin:
      Monitoring → Notification Settings → Webhook URL:
        https://app.sabiwifi.com/api/routers/openwisp-webhook/
      Secret header: X-OpenWISP-Signature with the value of settings.OPENWISP_WEBHOOK_SECRET

    Expected payload (OpenWISP sends JSON):
      {
        "event": "device_status_changed",
        "device": {
          "id": "<uuid>",
          "name": "<name>",
          "status": "ok" | "problem" | "unknown"
        }
      }
    """
    from django.conf import settings as django_settings
    from django.utils import timezone
    from routers.models import RouterHealthLog

    # Verify webhook signature (required)
    webhook_secret = getattr(django_settings, 'OPENWISP_WEBHOOK_SECRET', '')
    if not webhook_secret:
        logger.error('OPENWISP_WEBHOOK_SECRET not configured — rejecting webhook')
        return Response({'error': 'Webhook not configured.'}, status=500)
    incoming = request.META.get('HTTP_X_OPENWISP_SIGNATURE', '')
    if not incoming or incoming != webhook_secret:
        return Response({'error': 'Invalid signature.'}, status=403)

    data = request.data
    event = data.get('event', '')
    device_data = data.get('device', {})
    device_id = device_data.get('id', '')
    device_status = device_data.get('status', '')

    if not device_id or event != 'device_status_changed':
        return Response({'ok': True})

    # Find the router via OpenWISPBinding
    try:
        from integrations.models import OpenWISPBinding
        binding = OpenWISPBinding.objects.filter(
            openwisp_id=device_id,
            openwisp_type='device',
        ).select_related('router').first()
    except Exception as e:
        logger.warning(f'OpenWISP webhook: binding lookup failed: {e}')
        return Response({'ok': True})

    if not binding or not binding.router:
        return Response({'ok': True})

    router = binding.router
    now = timezone.now()

    if device_status == 'ok':
        was_offline = router.status == 'offline'
        router.status = 'online'
        router.last_seen = now
        router.offline_since = None
        router.save(update_fields=['status', 'last_seen', 'offline_since'])
        if was_offline:
            RouterHealthLog.objects.create(router=router, event='online')
            logger.info(f'Router {router.serial_number} back online via OpenWISP webhook')

    elif device_status in ('problem', 'unknown'):
        was_online = router.status in ('online', 'provisioned')
        router.status = 'offline'
        if not router.offline_since:
            router.offline_since = now
        router.save(update_fields=['status', 'offline_since'])
        if was_online:
            RouterHealthLog.objects.create(router=router, event='offline')
            logger.info(f'Router {router.serial_number} went offline via OpenWISP webhook')

    return Response({'ok': True})


# ── OpenWrt Phone-Home & Provision (deprecated — kept for bench-test devices) ─


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([ProvisionRateThrottle])
def openwrt_provision(request, mac):
    """
    Phone-home endpoint for OpenWrt routers.
    Called every 2 minutes by the baked-in phone-home script.
    MAC is the device identifier (equivalent to MikroTik serial).

    Headers (sent by the phone-home script):
      X-WG-Public-Key: the router's WireGuard public key (generated on first boot)

    Flow:
      1. Auto-register unknown MACs as 'available' OpenWrt devices
      2. Return '# not ready' until a reseller claims the device
      3. Once claimed, return full UCI provision script
    """
    if not getattr(settings, 'OPENWRT_ENABLED', False):
        return HttpResponse('# openwrt disabled', content_type='text/plain')

    mac = normalize_mac(mac)

    if not is_valid_mac(mac):
        return HttpResponse('# invalid mac', content_type='text/plain')

    from django.utils import timezone

    try:
        router = Router.objects.get(serial_number=mac)
    except Router.DoesNotExist:
        router, created = Router.objects.get_or_create(
            serial_number=mac,
            defaults={
                'status': 'available',
                'device_type': 'openwrt',
            },
        )
        if created:
            logger.info(f"Auto-registered OpenWrt device {mac} from phone-home")

    # Store the WG public key from the device (generated on first boot)
    wg_public_key = request.META.get('HTTP_X_WG_PUBLIC_KEY', '').strip()
    if wg_public_key and wg_public_key != router.wg_public_key:
        old_wg_pub = router.wg_public_key
        router.wg_public_key = wg_public_key
        router.save(update_fields=['wg_public_key'])
        # Update server-side WG peer (e.g. after router reflash generates new keys)
        if router.wg_tunnel_ip:
            try:
                if old_wg_pub:
                    remove_peer(old_wg_pub)
                add_peer(wg_public_key, router.wg_tunnel_ip)
            except WireGuardError as e:
                logger.error(f"Failed to update WG peer for OpenWrt {mac}: {e}")

    # Update last_seen
    Router.objects.filter(pk=router.pk).update(last_seen=timezone.now())

    if router.reseller is None:
        return HttpResponse('# not ready', content_type='text/plain')

    # Router is claimed — generate credentials if needed
    if not router.wg_tunnel_ip or not router.nas_secret:
        router.wg_tunnel_ip = _allocate_tunnel_ip()
        router.nas_secret = secrets.token_urlsafe(24)
        router.status = 'pending_provision'
        router.save()

        # Add WireGuard peer on server (using the key the device sent)
        if router.wg_public_key:
            try:
                add_peer(router.wg_public_key, router.wg_tunnel_ip)
            except WireGuardError as e:
                logger.error(f"Failed to add WG peer for OpenWrt {mac}: {e}")

        # Write NAS entry for FreeRADIUS and reload
        from radius.utils import upsert_nas_and_reload
        upsert_nas_and_reload(
            nasname=router.wg_tunnel_ip,
            shortname=mac,
            secret=router.nas_secret,
            description=f'SabiWiFi OpenWrt {mac}',
        )

    # Generate provision script
    from routers.openwrt_provision import generate_openwrt_provision
    script = generate_openwrt_provision(router)

    # Update status
    router.status = 'provisioned'
    router.provision_count += 1
    router.save(update_fields=['status', 'provision_count'])

    logger.info(f"OpenWrt provision delivered for {mac} (count: {router.provision_count})")

    return HttpResponse(script, content_type='text/plain')
