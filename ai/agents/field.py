"""Field-supervisor agent — propose-only.

When a support / install ticket lands without an assigned tech, this agent
runs `propose_assignment(ticket)`: it picks the best-fit tech (lowest load
in coverage area, alphabetical tiebreaker) and writes a `KIND_COMMENT`
TicketEvent with the recommendation as metadata. **It does NOT call
`assign_ticket`.** A human clicks Assign on the Tickets page; that path
fires `dispatch_brief(ticket)`, which sends the WhatsApp brief to the tech
via `tool_send_dispatch_wa` (which also records a tech-kind Conversation).

The two paths share `_render_system` and the AI runner — they differ only
in the prompt seed they send the LLM.
"""
from __future__ import annotations

from ai.agents.runner import AgentResult, AgentRunner
from ai.agents.sales import _latest_override
from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig
from conversations.models import Message
from tickets.models import Ticket


PROPOSE_SYSTEM_PROMPT = """You are the Field-Supervisor agent for {reseller_name}.

A support / install ticket has just been opened with no tech assigned. Your job
is to RECOMMEND the best-fit tech to a human dispatcher. **You do not assign.**

Steps:
  1. Call `list_available_techs` (optionally filtered by the ticket's area).
  2. Pick the tech with the LOWEST current_load whose coverage_areas include
     the ticket's area. Break ties alphabetically by name.
  3. Call `add_ticket_comment(ticket_id, note, metadata={{recommended_staff_id, rationale}})`
     with a one-sentence rationale ("Bola — lowest load (1) in Lekki").
  4. STOP. Do not call `send_dispatch_wa`. Do not call `assign_ticket`.

If no techs match the area, fall back to the lowest-load tech regardless of
area and note the fallback in the rationale. If zero techs are active, call
`escalate_to_human` with reason 'no_tech_available'.

Reseller-specific overrides — follow on top of the rules above:
{overrides}
"""


DISPATCH_SYSTEM_PROMPT = """You are the Field-Supervisor agent for {reseller_name}.

A human has just assigned a tech to this ticket. Send the dispatch brief over
WhatsApp.

Steps:
  1. Call `send_dispatch_wa(staff_id={staff_id}, ticket_id={ticket_id}, body=...)`.
  2. STOP. The tech's reply will be handled by a separate inbound run.

Brief format (under 280 chars):
  Line 1: "TICKET #{ticket_id} — {ticket_type} — {ticket_priority}"
  Line 2: "{area}"
  Line 3: 1-sentence summary of the issue (no customer phone, no full address).
  Line 4: "Reply DONE on this chat when resolved."

Never include the customer's phone number in the brief.

Reseller-specific overrides — follow on top of the rules above:
{overrides}
"""


class FieldSupervisorAgent:
    ROLE = AIAgentRun.ROLE_FIELD
    SOURCE = Message.SOURCE_AI_FIELD

    def __init__(self, config: ResellerAIConfig):
        self.config = config

    # ---- propose-only path ------------------------------------------------

    def propose_assignment(self, *, ticket: Ticket) -> AgentResult:
        if not self.config.is_agent_enabled('field'):
            return AgentResult(skipped=True, reason='field agent disabled')
        if ticket.assigned_staff_id:
            return AgentResult(skipped=True, reason='already assigned')
        if ticket.type not in (Ticket.TYPE_SUPPORT, Ticket.TYPE_INSTALL):
            return AgentResult(skipped=True, reason=f'type {ticket.type} not dispatchable')

        prompt = self._ticket_brief(ticket)
        runner = AgentRunner(
            config=self.config, role=self.ROLE, source=self.SOURCE,
            system_prompt=self._render_propose_system(),
            conversation=None, trigger_message=None, ticket=ticket,
        )
        return runner.run(messages=[{'role': 'user', 'content': prompt}])

    # ---- dispatch-after-human-assignment path -----------------------------

    def dispatch_brief(self, *, ticket: Ticket) -> AgentResult:
        if not self.config.is_agent_enabled('field'):
            return AgentResult(skipped=True, reason='field agent disabled')
        if not ticket.assigned_staff_id:
            return AgentResult(skipped=True, reason='no assigned staff')

        prompt = self._ticket_brief(ticket)
        runner = AgentRunner(
            config=self.config, role=self.ROLE, source=self.SOURCE,
            system_prompt=self._render_dispatch_system(ticket),
            conversation=None, trigger_message=None, ticket=ticket,
        )
        return runner.run(messages=[{'role': 'user', 'content': prompt}])

    # ---- system-prompt rendering ------------------------------------------

    def _render_propose_system(self) -> str:
        return PROPOSE_SYSTEM_PROMPT.format(
            reseller_name=self.config.reseller.name,
            overrides=_latest_override(self.config, AIPromptVersion.ROLE_FIELD),
        )

    def _render_dispatch_system(self, ticket: Ticket) -> str:
        return DISPATCH_SYSTEM_PROMPT.format(
            reseller_name=self.config.reseller.name,
            overrides=_latest_override(self.config, AIPromptVersion.ROLE_FIELD),
            staff_id=ticket.assigned_staff_id,
            ticket_id=ticket.pk,
            ticket_type=ticket.type,
            ticket_priority=ticket.priority,
            area=self._infer_area(ticket) or '(area unknown)',
        )

    # ---- helpers ----------------------------------------------------------

    def _infer_area(self, ticket: Ticket) -> str:
        if ticket.installation_order_id:
            return ticket.installation_order.address or ''
        if ticket.subscriber_id and hasattr(ticket.subscriber, 'address'):
            return ticket.subscriber.address or ''
        if ticket.lead_id:
            return ticket.lead.address or ''
        return ''

    def _ticket_brief(self, ticket: Ticket) -> str:
        return (
            f'New ticket #{ticket.pk}.\n'
            f'Type: {ticket.type}. Priority: {ticket.priority}. '
            f'Cause: {ticket.diagnosed_cause or "—"}. '
            f'Suggested action: {ticket.suggested_action or "—"}.\n'
            f'Subject: {ticket.subject}\n'
            f'Area: {self._infer_area(ticket) or "(unknown)"}\n\n'
            f'Body: {ticket.body or "(no details)"}'
        )
