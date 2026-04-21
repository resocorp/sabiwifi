"""Reseller dashboard AI-config page: roundtrip save + prompt version + pause."""
from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import Reseller
from ai.models import AIPromptVersion, ResellerAIConfig


def _reseller(slug='acme'):
    u = User.objects.create_user(username=f'{slug}-u', password='x')
    r = Reseller.objects.create(
        user=u, slug=slug, name=slug.title(), phone='+234800',
        paystack_subaccount_code='ACCT_x', payment_verified=True,
    )
    return u, r


class DashboardAIConfigTest(TestCase):
    def test_get_auto_creates_config_and_renders(self):
        u, r = _reseller()
        self.client.force_login(u)
        resp = self.client.get('/dashboard/ai/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(ResellerAIConfig.objects.filter(reseller=r).exists())

    def test_save_persists_caps_and_rotates_key(self):
        u, r = _reseller(slug='bravo')
        self.client.force_login(u)
        # Prime the config so we can check key preservation.
        self.client.get('/dashboard/ai/')

        resp = self.client.post('/dashboard/ai/save/', {
            'text_provider': 'anthropic',
            'text_model': 'claude-sonnet-4-6',
            'text_api_key': 'sk-live-real-key',
            'cap_ai_enabled': 'on',
            'cap_sales_enabled': 'on',
            'cap_auto_quote_below_ngn': '25000',
            'cap_max_outbound_per_customer_per_day': '5',
        })
        self.assertEqual(resp.status_code, 302)

        cfg = ResellerAIConfig.objects.get(reseller=r)
        self.assertEqual(cfg.text_model, 'claude-sonnet-4-6')
        self.assertTrue(cfg.capabilities['ai_enabled'])
        self.assertTrue(cfg.capabilities['sales_enabled'])
        self.assertFalse(cfg.capabilities['support_enabled'])
        self.assertEqual(cfg.capabilities['auto_quote_below_ngn'], 25000)
        self.assertEqual(cfg.text_api_key, 'sk-live-real-key')

        # Placeholder '********' must NOT overwrite the real key.
        self.client.post('/dashboard/ai/save/', {
            'text_provider': 'anthropic',
            'text_model': 'claude-sonnet-4-6',
            'text_api_key': '********',
            'cap_ai_enabled': 'on',
        })
        cfg.refresh_from_db()
        self.assertEqual(cfg.text_api_key, 'sk-live-real-key')

    def test_prompt_save_appends_version(self):
        u, r = _reseller(slug='charlie')
        self.client.force_login(u)
        self.client.get('/dashboard/ai/')
        self.client.post('/dashboard/ai/prompts/', {
            'agent_role': 'sales',
            'body': 'Always greet in Pidgin.',
            'note': 'initial',
        })
        self.client.post('/dashboard/ai/prompts/', {
            'agent_role': 'sales',
            'body': 'Always greet in English.',
            'note': 'revert',
        })
        versions = AIPromptVersion.objects.filter(
            config__reseller=r, agent_role='sales').order_by('-created_at')
        self.assertEqual(versions.count(), 2)
        self.assertEqual(versions.first().body, 'Always greet in English.')

    def test_pause_and_resume_from_dashboard(self):
        u, r = _reseller(slug='delta')
        self.client.force_login(u)
        self.client.get('/dashboard/ai/')
        self.client.post('/dashboard/ai/pause/')
        cfg = ResellerAIConfig.objects.get(reseller=r)
        self.assertIsNotNone(cfg.ai_paused_at)
        self.client.post('/dashboard/ai/resume/')
        cfg.refresh_from_db()
        self.assertIsNone(cfg.ai_paused_at)
