"""RQ jobs entry points for AI agents.

Inbound-message path:
  record_inbound_message() schedules `route_inbound_message(message_id)` on
  the `ai` queue after the DB commit. That job picks an agent based on the
  conversation's state and hands off to `run_sales_agent` or
  `run_support_agent`.

Routing is intentionally crude for Phase 2:
  - Conversation has a linked Subscriber → support agent
  - Otherwise                            → sales agent
This keeps things obvious. Later we can add a classifier tool that the router
calls before picking an agent.
"""
import logging

import django_rq

from ai.agents.field import FieldSupervisorAgent
from ai.agents.sales import SalesAgent
from ai.agents.support import SupportAgent
from ai.models import ResellerAIConfig
from conversations.models import Conversation, Message
from tickets.models import Ticket

logger = logging.getLogger(__name__)


def enqueue_sales_agent(message_id: int) -> None:
    """Back-compat shim. Prefer `enqueue_inbound_router` for new call sites."""
    django_rq.get_queue('ai').enqueue(route_inbound_message, message_id)


def enqueue_inbound_router(message_id: int) -> None:
    django_rq.get_queue('ai').enqueue(route_inbound_message, message_id)


def route_inbound_message(message_id: int) -> dict:
    try:
        msg = (Message.objects
               .select_related('conversation', 'conversation__reseller')
               .get(pk=message_id))
    except Message.DoesNotExist:
        return {'skipped': True, 'reason': 'message gone'}

    convo = msg.conversation
    if msg.direction != Message.DIRECTION_IN:
        return {'skipped': True, 'reason': 'not inbound'}
    if not convo.ai_enabled:
        return {'skipped': True, 'reason': 'conversation.ai_enabled=False'}

    try:
        config = convo.reseller.ai_config
    except ResellerAIConfig.DoesNotExist:
        return {'skipped': True, 'reason': 'no ai_config for reseller'}

    if config.ai_paused_at or not config.cap('ai_enabled'):
        return {'skipped': True, 'reason': 'ai paused or not enabled'}

    if convo.subscriber_id:
        return run_support_agent(message_id)
    return run_sales_agent(message_id)


def run_sales_agent(message_id: int) -> dict:
    msg = Message.objects.select_related(
        'conversation', 'conversation__reseller').get(pk=message_id)
    config = msg.conversation.reseller.ai_config
    if not config.is_agent_enabled('sales'):
        return {'skipped': True, 'reason': 'sales agent disabled'}
    r = SalesAgent(config).handle_inbound_message(
        conversation=msg.conversation, message=msg)
    return {'agent': 'sales', 'run_id': r.run_id, 'replied': r.replied,
            'drafted': r.drafted, 'skipped': r.skipped}


def run_support_agent(message_id: int) -> dict:
    msg = Message.objects.select_related(
        'conversation', 'conversation__reseller').get(pk=message_id)
    config = msg.conversation.reseller.ai_config
    if not config.is_agent_enabled('support'):
        return {'skipped': True, 'reason': 'support agent disabled'}
    r = SupportAgent(config).handle_inbound_message(
        conversation=msg.conversation, message=msg)
    return {'agent': 'support', 'run_id': r.run_id, 'replied': r.replied,
            'drafted': r.drafted, 'skipped': r.skipped}


def enqueue_field_agent(ticket_id: int) -> None:
    django_rq.get_queue('ai').enqueue(run_field_agent, ticket_id)


def run_field_agent(ticket_id: int) -> dict:
    try:
        ticket = Ticket.objects.select_related('reseller').get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {'skipped': True, 'reason': 'ticket gone'}
    try:
        config = ticket.reseller.ai_config
    except ResellerAIConfig.DoesNotExist:
        return {'skipped': True, 'reason': 'no ai_config'}
    if config.ai_paused_at or not config.cap('ai_enabled'):
        return {'skipped': True, 'reason': 'ai paused'}
    if not config.is_agent_enabled('field'):
        return {'skipped': True, 'reason': 'field agent disabled'}
    r = FieldSupervisorAgent(config).handle_new_ticket(ticket=ticket)
    return {'agent': 'field', 'run_id': r.run_id, 'skipped': r.skipped}
