"""Template tags exposing the RBAC capability check.

Usage:
    {% load permissions_tags %}
    {% if user|can:"plans" %}<a href="...">Plans</a>{% endif %}
    {% if user|can_any:"tickets,tickets_read,tickets_own" %}...{% endif %}
"""
from django import template

from accounts.permissions import can as _can, effective_role as _effective_role

register = template.Library()


@register.filter(name='can')
def can_filter(user, capability):
    return _can(user, capability)


@register.filter(name='can_any')
def can_any_filter(user, capabilities_csv):
    caps = [c.strip() for c in (capabilities_csv or '').split(',') if c.strip()]
    return any(_can(user, c) for c in caps)


@register.simple_tag(takes_context=True)
def role(context):
    request = context.get('request')
    return _effective_role(request.user) if request else ''
