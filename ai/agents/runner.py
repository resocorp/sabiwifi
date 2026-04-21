"""Shared agent loop.

One AgentRunner handles: provider call → tool-call execution → re-call with
tool results → draft-mode hand-off. All three AI personas share this runner
and differ only in their system prompt + tool set (see ai.tools.AGENT_TOOLS).

Draft-mode semantics:
  - If `auto_send_replies` is OFF, the runner intercepts `send_reply` and
    stores the proposed body as a message with source=ai_sales (etc.) and
    conversation.state='ai_drafted' — a human hits Send from the inbox.
  - If ON, `send_reply` fires normally. Even then, certain tools gate
    auto-send: an agent that calls `escalate_to_human` ends the turn in
    pending_human regardless.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.utils import timezone

from ai import tools as tools_mod
from ai.models import AIAgentRun, ResellerAIConfig
from ai.providers import ChatResponse, ToolCall, get_provider
from conversations.models import Conversation, Message
from conversations.services import record_outbound_message


MAX_STEPS = 5


@dataclass
class AgentResult:
    skipped: bool = False
    reason: str = ''
    run_id: int | None = None
    replied: bool = False
    drafted: bool = False
    tool_calls: list = field(default_factory=list)
    text: str = ''


class AgentRunner:
    def __init__(self, *, config: ResellerAIConfig, role: str, source: str,
                 system_prompt: str,
                 conversation: Conversation | None = None,
                 trigger_message: Message | None = None,
                 ticket=None):
        self.config = config
        self.role = role
        self.source = source
        self.system = system_prompt
        self.conversation = conversation
        self.trigger_message = trigger_message
        self.ticket = ticket

    def run(self, *, messages: list, auto_quote_cap_ngn: Decimal = Decimal('0'),
            max_tokens: int = 600) -> AgentResult:
        run = AIAgentRun.objects.create(
            reseller=self.config.reseller,
            conversation=self.conversation,
            message=self.trigger_message,
            ticket=self.ticket,
            agent_role=self.role,
            provider=self.config.text_provider,
            model=self.config.text_model or '',
            inputs={'messages': messages, 'system': self.system[:2000]},
        )
        ctx = tools_mod.ToolContext(
            reseller=self.config.reseller,
            conversation=self.conversation,
            run_id=run.pk,
        )
        tool_schemas = tools_mod.tool_schemas_for(self.role)
        provider = get_provider(self.config)

        auto_send = bool(self.config.cap('auto_send_replies'))
        start = time.monotonic()
        acc_prompt = acc_completion = 0
        acc_cost = Decimal('0')
        tool_trace: list[dict] = []
        reply_sent = reply_drafted = False
        final_text = ''
        status = AIAgentRun.STATUS_SUCCESS
        err = ''

        try:
            convo = list(messages)
            for step in range(MAX_STEPS):
                resp: ChatResponse = provider.chat(
                    system=self.system, messages=convo,
                    tools=tool_schemas, max_tokens=max_tokens,
                )
                acc_prompt += resp.prompt_tokens
                acc_completion += resp.completion_tokens
                acc_cost += resp.cost_ngn
                final_text = resp.text or final_text

                if not resp.tool_calls:
                    break

                # Record assistant turn with tool calls, then resolve each.
                convo.append({
                    'role': 'assistant',
                    'content': resp.text or '',
                    'tool_calls': [{'id': tc.id, 'name': tc.name,
                                    'arguments': tc.arguments}
                                   for tc in resp.tool_calls],
                })
                for tc in resp.tool_calls:
                    outcome = self._dispatch_tool(
                        tc, ctx, auto_send=auto_send,
                        auto_quote_cap_ngn=auto_quote_cap_ngn,
                    )
                    tool_trace.append({
                        'name': tc.name, 'arguments': tc.arguments,
                        'result': outcome['result'],
                        'gated': outcome.get('gated', False),
                    })
                    if outcome.get('replied'):
                        reply_sent = True
                    if outcome.get('drafted'):
                        reply_drafted = True
                    convo.append({
                        'role': 'tool',
                        'tool_call_id': tc.id,
                        'tool_name': tc.name,
                        'content': str(outcome['result']),
                    })
                if reply_sent or reply_drafted:
                    # Standard single-reply turn — exit the loop. If the agent
                    # needs to do more (e.g. open_ticket after send_reply)
                    # let the next inbound message retrigger it.
                    break
        except Exception as exc:
            status = AIAgentRun.STATUS_FAILED
            err = f'{type(exc).__name__}: {exc}'[:500]

        ended = timezone.now()
        AIAgentRun.objects.filter(pk=run.pk).update(
            status=status, error_message=err,
            prompt_tokens=acc_prompt, completion_tokens=acc_completion,
            cost_ngn=acc_cost,
            outputs={'text': final_text[:4000], 'replied': reply_sent,
                     'drafted': reply_drafted},
            tool_calls=tool_trace,
            ended_at=ended,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

        if status == AIAgentRun.STATUS_FAILED:
            from ai.safety import trip_circuit_breaker
            trip_circuit_breaker(self.config)

        return AgentResult(
            skipped=False, run_id=run.pk,
            replied=reply_sent, drafted=reply_drafted,
            tool_calls=tool_trace, text=final_text,
        )

    def _dispatch_tool(self, tc: ToolCall, ctx, *, auto_send: bool,
                       auto_quote_cap_ngn: Decimal) -> dict:
        name = tc.name
        args = tc.arguments or {}

        # Gate 1: quote cap. Payment links above the cap always require a human.
        if name == 'create_payment_link':
            try:
                amount = Decimal(str(args.get('amount_ngn', 0)))
            except Exception:
                amount = Decimal('0')
            if auto_quote_cap_ngn > 0 and amount > auto_quote_cap_ngn:
                return {'result': {'gated': True,
                                   'reason': 'amount exceeds auto_quote_below_ngn cap',
                                   'cap_ngn': str(auto_quote_cap_ngn)},
                        'gated': True}

        # Gate 2: send_reply → if auto-send is OFF, store as draft instead.
        if name == 'send_reply':
            if not ctx.conversation:
                return {'result': {'error': 'no conversation'}}
            body = (args.get('body') or '').strip()
            if not body:
                return {'result': {'error': 'body required'}}
            if not auto_send:
                return self._draft_reply(ctx.conversation, body, ctx.run_id)
            # Per-customer daily outbound cap (safety rail). If reached, force
            # a draft so a human decides whether to keep messaging the user.
            from ai.safety import outbound_cap_reached
            if outbound_cap_reached(self.config, ctx.conversation):
                return self._draft_reply(ctx.conversation, body, ctx.run_id)
            # Fall through to the real tool (delivers + records).

        result = tools_mod.run_tool(name, ctx, args)

        if name == 'send_reply' and not result.get('error'):
            return {'result': result, 'replied': True}
        if name == 'escalate_to_human' and not result.get('error'):
            # Escalation always flips auto-send off for this thread.
            return {'result': result}
        return {'result': result}

    def _draft_reply(self, conversation: Conversation, body: str, run_id: int) -> dict:
        msg = record_outbound_message(
            conversation=conversation, body=body, source=self.source,
            agent_run_id=run_id,
        )
        # Mark as draft so the inbox renders a "Send" button, not a "Sent" chip.
        Message.objects.filter(pk=msg.pk).update(
            is_draft=True, delivery_status=Message.DELIVERY_PENDING,
        )
        conversation.state = Conversation.STATE_AI_DRAFTED
        conversation.save(update_fields=['state', 'updated_at'])
        return {'result': {'drafted': True, 'message_id': msg.pk},
                'drafted': True}
