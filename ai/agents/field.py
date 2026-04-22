"""Field-supervisor agent.

Triggered when a Ticket enters `open` state with type in {support, install}.
Picks the best tech (covers area + lowest load + on shift), sends a WA
dispatch brief, and auto-assigns the ticket. The tech's reply is NOT handled
here yet — phase 2b will wire that via a separate inbound route once the
Sales/Support draft-mode rollout is stable.
"""
from __future__ import annotations

from ai.agents.runner import AgentResult, AgentRunner
from ai.agents.sales import _latest_override
from ai.models import AIAgentRun, AIPromptVersion, ResellerAIConfig
from conversations.models import Message
from tickets.models import Ticket


FIELD_SYSTEM_PROMPT = """You are the Field-Supervisor agent for {reseller_name}, coordinating field technicians in Nigeria.

Context: a ticket has just been opened. Your job is to dispatch the best-fit technician.

Steps:
  1. Call `list_available_techs` (optionally filtered by the ticket's area).
  2. Pick the tech with the LOWEST current_load whose coverage_areas include the ticket's area.
  3. Call `assign_ticket(ticket_id, staff_id)` with that tech.
  4. Call `send_dispatch_wa(staff_id, body)` with a 2-sentence brief: customer name (if known), area, issue summary, ticket id.
  5. Stop. Do NOT close the ticket yet — the tech is responsible for marking it resolved.

If no techs are available, call `escalate_to_human` with reason 'no_tech_available'.

Rules:
  - Do not invent tech names. Only use techs returned by `list_available_techs`.
  - Never expose customer phone numbers in the dispatch brief — use the ticket id.

Reseller-specific overrides — follow these on top of the rules above:
{overrides}
"""


class FieldSupervisorAgent:
    ROLE = AIAgentRun.ROLE_FIELD
    SOURCE = Message.SOURCE_AI_FIELD

    def __init__(self, config: ResellerAIConfig):
        self.config = config

    def handle_new_ticket(self, *, ticket: Ticket) -> AgentResult:
        if not self.config.is_agent_enabled('field'):
            return AgentResult(skipped=True, reason='field agent disabled')
        if ticket.assigned_staff_id:
            return AgentResult(skipped=True, reason='already assigned')
        if ticket.type not in (Ticket.TYPE_SUPPORT, Ticket.TYPE_INSTALL):
            return AgentResult(skipped=True, reason=f'type {ticket.type} not dispatchable')

        prompt = self._ticket_brief(ticket)
        runner = AgentRunner(
            config=self.config, role=self.ROLE, source=self.SOURCE,
            system_prompt=self._render_system(),
            conversation=None, trigger_message=None, ticket=ticket,
        )
        return runner.run(messages=[{'role': 'user', 'content': prompt}])

    def _render_system(self) -> str:
        return FIELD_SYSTEM_PROMPT.format(
            reseller_name=self.config.reseller.name,
            overrides=_latest_override(self.config, AIPromptVersion.ROLE_FIELD),
        )

    def _ticket_brief(self, ticket: Ticket) -> str:
        area = ''
        if ticket.installation_order_id:
            area = ticket.installation_order.address or ''
        elif ticket.subscriber_id:
            area = (ticket.subscriber.address or '') if hasattr(ticket.subscriber, 'address') else ''
        elif ticket.lead_id:
            area = ticket.lead.address or ''
        return (
            f'New ticket #{ticket.pk}.\n'
            f'Type: {ticket.type}. Priority: {ticket.priority}.\n'
            f'Subject: {ticket.subject}\n'
            f'Area: {area or "(unknown)"}\n\n'
            f'Body: {ticket.body or "(no details)"}'
        )
