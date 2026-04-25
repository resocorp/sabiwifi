"""Reseller dashboard: voice (AVR hotline) configuration.

Phase 2 scope: master toggle, greeting text, recording opt-in, concurrent-
call cap, and the voice_customer prompt override. DID management and
outbound/renewal settings ship in Phase 3.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import require_cap
from ai.models import AIPromptVersion, ResellerAIConfig
from voice.models import RoutingRule, VoiceCall, VoiceTenant


def _get_or_create_tenant(reseller) -> VoiceTenant:
    tenant, _ = VoiceTenant.objects.get_or_create(
        reseller=reseller,
        defaults={'enabled_flags': dict(VoiceTenant.DEFAULT_FLAGS)},
    )
    return tenant


def _latest_voice_prompt(reseller) -> str:
    try:
        cfg = reseller.ai_config
    except ResellerAIConfig.DoesNotExist:
        return ''
    row = (AIPromptVersion.objects
           .filter(config=cfg, agent_role=AIPromptVersion.ROLE_VOICE_CUSTOMER)
           .order_by('-created_at').first())
    return row.body if row else ''


@login_required
@require_cap('ai_config')
def voice_config(request):
    reseller = request.effective_reseller
    if not reseller:
        return redirect('login')

    tenant = _get_or_create_tenant(reseller)
    flags = {**VoiceTenant.DEFAULT_FLAGS, **(tenant.enabled_flags or {})}

    recent_calls = (VoiceCall.objects
                    .filter(reseller=reseller)
                    .order_by('-started_at')[:20])
    routing_rules = RoutingRule.objects.filter(reseller=reseller, is_active=True)

    return render(request, 'dashboard/voice_config.html', {
        'active_tab': 'voice',
        'reseller': reseller,
        'tenant': tenant,
        'flags': flags,
        'voice_prompt': _latest_voice_prompt(reseller),
        'recent_calls': recent_calls,
        'routing_rules': routing_rules,
    })


@require_POST
@login_required
@require_cap('ai_config')
def voice_config_save(request):
    reseller = request.effective_reseller
    if not reseller:
        return redirect('login')

    tenant = _get_or_create_tenant(reseller)

    tenant.voice_enabled = request.POST.get('voice_enabled') == 'on'
    tenant.greeting_text = (request.POST.get('greeting_text') or '').strip()[:500]
    tenant.recording_enabled = request.POST.get('recording_enabled') == 'on'
    tenant.tts_voice_id = (request.POST.get('tts_voice_id') or '').strip()[:64]

    # Concurrent-call cap: sanitise to a positive int (0 = no cap).
    raw_cap = (request.POST.get('concurrent_call_cap') or '').strip()
    try:
        tenant.concurrent_call_cap = max(0, int(raw_cap)) if raw_cap else 5
    except ValueError:
        tenant.concurrent_call_cap = 5

    # Retention: 1-365 days window. Below 1 disables retention tracking; above
    # 365 is flagged to prevent accidental NDPR exposure on opt-in recordings.
    raw_ret = (request.POST.get('recording_retention_days') or '').strip()
    try:
        ret = int(raw_ret) if raw_ret else 30
    except ValueError:
        ret = 30
    tenant.recording_retention_days = max(1, min(365, ret))

    # Per-feature flags
    flags = dict(tenant.enabled_flags or {})
    for key in VoiceTenant.DEFAULT_FLAGS:
        flags[key] = request.POST.get(f'flag_{key}') == 'on'
    tenant.enabled_flags = flags

    tenant.save()
    messages.success(request, 'Voice settings saved.')
    return redirect('dashboard-voice-config')


@require_POST
@login_required
@require_cap('ai_config')
def voice_prompt_save(request):
    reseller = request.effective_reseller
    if not reseller:
        return redirect('login')

    try:
        cfg = reseller.ai_config
    except ResellerAIConfig.DoesNotExist:
        messages.error(request, 'Set up your AI provider under AI Config first.')
        return redirect('dashboard-voice-config')

    body = (request.POST.get('voice_prompt') or '').strip()
    if not body:
        messages.error(request, 'Prompt cannot be empty.')
        return redirect('dashboard-voice-config')

    AIPromptVersion.objects.create(
        config=cfg,
        agent_role=AIPromptVersion.ROLE_VOICE_CUSTOMER,
        body=body[:8000],
        edited_by=request.user,
        note='voice_customer override from dashboard',
    )
    messages.success(request, 'Voice prompt saved.')
    return redirect('dashboard-voice-config')
