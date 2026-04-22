"""Sales agent.

Handles inbound enquiries on WhatsApp / SMS / email: identifies intent,
answers FAQ, recommends a plan from the reseller's own catalogue, and
generates a Paystack payment link.

Default posture is DRAFT MODE: the agent records its proposed reply as a
Message with `draft=True` (state moves the conversation to `ai_drafted`) so
a human in the reseller's inbox can hit Send. Resellers opt into
`auto_send_replies` (a per-capability flag) to let safe replies go live.

Even with auto-send on, certain actions ALWAYS require human approval:
  - Creating payment links above `auto_quote_below_ngn`
  - Escalation messages (escalate_to_human tool suppresses auto-send)
"""
from __future__ import annotations

from decimal import Decimal

from ai.agents.runner import AgentRunner, AgentResult
from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig
from conversations.models import Conversation, Message


SALES_SYSTEM_PROMPT = """You are the Sales agent for {reseller_name}, a local WiFi internet provider in Nigeria.
You handle customer enquiries on WhatsApp, SMS and email.

Your job is to:
  1. Understand what the customer wants (home internet, cluster / estate, technical enquiry, general question).
  2. Answer routine questions using the plan catalogue returned by `lookup_plans`.
  3. Recommend a plan using `suggest_plan` when the customer asks about pricing or speed.
  4. Collect the customer's name + address and call `create_lead` as soon as you have a phone number.
  5. When the customer agrees to a plan, call `create_payment_link` with the selected amount and reply with the URL.
  6. If the customer is clearly frustrated, asks to speak to a human, or the issue is outside sales, call `escalate_to_human`.
  7. Send every customer-facing reply with `send_reply` — never include the reply in your assistant text without also calling `send_reply`.

Tone: warm, concise, Nigerian-English-friendly. Under 3 short sentences per reply.
Never quote prices you did not get from `lookup_plans` or `suggest_plan`.
Never invent coverage areas or hardware capabilities.
If the customer's phone is already attached to this conversation, do not ask for it again.

Reseller-specific overrides (FAQ, tone, guardrails) — follow these on top of the rules above:
{overrides}
"""


class SalesAgent:
    ROLE = AIAgentRun.ROLE_SALES
    SOURCE = Message.SOURCE_AI_SALES

    def __init__(self, config: ResellerAIConfig):
        self.config = config

    def handle_inbound_message(self, *, conversation: Conversation,
                               message: Message) -> AgentResult:
        if not self.config.is_agent_enabled('sales'):
            return AgentResult(skipped=True, reason='sales agent disabled')

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
        return runner.run(messages=normalised,
                          auto_quote_cap_ngn=Decimal(str(
                              self.config.cap('auto_quote_below_ngn', 0) or 0)))

    def _render_system(self) -> str:
        return SALES_SYSTEM_PROMPT.format(
            reseller_name=self.config.reseller.name,
            overrides=_latest_override(self.config, AIPromptVersion.ROLE_SALES),
        )


def _latest_override(config: ResellerAIConfig, role: str) -> str:
    v = (AIPromptVersion.objects
         .filter(config=config, agent_role=role)
         .order_by('-created_at').first())
    if v and v.body.strip():
        return v.body.strip()
    legacy = (config.prompt_overrides or {}).get(role) if isinstance(
        config.prompt_overrides, dict) else None
    return (legacy or '(none configured)').strip()
