"""Voice / telephony models for Phase 2 hotline.

Per-reseller telephony config + per-call audit rows. Mirrors existing patterns:
  - VoiceTenant ↔ WhatsappSession / ResellerAIConfig (OneToOne → Reseller)
  - VoiceCall   ↔ Conversation (channel=CHANNEL_VOICE) + AIAgentRun
  - VoiceTurn   ↔ AIAgentRun per-turn audit
  - RoutingRule maps an inbound DID to the tenant + prompt role to run.

SIP credentials reuse the Fernet helper from ai.crypto — same key, same
encrypt/decrypt API used for AI provider keys on ResellerAIConfig.
"""
from __future__ import annotations

import json
from decimal import Decimal

from django.contrib.postgres.fields import ArrayField
from django.db import models

from ai.crypto import decrypt, encrypt


# ---------------------------------------------------------------------------
# Tenant-level configuration
# ---------------------------------------------------------------------------

class VoiceTenant(models.Model):
    """Per-reseller voice configuration. One row per reseller who opts in."""

    DEFAULT_FLAGS = {
        'hotline': False,          # inbound AI support hotline (Phase 2)
        'outbound_renewal': False, # outbound expiry/renewal calls (Phase 3)
        'otp_fallback': False,     # voice-OTP when SMS fails (Phase 1)
        'broadcast': False,        # bulk voice broadcasts (Phase 5)
    }

    reseller = models.OneToOneField(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='voice_tenant',
    )
    voice_enabled = models.BooleanField(
        default=False,
        help_text='Master toggle. When False the voice droplet routes all '
                  'calls for this tenant to a "temporarily unavailable" prompt.',
    )

    # SIP trunk binding — one tenant can share the platform trunk or bring
    # their own. For Phase 2 pilot every tenant shares the platform trunk and
    # this field holds the display name only.
    sip_provider_name = models.CharField(
        max_length=64, blank=True, default='',
        help_text='Display-only (e.g. "VoIP.ms", "MTN"). SIP creds live in '
                  'sip_credentials_encrypted; empty means use platform trunk.',
    )
    sip_credentials_encrypted = models.TextField(
        blank=True, default='',
        help_text='Fernet-encrypted JSON: {host, username, password, port}. '
                  'Access via the sip_credentials property.',
    )

    # Caller-ID for outbound calls. Should be one of the tenant's own DIDs
    # (any RoutingRule.did_e164 that points at this reseller).
    outbound_caller_id = models.CharField(
        max_length=20, blank=True, default='',
        help_text='E.164 number used as From: for outbound calls.',
    )

    # TTS voice customisation. Provider-specific id (Cartesia voice_id,
    # ElevenLabs voice name, Google voice code) — interpreted by the AVR
    # container. Blank = use platform default voice.
    tts_voice_id = models.CharField(max_length=64, blank=True, default='')

    # Static greeting the caller hears before the AI engages. Keeping a
    # static greeting separate from the LLM system prompt keeps the first
    # ~300 ms of every call predictable and cheap (no LLM latency before
    # the hello).
    greeting_text = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Spoken before any AI turn. Leave blank for a platform-'
                  'generated greeting using reseller.name.',
    )

    # Call recording — NDPR compliance: must be opt-in, must disclose at
    # call start. The voice droplet's dialplan plays a "this call may be
    # recorded" TTS when recording_enabled is True.
    recording_enabled = models.BooleanField(default=False)
    recording_retention_days = models.PositiveIntegerField(
        default=30,
        help_text='Days to retain recordings + transcripts. Longer = more '
                  'storage cost + NDPR exposure.',
    )

    # Concurrency cap: prevents one tenant's marketing blast from saturating
    # the SIP trunk and starving another tenant's support hotline. Enforced
    # by the voice droplet before it rings a call. 0 = no cap.
    concurrent_call_cap = models.PositiveIntegerField(default=5)

    enabled_flags = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Voice tenant'

    def __str__(self):
        return f'VoiceTenant<{self.reseller.slug}:{"on" if self.voice_enabled else "off"}>'

    # --- SIP credential crypto ------------------------------------------------

    @property
    def sip_credentials(self) -> dict:
        """Return decrypted SIP creds as a dict, {} if empty."""
        raw = decrypt(self.sip_credentials_encrypted) if self.sip_credentials_encrypted else ''
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    @sip_credentials.setter
    def sip_credentials(self, creds: dict | None) -> None:
        if not creds:
            self.sip_credentials_encrypted = ''
            return
        self.sip_credentials_encrypted = encrypt(json.dumps(creds))

    # --- Flag helpers ---------------------------------------------------------

    def flag(self, key: str, default: bool = False) -> bool:
        flags = self.enabled_flags if isinstance(self.enabled_flags, dict) else {}
        if key in flags:
            return bool(flags[key])
        return bool(self.DEFAULT_FLAGS.get(key, default))


# ---------------------------------------------------------------------------
# DID → tenant routing
# ---------------------------------------------------------------------------

class RoutingRule(models.Model):
    """Maps an inbound DID to a reseller + the agent role to invoke.

    Asterisk dialplan is regenerated from these rows on save (Phase 2.5).
    Until then, rows are documented state only — the pjsip config is edited
    by hand when a DID is provisioned.
    """
    ROLE_VOICE_CUSTOMER = 'voice_customer'
    ROLE_VOICE_FIELD = 'voice_field'
    ROLE_VOICE_RESELLER = 'voice_reseller'
    ROLE_VOICE_OTP = 'voice_otp'  # non-AI, TTS-only
    ROLE_CHOICES = [
        (ROLE_VOICE_CUSTOMER, 'Voice — subscriber hotline'),
        (ROLE_VOICE_FIELD,    'Voice — field-tech callback'),
        (ROLE_VOICE_RESELLER, 'Voice — reseller ops support'),
        (ROLE_VOICE_OTP,      'Voice — OTP delivery only (no AI)'),
    ]

    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='voice_routing_rules',
    )
    did_e164 = models.CharField(
        max_length=20, unique=True,
        help_text='The DID the caller dials, E.164 format (+23480...).',
    )
    agent_role = models.CharField(
        max_length=32, choices=ROLE_CHOICES, default=ROLE_VOICE_CUSTOMER,
    )
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['reseller', 'is_active']),
        ]

    def __str__(self):
        return f'{self.did_e164} → {self.reseller.slug}/{self.agent_role}'


# ---------------------------------------------------------------------------
# Per-call audit
# ---------------------------------------------------------------------------

class VoiceCall(models.Model):
    """One row per call — inbound or outbound. Mirrors AIAgentRun granularity
    at the CALL level; VoiceTurn below captures per-turn granularity.
    """
    DIRECTION_IN = 'inbound'
    DIRECTION_OUT = 'outbound'
    DIRECTION_CHOICES = [
        (DIRECTION_IN, 'Inbound'),
        (DIRECTION_OUT, 'Outbound'),
    ]

    STATUS_RINGING = 'ringing'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_NO_ANSWER = 'no_answer'
    STATUS_BUSY = 'busy'
    STATUS_CHOICES = [
        (STATUS_RINGING, 'Ringing'),
        (STATUS_IN_PROGRESS, 'In progress'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_NO_ANSWER, 'No answer'),
        (STATUS_BUSY, 'Busy'),
    ]

    reseller = models.ForeignKey(
        'accounts.Reseller', on_delete=models.CASCADE, related_name='voice_calls',
    )
    # A call is the first class thread for voice — a Conversation row is
    # opened lazily when the caller utters anything (non-OTP calls). OTP
    # calls finish without ever creating a Conversation.
    conversation = models.ForeignKey(
        'conversations.Conversation', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='voice_calls',
    )
    subscriber = models.ForeignKey(
        'accounts.Subscriber', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='voice_calls',
        help_text='Resolved once caller-ID is matched against subscribers; '
                  'nullable for new/anonymous callers.',
    )
    ticket = models.ForeignKey(
        'tickets.Ticket', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='voice_calls',
    )

    call_id = models.CharField(
        max_length=128, unique=True,
        help_text='Asterisk uniqueid / AVR UUID. Dedupes webhook events.',
    )
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)

    from_e164 = models.CharField(max_length=20, blank=True, default='')
    to_e164 = models.CharField(max_length=20, blank=True, default='')

    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_RINGING,
    )
    failure_reason = models.CharField(max_length=255, blank=True, default='')

    started_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)

    recording_url = models.URLField(blank=True, default='')
    transcript = models.TextField(
        blank=True, default='',
        help_text='Full call transcript accumulated from VoiceTurn rows.',
    )

    cost_ngn = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal('0'),
        help_text='Sum of carrier + AI costs for this call.',
    )

    # Unstructured provider data (SIP headers, caller location hints, etc.)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['reseller', '-started_at']),
            models.Index(fields=['subscriber', '-started_at']),
            models.Index(fields=['status', '-started_at']),
        ]

    def __str__(self):
        return (f'VoiceCall<{self.direction}:{self.from_e164}→{self.to_e164}'
                f' {self.status}>')


class VoiceTurn(models.Model):
    """Per-turn row for one VoiceCall. Inbound turns are transcribed caller
    speech; outbound turns are the AI's synthesized reply. The agent_run_id
    links to the AIAgentRun that produced an outbound turn (nullable for
    inbound turns and for OTP plays)."""

    DIRECTION_IN = 'in'
    DIRECTION_OUT = 'out'
    DIRECTION_CHOICES = [
        (DIRECTION_IN, 'Inbound (caller speech)'),
        (DIRECTION_OUT, 'Outbound (AI speech)'),
    ]

    call = models.ForeignKey(
        VoiceCall, on_delete=models.CASCADE, related_name='turns',
    )
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES)
    text = models.TextField(blank=True, default='')

    # Link back to the LLM run that generated an outbound turn. BigIntegerField
    # (not FK) because AIAgentRun is in the ai app and we want the voice app
    # to stay optional for test harnesses.
    agent_run_id = models.BigIntegerField(null=True, blank=True)

    # Timing — first-byte latency for streaming, total otherwise.
    started_at = models.DateTimeField(auto_now_add=True)
    latency_ms = models.IntegerField(null=True, blank=True)

    cost_ngn = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal('0'),
    )

    class Meta:
        ordering = ['call', 'started_at']
        indexes = [
            models.Index(fields=['call', 'started_at']),
        ]

    def __str__(self):
        preview = (self.text[:50] + '…') if len(self.text) > 50 else self.text
        return f'VoiceTurn<{self.direction}:{preview}>'
