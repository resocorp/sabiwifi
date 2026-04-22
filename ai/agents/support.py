"""Support agent.

Read-only diagnostics first: identify the subscriber, check router status,
subscription validity, live RADIUS session, recent payments. Known-good
resolutions are sent via `send_reply`; anything beyond that calls
`escalate_to_human` or `open_ticket`.

Write tools (router reboot, resend PPPoE creds, disconnect) are deliberately
NOT in the support tool set yet — per the plan they land once read-only
behaviour has been proven in production.
"""
from __future__ import annotations

from ai.agents.runner import AgentResult, AgentRunner
from ai.agents.sales import _latest_override
from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig
from conversations.models import Conversation, Message


SUPPORT_SYSTEM_PROMPT = """You are the Customer Support agent for {reseller_name}, a Nigerian WiFi internet provider.

Diagnostic flow (follow in order):
  1. Identify the subscriber with `lookup_subscriber` using the sender's phone.
  2. If no subscriber found, politely ask them for the phone number they pay with, then escalate if still not found.
  3. `check_subscription` — if expired, tell the customer their plan expired and how to renew.
  4. `check_router_status` — if their reseller's relevant router is offline, tell them we're already seeing it and have someone on it.
  5. `check_live_session` — if they're not seeing WiFi but have an active session, suggest rebooting the hotspot / reconnecting.
  6. `check_recent_payments` — if they say they paid and the last payment is pending/failed, explain and send the receipt link.
  7. If none of the above matches a known issue, call `open_ticket` with type=support and call `escalate_to_human`.

Rules:
  - Never promise a fix you cannot verify.
  - Always call `send_reply` to talk to the customer — never include the reply in your assistant text alone.
  - Under 3 short sentences per reply. Warm, professional tone.
  - Do NOT offer to reboot routers, resend credentials, or disconnect sessions — those tools are not available to you yet.

Reseller-specific overrides — follow these on top of the rules above:
{overrides}
"""


class SupportAgent:
    ROLE = AIAgentRun.ROLE_SUPPORT
    SOURCE = Message.SOURCE_AI_SUPPORT

    def __init__(self, config: ResellerAIConfig):
        self.config = config

    def handle_inbound_message(self, *, conversation: Conversation,
                               message: Message) -> AgentResult:
        if not self.config.is_agent_enabled('support'):
            return AgentResult(skipped=True, reason='support agent disabled')

        history = list(conversation.messages.order_by('-created_at')
                       .values('direction', 'body', 'source')[:30])
        history.reverse()
        normalised = []
        for m in history:
            role = 'assistant' if m['direction'] == Message.DIRECTION_OUT else 'user'
            normalised.append({'role': role, 'content': m['body'] or ''})

        runner = AgentRunner(
            config=self.config, role=self.ROLE, source=self.SOURCE,
            system_prompt=self._render_system(),
            conversation=conversation, trigger_message=message,
        )
        return runner.run(messages=normalised)

    def _render_system(self) -> str:
        return SUPPORT_SYSTEM_PROMPT.format(
            reseller_name=self.config.reseller.name,
            overrides=_latest_override(self.config, AIPromptVersion.ROLE_SUPPORT),
        )
