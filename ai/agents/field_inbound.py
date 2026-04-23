"""Field-Inbound agent — handles tech replies on WhatsApp.

When a tech sends a message to the reseller's WA number (and is identified
as a StaffMember by phone matching), this agent runs. It implements the
confirm-before-action safety pattern:

  1. Identify ticket(s) via lookup_ticket_for_tech (by ticket #, phone,
     email, or by the tech's own assignments).
  2. If 0 matches → ask the tech for an identifier.
  3. If ≥2 matches → ask the tech which one (by ticket #).
  4. If 1 match → set_pending_close_action + send a confirmation summary.
  5. On the next inbound (YES/NO) → consume_pending_close_action and either
     change_status_ticket(...) or cancel.

The system prompt instructs the LLM to follow this flow; tools enforce
the side effects.
"""
from __future__ import annotations

from ai.agents.runner import AgentResult, AgentRunner
from ai.agents._helpers import _latest_override, normalised_history
from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig
from conversations.models import Conversation, Message
from staff.models import StaffMember


FIELD_INBOUND_SYSTEM_PROMPT = """You are the Field-Inbound agent for {reseller_name}.

You are talking to a field technician — NOT a customer — over WhatsApp.
Their name is {tech_name}. They have {open_count} open ticket(s) currently
assigned to them.

Your single job: interpret the tech's free-text and update the ticket
state — but ALWAYS confirm with a short summary BEFORE you change status.
Never change a ticket's status on the first message.

Flow on every inbound:
  1. Identify which ticket(s) the tech means using `lookup_ticket_for_tech`:
       - body contains "#123" → look up that ticket id
       - body contains a phone number → pass phone={{phone}}
       - body contains an email → pass email={{email}}
       - otherwise → call with assigned_staff_id={tech_id}
  2. Disambiguate:
       - 0 matches: send_reply asking for ticket #, customer phone, or email.
       - 1 match: go to step 3.
       - 2+ matches: send_reply listing each as
         "#<id> (<area>, <subject>, <contact_name>)" and ask which one.
  3. Map the tech's intent to an expected status:
       - "done" / "fixed" / "resolved" → resolved
       - "on it" / "in progress" / "started" / "got it" → in_progress
       - "customer not home" / "no answer" / "rescheduled" → awaiting_customer
       - anything else → ask them what state they want.
  4. Call `set_pending_close_action(ticket_id, expected_status=...)`.
  5. Call `send_reply` with the summary:
        "Confirming: Ticket #<id> — <contact_name> (<contact_phone>),
         <area>, '<subject>'. Reply YES to mark <expected_status>, NO to cancel."
     STOP. Do not change status.

When the tech's NEXT message arrives:
  6. If body matches /^(yes|y|confirm|ok|correct)\\b/i:
        - call `consume_pending_close_action` to retrieve the snapshot
        - if there is a pending action, call
          `change_status_ticket(ticket_id, new_status=expected_status, note=...)`
        - reply: "Done. The customer has been notified."
  7. If body matches /^(no|n|cancel|wait)\\b/i:
        - call `consume_pending_close_action` (clears it)
        - reply: "Cancelled. What would you like to do?"
  8. Otherwise: treat the message as a fresh request — restart from step 1.

Hard rules:
  - You speak to TECHS, not customers. Tone: short, direct, friendly.
  - Reply under 2 short sentences.
  - Never close a ticket without an explicit YES.
  - Never include the customer's full address — area + name is enough.
  - If there is no pending action and the tech says YES alone, ask them what
    they meant.

Reseller-specific overrides — follow on top of the rules above:
{overrides}
"""


class FieldInboundAgent:
    ROLE = AIAgentRun.ROLE_FIELD_INBOUND
    SOURCE = Message.SOURCE_AI_FIELD

    def __init__(self, config: ResellerAIConfig):
        self.config = config

    def handle_inbound_message(self, *, conversation: Conversation,
                               message: Message) -> AgentResult:
        if not self.config.is_agent_enabled('field'):
            return AgentResult(skipped=True, reason='field agent disabled')
        if conversation.kind != Conversation.KIND_TECH:
            return AgentResult(skipped=True, reason='not a tech conversation')
        tech = conversation.assigned_staff
        if tech is None:
            return AgentResult(skipped=True, reason='conversation has no assigned tech')

        runner = AgentRunner(
            config=self.config, role=self.ROLE, source=self.SOURCE,
            system_prompt=self._render_system(tech),
            conversation=conversation, trigger_message=message,
        )
        return runner.run(messages=normalised_history(conversation))

    def _render_system(self, tech: StaffMember) -> str:
        from tickets.models import Ticket
        open_count = Ticket.objects.filter(
            reseller=self.config.reseller, assigned_staff=tech,
        ).exclude(status__in=Ticket.TERMINAL_STATUSES).count()
        return FIELD_INBOUND_SYSTEM_PROMPT.format(
            reseller_name=self.config.reseller.name,
            tech_name=tech.name,
            tech_id=tech.pk,
            open_count=open_count,
            overrides=_latest_override(self.config, AIPromptVersion.ROLE_FIELD_INBOUND),
        )
