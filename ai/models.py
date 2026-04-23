"""Models for per-reseller AI configuration, agent runs, and prompt versioning.

- ResellerAIConfig: one row per reseller. API keys Fernet-encrypted at rest
  via ai.crypto. Access via the plaintext `text_api_key` / `asr_api_key`
  properties — never read the `_encrypted` columns directly.
- AIAgentRun: append-only audit of every LLM turn for replay + cost roll-up.
- AIPromptVersion: append-only log of prompt-override edits. The "current"
  prompt is the latest row for (config, agent_role).
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from ai.crypto import decrypt, encrypt


class ResellerAIConfig(models.Model):
    PROVIDER_ANTHROPIC = 'anthropic'
    PROVIDER_OPENAI = 'openai'
    PROVIDER_GEMINI = 'gemini'
    PROVIDER_OPENAI_COMPAT = 'openai_compatible'
    PROVIDER_CHOICES = [
        (PROVIDER_ANTHROPIC, 'Anthropic'),
        (PROVIDER_OPENAI, 'OpenAI'),
        (PROVIDER_GEMINI, 'Gemini'),
        (PROVIDER_OPENAI_COMPAT, 'OpenAI-compatible (Groq/Together/Ollama)'),
    ]

    ASR_WHISPER = 'whisper'
    ASR_DEEPGRAM = 'deepgram'
    ASR_GOOGLE = 'google'
    ASR_CHOICES = [
        ('', '— none —'),
        (ASR_WHISPER, 'Whisper'),
        (ASR_DEEPGRAM, 'Deepgram'),
        (ASR_GOOGLE, 'Google'),
    ]

    DEFAULT_CAPABILITIES = {
        'ai_enabled': False,
        'customer_enabled': False,
        # sales_enabled / support_enabled kept for back-compat — a reseller
        # that had either one set before the CustomerAgent merge is treated as
        # having the customer flow on (see is_agent_enabled).
        'sales_enabled': False,
        'support_enabled': False,
        'field_enabled': False,
        'auto_send_replies': False,
        'auto_quote_below_ngn': 0,
        'max_auto_disconnects_per_hour': 5,
        'max_outbound_per_customer_per_day': 4,
        # Field-tech follow-up cadence: first ping after this many minutes,
        # subsequent pings hourly, capped by max_field_pings before operator
        # escalation.
        'cap_field_ping_minutes': 30,
        'max_field_pings': 4,
    }

    reseller = models.OneToOneField(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='ai_config',
    )

    text_provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES,
                                     default=PROVIDER_ANTHROPIC)
    text_api_key_encrypted = models.TextField(blank=True)
    text_endpoint_url = models.URLField(blank=True,
                                        help_text='Only for openai_compatible (Groq / Together / Ollama URL).')
    text_model = models.CharField(max_length=128, blank=True,
                                  help_text='e.g. claude-sonnet-4-6, gpt-4o, gemini-2.0-flash.')

    asr_provider = models.CharField(max_length=32, choices=ASR_CHOICES, blank=True, default='')
    asr_api_key_encrypted = models.TextField(blank=True)
    tts_provider = models.CharField(max_length=32, blank=True, default='')

    capabilities = models.JSONField(default=dict, blank=True)
    prompt_overrides = models.JSONField(default=dict, blank=True,
                                        help_text='Active tone / FAQ / guardrails. History in AIPromptVersion.')

    # Circuit-breaker state — flipped by safety rails, visible to operator.
    ai_paused_at = models.DateTimeField(null=True, blank=True)
    ai_pause_reason = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Reseller AI config'

    def __str__(self):
        return f'AIConfig<{self.reseller.slug}:{self.text_provider}>'

    @property
    def text_api_key(self) -> str:
        return decrypt(self.text_api_key_encrypted)

    @text_api_key.setter
    def text_api_key(self, value: str) -> None:
        self.text_api_key_encrypted = encrypt(value or '')

    @property
    def asr_api_key(self) -> str:
        return decrypt(self.asr_api_key_encrypted)

    @asr_api_key.setter
    def asr_api_key(self, value: str) -> None:
        self.asr_api_key_encrypted = encrypt(value or '')

    def cap(self, key, default=None):
        if not isinstance(self.capabilities, dict):
            return default
        if key in self.capabilities:
            return self.capabilities[key]
        return self.DEFAULT_CAPABILITIES.get(key, default)

    def is_agent_enabled(self, role: str) -> bool:
        if self.ai_paused_at:
            return False
        if not self.cap('ai_enabled'):
            return False
        if role == 'customer' and 'customer_enabled' not in (self.capabilities or {}):
            # Back-compat: pre-merge resellers didn't set customer_enabled.
            # Treat either legacy flag as opting the customer flow in.
            return bool(self.cap('sales_enabled')) or bool(self.cap('support_enabled'))
        return bool(self.cap(f'{role}_enabled'))


class AIPromptVersion(models.Model):
    # ROLE_SALES / ROLE_SUPPORT kept for historical rows. New prompt overrides
    # for the customer-facing flow use ROLE_CUSTOMER.
    ROLE_SALES = 'sales'
    ROLE_SUPPORT = 'support'
    ROLE_CUSTOMER = 'customer'
    ROLE_FIELD = 'field'
    ROLE_FIELD_INBOUND = 'field_inbound'
    ROLE_CHOICES = [
        (ROLE_CUSTOMER, 'Customer (enquiry + support)'),
        (ROLE_SALES, 'Sales (legacy)'),
        (ROLE_SUPPORT, 'Support (legacy)'),
        (ROLE_FIELD, 'Field supervisor'),
        (ROLE_FIELD_INBOUND, 'Field inbound (tech replies)'),
    ]

    config = models.ForeignKey(ResellerAIConfig, on_delete=models.CASCADE,
                               related_name='prompt_versions')
    agent_role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    body = models.TextField()
    edited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL)
    note = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['config', 'agent_role', '-created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'PromptV<{self.config.reseller.slug}:{self.agent_role}@{self.created_at:%Y-%m-%d}>'


class AIAgentRun(models.Model):
    # ROLE_SALES / ROLE_SUPPORT kept for historical rows. New runs emit
    # ROLE_CUSTOMER.
    ROLE_SALES = 'sales'
    ROLE_SUPPORT = 'support'
    ROLE_CUSTOMER = 'customer'
    ROLE_FIELD = 'field'
    ROLE_FIELD_INBOUND = 'field_inbound'
    ROLE_CHOICES = [
        (ROLE_CUSTOMER, 'Customer (enquiry + support)'),
        (ROLE_SALES, 'Sales (legacy)'),
        (ROLE_SUPPORT, 'Support (legacy)'),
        (ROLE_FIELD, 'Field supervisor'),
        (ROLE_FIELD_INBOUND, 'Field inbound (tech replies)'),
    ]

    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_AWAITING_APPROVAL = 'awaiting_approval'
    STATUS_HUMAN_OVERRIDDEN = 'human_overridden'
    STATUS_CHOICES = [
        (STATUS_SUCCESS, 'Success'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_AWAITING_APPROVAL, 'Awaiting approval'),
        (STATUS_HUMAN_OVERRIDDEN, 'Human overridden'),
    ]

    reseller = models.ForeignKey('accounts.Reseller', on_delete=models.CASCADE,
                                 related_name='ai_runs')
    conversation = models.ForeignKey('conversations.Conversation', null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name='ai_runs')
    message = models.ForeignKey('conversations.Message', null=True, blank=True,
                                on_delete=models.SET_NULL, related_name='ai_runs')
    ticket = models.ForeignKey('tickets.Ticket', null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='ai_runs')

    agent_role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=128, blank=True, default='')

    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    cost_ngn = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))

    inputs = models.JSONField(default=dict, blank=True)
    outputs = models.JSONField(default=dict, blank=True)
    tool_calls = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_SUCCESS)
    error_message = models.CharField(max_length=500, blank=True, default='')

    human_reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                          on_delete=models.SET_NULL,
                                          related_name='reviewed_ai_runs')
    reviewed_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['reseller', '-started_at']),
            models.Index(fields=['agent_role', 'status']),
            models.Index(fields=['status', '-started_at']),
        ]
        ordering = ['-started_at']

    def __str__(self):
        return f'AIRun<{self.reseller.slug}:{self.agent_role}:{self.status}>'
