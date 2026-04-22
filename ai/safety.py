"""Per-reseller safety rails for AI runs.

Gates that live here rather than in tools.py because they read durable state
across multiple runs (error rate, daily outbound count) and are the right
place for the operator's kill switch.

Nothing here catches provider exceptions — the runner already converts those
to AIAgentRun.status='failed' rows. The circuit-breaker watches that audit
log rolling-window and flips `ResellerAIConfig.ai_paused_at` when the failure
rate exceeds a threshold.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from ai.models import AIAgentRun, ResellerAIConfig
from conversations.models import Conversation, Message


CIRCUIT_WINDOW_MINUTES = 15
CIRCUIT_MIN_RUNS = 5
CIRCUIT_ERROR_RATIO = 0.5  # flip if >=50% of recent runs failed


def pause_on_provider_config_error(config: ResellerAIConfig, error_message: str) -> bool:
    """Pause AI immediately if the provider returned a permanent config error
    (auth / wrong model / etc) rather than waiting for the rolling-window
    breaker. Reasoning: a 401/403/404 will not fix itself — every queued
    customer message would just burn through retries until the breaker
    eventually trips, while the customer gets nothing.

    Returns True if the breaker was just flipped by this call.
    """
    if config.ai_paused_at:
        return False
    msg = (error_message or '').lower()
    # Match: 'HTTPError: 401', '404 Client Error', '403 Forbidden',
    # explicit 'authentication' / 'invalid api key' / 'model not found'.
    triggers = (
        'httperror: 401', 'httperror: 403', 'httperror: 404',
        '401 client error', '403 client error', '404 client error',
        'invalid api key', 'invalid_api_key', 'authentication',
        'model not found', 'model_not_found', 'unknown model',
    )
    if not any(t in msg for t in triggers):
        return False
    # Trim to the first useful chunk of the upstream message — keeps the
    # banner readable in the dashboard.
    short = (error_message or '').strip().split('\n')[0][:200]
    config.ai_paused_at = timezone.now()
    config.ai_pause_reason = f'provider_config_error: {short}'
    config.save(update_fields=['ai_paused_at', 'ai_pause_reason', 'updated_at'])
    return True


def trip_circuit_breaker(config: ResellerAIConfig) -> bool:
    """Inspect the recent run window and pause AI for this reseller if the
    error rate is above threshold. Returns True if the breaker tripped."""
    since = timezone.now() - timedelta(minutes=CIRCUIT_WINDOW_MINUTES)
    agg = (AIAgentRun.objects.filter(reseller=config.reseller, started_at__gte=since)
           .aggregate(total=Count('id'),
                      failed=Count('id', filter=Q(status=AIAgentRun.STATUS_FAILED))))
    total = agg['total'] or 0
    failed = agg['failed'] or 0
    if total < CIRCUIT_MIN_RUNS:
        return False
    if failed / total < CIRCUIT_ERROR_RATIO:
        return False
    if config.ai_paused_at:
        return False
    config.ai_paused_at = timezone.now()
    config.ai_pause_reason = f'circuit_breaker: {failed}/{total} failed in {CIRCUIT_WINDOW_MINUTES}m'
    config.save(update_fields=['ai_paused_at', 'ai_pause_reason', 'updated_at'])
    return True


def outbound_today_count(conversation: Conversation) -> int:
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return Message.objects.filter(
        conversation=conversation,
        direction=Message.DIRECTION_OUT,
        created_at__gte=start,
        source__in=(Message.SOURCE_AI_SALES, Message.SOURCE_AI_SUPPORT, Message.SOURCE_AI_FIELD),
    ).count()


def outbound_cap_reached(config: ResellerAIConfig, conversation: Conversation) -> bool:
    cap = int(config.cap('max_outbound_per_customer_per_day', 0) or 0)
    if cap <= 0:
        return False
    return outbound_today_count(conversation) >= cap


def pause_ai(config: ResellerAIConfig, reason: str) -> None:
    config.ai_paused_at = timezone.now()
    config.ai_pause_reason = reason[:255]
    config.save(update_fields=['ai_paused_at', 'ai_pause_reason', 'updated_at'])


def resume_ai(config: ResellerAIConfig) -> None:
    config.ai_paused_at = None
    config.ai_pause_reason = ''
    config.save(update_fields=['ai_paused_at', 'ai_pause_reason', 'updated_at'])
