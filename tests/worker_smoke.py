"""Run only in the isolated Docker integration project."""
import os
import subprocess
import sys
from datetime import timedelta
from time import monotonic, sleep

os.environ["MONITOR_TEST_BROKER_URL"] = os.environ["MONITOR_SMOKE_BROKER_URL"]
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django

django.setup()

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, Organizer, OutgoingMail

from pretix_dataprotection_monitor.audit import Status
from pretix_dataprotection_monitor.models import AuditRun, EventAuditState, Reminder

call_command("migrate", interactive=False, verbosity=0)
with scopes_disabled():
    organizer = Organizer.objects.create(name="Integration fixture", slug="monitor-fixture")
    organizer.create_default_sales_channels()
    end = timezone.now() - timedelta(days=60)
    event = Event.objects.create(
        organizer=organizer, name="Integration fixture", slug="monitor-fixture",
        date_from=end - timedelta(hours=2), date_to=end,
    )
with transaction.atomic(), scope(organizer=organizer.pk):
    Order.objects.create(
        organizer=organizer, event=event, total=0, status=Order.STATUS_PAID,
        email="fixture@example.invalid",
        sales_channel=organizer.sales_channels.get(identifier="web"),
    )

command = [
    sys.executable, "-m", "celery", "-A", "pretix.celery_app:app", "worker",
    "--pool=solo", "--loglevel=WARNING", "-Q", "default,background,mail",
]
with subprocess.Popen(command) as worker:
    try:
        call_command("runperiodic", tasks="pretix_dataprotection_monitor.signals.schedule_monitor")
        deadline = monotonic() + 60
        while monotonic() < deadline:
            state = AuditRun.objects.get().state
            if state in (AuditRun.State.COMPLETED, AuditRun.State.FAILED):
                break
            if worker.poll() is not None:
                raise RuntimeError("The Celery worker exited before completing the dry run.")
            sleep(0.25)
        assert AuditRun.objects.get().state == AuditRun.State.COMPLETED
        assert EventAuditState.objects.get(event=event).status == Status.ACTION_REQUIRED
        assert not Reminder.objects.exists()
        assert not OutgoingMail.objects.exists()
        print("Native scheduler, Redis and Celery completed the dry run without creating mail.")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
