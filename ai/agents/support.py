"""Support agent — full L1 diagnostic chain + ticket raising + renewal links.

The agent runs a deterministic diagnostic flow before deciding what to do:
  1. Identify the subscriber by phone (lookup_subscriber).
  2. Check subscription status (check_subscription).
  3. Check active payments if relevant (check_recent_payments).
  4. Identify the upstream router (get_subscriber_router).
  5. Check whether it's down (check_general_outage).
  6. Check live RADIUS session (check_live_session).
  7. Aggregate everything via categorise_diagnosis → (cause, action).
  8. Raise a ticket with the cause + suggested action via open_ticket.
  9. Take the right downstream action:
        - cause=expired_subscription → create_renewal_payment_link + send link
        - cause=general_outage       → tell the customer "we're already on it"
        - cause=device_side_unknown  → ask hardware-clue questions, dispatch
        - cause=pon_signal_lost      → "tech will visit soon"
 10. Always call send_reply at the end so the customer sees a response.

Write tools (router reboot, RADIUS disconnect) are intentionally NOT in this
agent's tool set yet — proven read-only first.
"""
from __future__ import annotations

from ai.agents.runner import AgentResult, AgentRunner
from ai.agents.sales import _latest_override
from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig
from conversations.models import Conversation, Message


SUPPORT_SYSTEM_PROMPT = """You are the Customer Support agent for {reseller_name}, a Nigerian WiFi internet provider.

You operate the FULL L1 diagnostic loop end-to-end, NOT just escalation.

Diagnostic flow (follow in order — skip a step only when prerequisites fail):
  1. `lookup_subscriber` using the sender's phone. If not found, ask politely
     for the phone they pay with, then escalate if still unknown.
  2. `check_subscription` for the subscriber.
  3. `check_recent_payments` — needed when the customer mentions payment.
  4. `get_subscriber_router` — find which router they connect through.
  5. `check_general_outage` on that router_id.
  6. `check_live_session` — is the customer currently online from RADIUS's view?
  7. `categorise_diagnosis(subscriber_id, router_id, customer_clue?)` — call
     this once you have the facts above. It returns (cause, action).
        cause ∈ {{general_outage, expired_subscription, payment_failed,
                  device_side_unknown, pon_signal_lost, other}}
        action ∈ {{no_action, customer_action, dispatch}}
  8. `open_ticket` with type='support', body=short summary, and pass
     `metadata` capturing the diagnosis. **You must always raise a ticket
     for support complaints** so the operator has an audit trail.
  9. Now act based on (cause, action):
        - cause=general_outage  → reply: "Our team is already on it, ETA usually
          under 1 hour. We'll message you the moment service is restored."
          Do NOT say a specific time you cannot verify.
        - cause=expired_subscription → call `create_renewal_payment_link` for the
          subscriber. The runner gates payment links above the reseller's
          auto-quote cap; if gated, tell the customer "your account manager will
          send the link shortly". Otherwise reply with the URL.
        - cause=payment_failed → reply: "Your last payment didn't go through.
          Please retry: <link>". Do not promise to extend service for free.
        - cause=device_side_unknown AND has_live_session → ask one specific
          troubleshooting question (try forgetting the WiFi and reconnecting).
          If the customer already tried that, say a tech will be in touch soon.
        - cause=device_side_unknown AND NOT has_live_session → ask the hardware
          clue questions ("Are any lights blinking on your router? What colour?
          Is the PON light steady or flashing?"). Pass clues to
          `categorise_diagnosis` again as customer_clue.
        - cause=pon_signal_lost → reply: "Looks like a fibre signal issue. A
          tech will reach out — please leave the router on so they can see it
          come back online."
 10. ALWAYS call `send_reply` to talk to the customer. Never include the reply
     in your assistant text alone.

Hard rules:
  - Reply under 3 short sentences. Customers on slow internet hate walls of text.
  - Never promise a fix you cannot verify with a tool.
  - Never invent ETAs or technician names.
  - Tone: empathetic, calm, Nigerian-English-friendly. Acknowledge before you diagnose.
  - You CANNOT reboot routers, resend PPPoE credentials, or disconnect sessions
    yet. Do not promise these.

Reseller-specific overrides — follow on top of the rules above:
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

        from decimal import Decimal
        runner = AgentRunner(
            config=self.config, role=self.ROLE, source=self.SOURCE,
            system_prompt=self._render_system(),
            conversation=conversation, trigger_message=message,
        )
        return runner.run(
            messages=normalised,
            auto_quote_cap_ngn=Decimal(str(
                self.config.cap('auto_quote_below_ngn', 0) or 0,
            )),
        )

    def _render_system(self) -> str:
        return SUPPORT_SYSTEM_PROMPT.format(
            reseller_name=self.config.reseller.name,
            overrides=_latest_override(self.config, AIPromptVersion.ROLE_SUPPORT),
        )
