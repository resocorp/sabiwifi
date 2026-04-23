"""Shared helpers for agent implementations.

Extracted from the original `sales.py` so the helpers survive the
Sales→CustomerAgent collapse. Imported by customer.py, field_inbound.py,
and field.py.
"""
from __future__ import annotations

from ai.models import AIPromptVersion, ResellerAIConfig
from conversations.models import Conversation, Message


def _latest_override(config: ResellerAIConfig, role: str) -> str:
    v = (AIPromptVersion.objects
         .filter(config=config, agent_role=role)
         .order_by('-created_at').first())
    if v and v.body.strip():
        return v.body.strip()
    legacy = (config.prompt_overrides or {}).get(role) if isinstance(
        config.prompt_overrides, dict) else None
    return (legacy or '(none configured)').strip()


def normalised_history(conversation: Conversation, *, limit: int = 30) -> list[dict]:
    """Pull recent messages and shape them for the LLM.

    - Outbound → role='assistant'.
    - Inbound  → role='user'.
    - Empty bodies are substituted with a placeholder describing attachments
      (or skipped if both body and attachments are empty). Anthropic returns
      400 Bad Request if a message has empty content, so we must never send
      ''.

    Returns oldest-first list of {'role', 'content'} dicts.
    """
    rows = list(conversation.messages.order_by('-created_at')
                .values('direction', 'body', 'source', 'attachments')[:limit])
    rows.reverse()
    out = []
    for m in rows:
        body = (m.get('body') or '').strip()
        if not body:
            atts = m.get('attachments') or []
            if not atts:
                continue  # nothing to say; drop the row
            kinds = ', '.join(sorted({(a or {}).get('kind', 'attachment') for a in atts}))
            body = f'[customer sent: {kinds}]'
        role = 'assistant' if m['direction'] == Message.DIRECTION_OUT else 'user'
        out.append({'role': role, 'content': body})
    return out
