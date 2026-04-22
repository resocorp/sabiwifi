"""Tests for the immediate-pause-on-provider-config-error rail."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import Reseller
from ai.models import AIAgentRun, ResellerAIConfig
from ai.safety import pause_on_provider_config_error  # noqa: F401


def _r(slug='acme'):
    user = User.objects.create_user(username=f'{slug}-owner', password='x')
    return Reseller.objects.create(
        user=user, slug=slug, name=slug.title(), phone='+2348000000000',
        paystack_subaccount_code='ACCT_t', payment_verified=True,
    )


def _cfg(reseller, *, paused=False):
    cfg = ResellerAIConfig.objects.create(
        reseller=reseller, text_provider=ResellerAIConfig.PROVIDER_ANTHROPIC,
        text_model='whatever',
        capabilities={'ai_enabled': True, 'sales_enabled': True},
    )
    cfg.text_api_key = 'sk-x'
    if paused:
        from django.utils import timezone
        cfg.ai_paused_at = timezone.now()
        cfg.ai_pause_reason = 'pre-existing'
    cfg.save()
    return cfg


class PauseOnProviderConfigErrorTest(TestCase):
    def test_404_pauses_immediately(self):
        cfg = _cfg(_r())
        flipped = pause_on_provider_config_error(
            cfg, 'HTTPError: 404 Client Error: Not Found for url: …/v1/messages')
        self.assertTrue(flipped)
        cfg.refresh_from_db()
        self.assertIsNotNone(cfg.ai_paused_at)
        self.assertIn('provider_config_error', cfg.ai_pause_reason)

    def test_401_pauses_immediately(self):
        cfg = _cfg(_r('a2'))
        self.assertTrue(pause_on_provider_config_error(
            cfg, 'HTTPError: 401 Client Error: Unauthorized'))
        cfg.refresh_from_db()
        self.assertIsNotNone(cfg.ai_paused_at)

    def test_invalid_api_key_text_pauses(self):
        cfg = _cfg(_r('a3'))
        self.assertTrue(pause_on_provider_config_error(
            cfg, 'BadRequest: invalid_api_key passed in header'))

    def test_500_does_not_pause(self):
        cfg = _cfg(_r('a4'))
        self.assertFalse(pause_on_provider_config_error(
            cfg, 'HTTPError: 500 Server Error: Internal Server Error'))
        cfg.refresh_from_db()
        self.assertIsNone(cfg.ai_paused_at)

    def test_already_paused_does_not_re_flip(self):
        cfg = _cfg(_r('a5'), paused=True)
        original_reason = cfg.ai_pause_reason
        original_ts = cfg.ai_paused_at
        self.assertFalse(pause_on_provider_config_error(
            cfg, 'HTTPError: 404 Client Error'))
        cfg.refresh_from_db()
        # Did not overwrite — keeps the original pause context
        self.assertEqual(cfg.ai_pause_reason, original_reason)
        self.assertEqual(cfg.ai_paused_at, original_ts)


class RunnerInvokesImmediatePauseTest(TestCase):
    """End-to-end: a 404 on the very first run trips pause without waiting
    for the rolling-window circuit breaker (which needs 5 runs)."""

    def test_first_404_pauses_ai(self):
        from conversations.models import Conversation
        from conversations.services import record_inbound_message
        from ai.providers.base import LLMProvider
        from ai.jobs import run_sales_agent

        reseller = _r('rfail')
        cfg = _cfg(reseller)

        msg = record_inbound_message(
            reseller=reseller, channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id='2348011112222@s.whatsapp.net',
            body='hi', attachments=[], external_message_id='WA-PAUSE-1',
            sender_phone='2348011112222',
        )

        class _BoomProvider:
            def chat(self, *, system, messages, tools=None, max_tokens=1024, temperature=0.3):
                from requests import HTTPError
                raise HTTPError('404 Client Error: Not Found for url: …/v1/messages')

        with patch('ai.agents.runner.get_provider', return_value=_BoomProvider()):
            run_sales_agent(msg.pk)

        cfg.refresh_from_db()
        self.assertIsNotNone(cfg.ai_paused_at, 'AI should be paused after the first 404')
        self.assertIn('provider_config_error', cfg.ai_pause_reason)
        # The run itself was recorded as failed
        run = AIAgentRun.objects.get(reseller=reseller)
        self.assertEqual(run.status, AIAgentRun.STATUS_FAILED)
