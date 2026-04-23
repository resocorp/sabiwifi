"""Shadow-mode replay harness: run past inbound messages through the AI.

Used to preview what the CustomerAgent *would* have replied on real
conversations without sending anything. Forces draft mode regardless of the
reseller's `auto_send_replies` cap by temporarily pinning the capability
off for the duration of the run.

    manage.py ai_replay --reseller acme --days 7 --limit 20

Exits 0 on success; prints a summary row per message. AIAgentRun entries are
created so the operator's run-audit page surfaces the replays.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.models import Reseller
from ai.agents.customer import CustomerAgent
from ai.models import ResellerAIConfig
from conversations.models import Conversation, Message


class Command(BaseCommand):
    help = 'Replay inbound messages through Sales AI in forced-draft mode.'

    def add_arguments(self, parser):
        parser.add_argument('--reseller', required=True,
                            help='Reseller slug to replay.')
        parser.add_argument('--days', type=int, default=7,
                            help='Window in days (default: 7).')
        parser.add_argument('--limit', type=int, default=20,
                            help='Max messages to replay (default: 20).')
        parser.add_argument('--channel', default='',
                            help='Optional channel filter: whatsapp / sms / email.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Do not persist drafts; only log what would happen.')

    def handle(self, *args, **opts):
        slug = opts['reseller']
        try:
            reseller = Reseller.objects.get(slug=slug)
        except Reseller.DoesNotExist:
            raise CommandError(f'No reseller with slug={slug!r}')

        try:
            config = reseller.ai_config
        except ResellerAIConfig.DoesNotExist:
            raise CommandError(f'Reseller {slug} has no ai_config — configure provider first.')

        # Force draft mode for this run — never auto-send in replay.
        caps_snapshot = dict(config.capabilities or {})
        config.capabilities = {**caps_snapshot, 'auto_send_replies': False}

        since = timezone.now() - timedelta(days=opts['days'])
        qs = (Message.objects
              .select_related('conversation', 'conversation__reseller')
              .filter(conversation__reseller=reseller,
                      direction=Message.DIRECTION_IN,
                      created_at__gte=since)
              .order_by('created_at'))
        if opts['channel']:
            qs = qs.filter(conversation__channel=opts['channel'])

        messages = list(qs[:opts['limit']])
        if not messages:
            self.stdout.write(self.style.WARNING('No inbound messages in window.'))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Replaying {len(messages)} inbound message(s) for {reseller.name} (draft mode).'
        ))

        agent = CustomerAgent(config)
        replied = drafted = skipped = errored = 0
        for msg in messages:
            label = f'msg#{msg.pk} [{msg.conversation.channel}] "{msg.body[:48]}"'
            if opts['dry_run']:
                self.stdout.write(f'  (dry-run) would replay {label}')
                continue
            try:
                result = agent.handle_inbound_message(
                    conversation=msg.conversation, message=msg,
                )
            except Exception as exc:
                errored += 1
                self.stdout.write(self.style.ERROR(
                    f'  ✗ {label} — {type(exc).__name__}: {exc}'
                ))
                continue
            if result.skipped:
                skipped += 1
                self.stdout.write(f'  · skipped {label} — {result.reason}')
            elif result.drafted:
                drafted += 1
                self.stdout.write(self.style.SUCCESS(
                    f'  ✎ drafted {label} → run#{result.run_id}'
                ))
            elif result.replied:
                replied += 1
                self.stdout.write(self.style.WARNING(
                    f'  ! replied (should not happen in draft mode) {label}'
                ))
            else:
                self.stdout.write(f'  · no-op {label}')

        # Restore original caps (in case the process runs on prod and this
        # config object stays in memory — safety.)
        config.capabilities = caps_snapshot

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done — drafted={drafted} replied={replied} skipped={skipped} errored={errored}'
        ))
