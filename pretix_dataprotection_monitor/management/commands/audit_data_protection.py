import json
from dataclasses import asdict

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ...audit import audit_event
from ...discovery import event_ids


class Command(BaseCommand):
    help = "Report aggregate data-protection findings for one event or one bounded event page."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", type=int)
        parser.add_argument("--after", type=int, default=0)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--grace-days", type=int, default=30)

    def handle(self, *args, **options):
        event_id = options["event_id"]
        grace_days = options["grace_days"]
        if grace_days < 0:
            raise CommandError("--grace-days must be nonnegative")
        if event_id is not None and event_id < 1:
            raise CommandError("--event-id must be positive")
        try:
            ids = [event_id] if event_id is not None else event_ids(
                after=options["after"], limit=options["limit"]
            )
        except ValueError as error:
            raise CommandError(str(error)) from None
        now = timezone.now()
        for pk in ids:
            result = audit_event(pk, now=now, grace_days=grace_days)
            self.stdout.write(json.dumps(asdict(result), default=str))
