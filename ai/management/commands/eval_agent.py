"""
Customer-agent regression eval runner.

V1 loads the JSON case file, validates structure, and prints a human-reviewable
checklist for each case. A future revision will replay turns through the real
agent and auto-check the assertions; for now it's a manual gate — after any
change to CUSTOMER_SYSTEM_PROMPT, run this and walk through each case in
production chat, ticking off the expected behaviours.

Usage:
  python manage.py eval_agent                       # list cases
  python manage.py eval_agent --case nedu-2026-04-23-ticket-1
  python manage.py eval_agent --validate            # just check structure
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


EVAL_PATH = Path(__file__).resolve().parents[2] / 'evals' / 'customer_agent_transcripts.json'


class Command(BaseCommand):
    help = 'Walk the customer-agent regression eval cases.'

    def add_arguments(self, parser):
        parser.add_argument('--case', type=str, default='', help='Case id to expand.')
        parser.add_argument('--validate', action='store_true',
                            help='Structure-check the eval file, exit non-zero on error.')

    def handle(self, *args, **opts):
        if not EVAL_PATH.exists():
            raise CommandError(f'Eval file missing: {EVAL_PATH}')

        with EVAL_PATH.open() as f:
            doc = json.load(f)

        cases = doc.get('cases') or []
        if not cases:
            raise CommandError('No cases defined in the eval file.')

        # Structural checks — catch drift before humans review.
        required = {'id', 'name', 'inbound_phone', 'turns'}
        errors = []
        for c in cases:
            missing = required - set(c.keys())
            if missing:
                errors.append(f'{c.get("id", "<unnamed>")}: missing keys {sorted(missing)}')
            for i, t in enumerate(c.get('turns') or []):
                if 'inbound' not in t:
                    errors.append(f'{c.get("id")}: turn #{i} has no "inbound"')
                if 'expect' not in t:
                    errors.append(f'{c.get("id")}: turn #{i} has no "expect"')
        if errors:
            for e in errors:
                self.stderr.write(self.style.ERROR(e))
            raise CommandError(f'{len(errors)} structural error(s).')

        if opts['validate']:
            self.stdout.write(self.style.SUCCESS(f'OK — {len(cases)} case(s) valid.'))
            return

        target = opts['case']
        if not target:
            self.stdout.write(f'{len(cases)} case(s) available:\n')
            for c in cases:
                self.stdout.write(f'  {c["id"]:40s}  {c["name"]}')
            self.stdout.write('\nRun `manage.py eval_agent --case <id>` to expand.')
            return

        case = next((c for c in cases if c['id'] == target), None)
        if not case:
            raise CommandError(f'Case not found: {target}')

        self._render(case)

    def _render(self, case):
        out = self.stdout.write
        out(self.style.MIGRATE_HEADING(f'\n=== {case["id"]} ==='))
        out(f'Name:            {case["name"]}')
        out(f'Reseller slug:   {case.get("reseller_slug", "(any)")}')
        out(f'Inbound phone:   {case["inbound_phone"]}')
        if case.get('subscriber_phone'):
            out(f'Account phone:   {case["subscriber_phone"]}')
        out(f'Channel:         {case.get("channel", "whatsapp")}')
        notes = (case.get('notes') or '').strip()
        if notes:
            out('\nNotes:')
            for line in notes.splitlines():
                out(f'  {line}')

        out('\nTurns:')
        for i, t in enumerate(case['turns'], start=1):
            out(f'\n  [{i}] Customer: {t["inbound"]!r}')
            out(f'      Agent MUST:')
            for check in (t.get('expect') or []):
                if isinstance(check, dict):
                    for k, v in check.items():
                        out(f'        - {k}: {v!r}')
                else:
                    out(f'        - {check}')

        globals_ = case.get('global_assertions') or []
        if globals_:
            out('\nAcross the whole transcript:')
            for g in globals_:
                out(f'  - {g}')

        out('\nReview:')
        out('  Send the inbound messages above through production in order (or a')
        out('  staging WA thread). After each turn, verify the agent\'s behaviour')
        out('  against the checklist. Record any failures as a new case id with')
        out('  today\'s date.\n')
