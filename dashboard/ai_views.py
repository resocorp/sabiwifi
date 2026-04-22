"""Reseller dashboard: AI configuration page.

Per-tenant settings for provider, API key (Fernet-encrypted on save), model,
capability toggles, and prompt overrides. Prompt edits write a new
AIPromptVersion row — the latest row per (config, agent_role) is "active".
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from ai.models import AIPromptVersion, ResellerAIConfig
from ai.safety import pause_ai, resume_ai
from notifications.notify import send_whatsapp


BOOL_CAPS = (
    'ai_enabled', 'sales_enabled', 'support_enabled', 'field_enabled',
    'auto_send_replies',
)
INT_CAPS = (
    'auto_quote_below_ngn',
    'max_auto_disconnects_per_hour',
    'max_outbound_per_customer_per_day',
)


def _get_reseller(request):
    if not hasattr(request.user, 'reseller'):
        return None
    return request.user.reseller


def _get_or_create_config(reseller):
    cfg, _ = ResellerAIConfig.objects.get_or_create(
        reseller=reseller,
        defaults={
            'text_provider': ResellerAIConfig.PROVIDER_ANTHROPIC,
            'capabilities': dict(ResellerAIConfig.DEFAULT_CAPABILITIES),
        },
    )
    return cfg


def _latest_prompts(cfg):
    """Return {role: body} of latest prompt overrides for each role."""
    out = {'sales': '', 'support': '', 'field': ''}
    seen = set()
    for v in (AIPromptVersion.objects
              .filter(config=cfg)
              .order_by('agent_role', '-created_at')):
        if v.agent_role in seen:
            continue
        seen.add(v.agent_role)
        out[v.agent_role] = v.body
    return out


@login_required
def ai_config(request):
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    cfg = _get_or_create_config(reseller)
    caps = {**ResellerAIConfig.DEFAULT_CAPABILITIES, **(cfg.capabilities or {})}
    return render(request, 'dashboard/ai_config.html', {
        'active_tab': 'ai',
        'reseller': reseller,
        'config': cfg,
        'caps': caps,
        'prompts': _latest_prompts(cfg),
        'provider_choices': ResellerAIConfig.PROVIDER_CHOICES,
        'wa_status': _wa_status(reseller),
    })


def _wa_status(reseller):
    try:
        sess = reseller.whatsapp_session
    except Exception:
        return {'connected': False, 'status': 'no_session', 'wa_phone': ''}
    return {
        'connected': sess.status == 'connected',
        'status': sess.status,
        'wa_phone': sess.wa_phone or '',
    }


@require_POST
@login_required
def ai_config_save(request):
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    cfg = _get_or_create_config(reseller)

    cfg.text_provider = request.POST.get('text_provider', cfg.text_provider)
    cfg.text_model = (request.POST.get('text_model') or '').strip()[:128]
    cfg.text_endpoint_url = (request.POST.get('text_endpoint_url') or '').strip()[:200]

    # API key only rotates if the user typed something — empty = keep existing.
    new_key = (request.POST.get('text_api_key') or '').strip()
    if new_key and new_key != '********':
        cfg.text_api_key = new_key

    # Capabilities
    caps = dict(cfg.capabilities or {})
    for key in BOOL_CAPS:
        caps[key] = request.POST.get(f'cap_{key}') == 'on'
    for key in INT_CAPS:
        raw = (request.POST.get(f'cap_{key}') or '').strip()
        if not raw:
            caps[key] = 0
            continue
        try:
            caps[key] = int(Decimal(raw))
        except (InvalidOperation, ValueError):
            caps[key] = 0
    cfg.capabilities = caps

    cfg.save()
    messages.success(request, 'AI configuration saved.')
    return redirect('dashboard-ai-config')


@require_POST
@login_required
def ai_prompt_save(request):
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    cfg = _get_or_create_config(reseller)
    role = request.POST.get('agent_role', '')
    body = (request.POST.get('body') or '').strip()
    note = (request.POST.get('note') or '').strip()[:255]

    if role not in ('sales', 'support', 'field'):
        messages.error(request, 'Unknown agent role.')
        return redirect('dashboard-ai-config')

    AIPromptVersion.objects.create(
        config=cfg, agent_role=role, body=body,
        edited_by=request.user, note=note,
    )
    messages.success(request, f'{role.title()} prompt updated.')
    return redirect('dashboard-ai-config')


@require_POST
@login_required
def ai_pause(request):
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')
    cfg = _get_or_create_config(reseller)
    pause_ai(cfg, f'reseller_manual:{request.user.username}')
    messages.success(request, 'AI paused for your account.')
    return redirect('dashboard-ai-config')


@require_POST
@login_required
def ai_test_whatsapp(request):
    """Send a one-off WhatsApp message to verify the sidecar is reachable
    and the reseller's session is connected. Does NOT touch the AI provider."""
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')

    phone = (request.POST.get('phone') or '').strip()
    body = (request.POST.get('body') or '').strip() or (
        f'SabiWiFi test message from {reseller.name}. '
        'If you see this, the WhatsApp sidecar is connected.'
    )
    if not phone:
        messages.error(request, 'Phone number is required.')
        return redirect('dashboard-ai-config')

    if not _wa_status(reseller)['connected']:
        messages.error(request,
                       'WhatsApp session is not connected. Link the session first '
                       'on the WhatsApp page, then retry the test.')
        return redirect('dashboard-ai-config')

    ok = send_whatsapp(reseller.slug, phone, body)
    if ok:
        messages.success(request,
                         f'Test message accepted by the WhatsApp sidecar (→ {phone}). '
                         'Check the recipient phone — delivery is async.')
    else:
        messages.error(request,
                       'WhatsApp sidecar rejected the send. Check '
                       'sabiwifi-whatsapp.service logs for the exact error.')
    return redirect('dashboard-ai-config')


@require_POST
@login_required
def ai_resume(request):
    reseller = _get_reseller(request)
    if not reseller:
        return redirect('login')
    cfg = _get_or_create_config(reseller)
    resume_ai(cfg)
    messages.success(request, 'AI resumed.')
    return redirect('dashboard-ai-config')
