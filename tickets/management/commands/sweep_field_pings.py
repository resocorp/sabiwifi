"""Periodic sweep that pings field techs on stale tickets.

For each non-terminal ticket where:
  - dispatch_sent_at is not null (a brief was actually sent)
  - last_field_ping_at + reseller.cap('cap_field_ping_minutes') minutes <= now
  - field_ping_count < reseller.cap('max_field_pings')

…send a follow-up "any update on #<id>?" via WhatsApp into the existing tech
Conversation, and bump the ping counter. After max_field_pings is reached,
log a TicketEvent (KIND_ESCALATED) and notify admin contacts so a human
takes over.

Designed to run every 10 minutes via a systemd timer (similar to
check_routers / process_broadcasts). The reseller's cap_field_ping_minutes
controls the actual cadence — 10 min just gives the sweep enough resolution
to honour the smallest sensible ping interval.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from accounts.models import Reseller
from ai.models import ResellerAIConfig
from conversations.models import Conversation, Message
from conversations.services import record_outbound_message
from notifications.notify import send_whatsapp
from tickets.models import Ticket, TicketEvent


PING_BODY = (
    "Quick check — any update on ticket #{ticket_id}?\n"
    "Reply 'done' / 'still working' / 'customer not home' "
    "to update the ticket."
)


class Command(BaseCommand):
    help = 'Ping field techs on stale tickets, escalate after max pings.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would happen, do not send.')
        parser.add_argument('--reseller', default='',
                            help='Only sweep this reseller slug.')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        scope = opts['reseller'].strip()

        candidates = (Ticket.objects
                      .filter(dispatch_sent_at__isnull=False,
                              assigned_staff__isnull=False)
                      .exclude(status__in=Ticket.TERMINAL_STATUSES)
                      .select_related('reseller', 'assigned_staff'))
        if scope:
            candidates = candidates.filter(reseller__slug=scope)

        now = timezone.now()
        sent = escalated = skipped = 0
        for t in candidates:
            try:
                cfg = t.reseller.ai_config
            except ResellerAIConfig.DoesNotExist:
                skipped += 1
                continue
            if cfg.ai_paused_at or not cfg.cap('ai_enabled'):
                skipped += 1
                continue

            ping_min = int(cfg.cap('cap_field_ping_minutes', 30) or 30)
            max_pings = int(cfg.cap('max_field_pings', 4) or 4)

            last = t.last_field_ping_at or t.dispatch_sent_at
            if last is None:
                skipped += 1
                continue
            if (now - last) < timedelta(minutes=ping_min):
                continue  # not yet due

            if t.field_ping_count >= max_pings:
                # Already pinged enough — escalate once and stop.
                self._escalate(t, dry=dry)
                escalated += 1
                continue

            if self._send_ping(t, dry=dry, now=now):
                sent += 1

        self.stdout.write(self.style.SUCCESS(
            f'Done. sent={sent} escalated={escalated} skipped={skipped}'
        ))

    # ----- helpers ----------------------------------------------------------

    def _send_ping(self, ticket: Ticket, *, dry: bool, now) -> bool:
        tech = ticket.assigned_staff
        target = (tech.whatsapp or tech.phone) if tech else ''
        if not target:
            return False
        body = PING_BODY.format(ticket_id=ticket.pk)

        if dry:
            self.stdout.write(
                f'  [dry-run] would ping #{ticket.pk} → {target} '
                f'(count {ticket.field_ping_count} → {ticket.field_ping_count + 1})'
            )
            return True

        # Reuse / create the tech-kind Conversation so the inbox shows the ping.
        conv, _ = Conversation.objects.get_or_create(
            reseller=ticket.reseller,
            channel=Conversation.CHANNEL_WHATSAPP,
            external_thread_id=target,
            defaults={
                'kind': Conversation.KIND_TECH,
                'assigned_staff': tech,
                'contact_phone': target,
                'contact_display_name': tech.name,
                'state': Conversation.STATE_OPEN,
            },
        )
        # Defensive promote — convo may have been created as customer-kind.
        if conv.kind != Conversation.KIND_TECH:
            conv.kind = Conversation.KIND_TECH
            conv.assigned_staff = tech
            conv.save(update_fields=['kind', 'assigned_staff', 'updated_at'])

        msg = record_outbound_message(
            conversation=conv, body=body, source=Message.SOURCE_AI_FIELD,
        )
        ok = bool(send_whatsapp(ticket.reseller.slug, target, body))
        msg.delivery_status = (Message.DELIVERY_QUEUED if ok
                               else Message.DELIVERY_FAILED)
        msg.save(update_fields=['delivery_status'])

        Ticket.objects.filter(pk=ticket.pk).update(
            last_field_ping_at=now,
            field_ping_count=ticket.field_ping_count + 1,
        )
        self.stdout.write(
            f'  pinged #{ticket.pk} → {target} '
            f'(count → {ticket.field_ping_count + 1})'
        )
        return ok

    def _escalate(self, ticket: Ticket, *, dry: bool) -> None:
        if dry:
            self.stdout.write(
                f'  [dry-run] would escalate #{ticket.pk} '
                f'(no field response after {ticket.field_ping_count} pings)'
            )
            return
        TicketEvent.objects.create(
            ticket=ticket,
            kind=TicketEvent.KIND_ESCALATED,
            actor='ai_field',
            note=f'No field response after {ticket.field_ping_count} pings',
            metadata={'field_ping_count': ticket.field_ping_count,
                      'reason': 'no_field_response'},
        )
        # Notify admin contacts via the existing reseller channel.
        try:
            from notifications.notify import notify_admin_contacts
            notify_admin_contacts(
                ticket.reseller, 'router_offline',  # closest existing event
                context={
                    'location': f'ticket #{ticket.pk}',
                    'serial': ticket.assigned_staff.name if ticket.assigned_staff_id else '',
                    'offline_duration': f'after {ticket.field_ping_count} pings',
                },
            )
        except Exception:
            pass
        # Bump priority so it sorts to the top of the operator's view.
        if ticket.priority == Ticket.PRIORITY_NORMAL:
            ticket.priority = Ticket.PRIORITY_HIGH
            ticket.save(update_fields=['priority', 'updated_at'])
        self.stdout.write(self.style.WARNING(
            f'  escalated #{ticket.pk} (no response after '
            f'{ticket.field_ping_count} pings)'
        ))
