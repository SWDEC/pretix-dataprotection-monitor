from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone
from django_scopes import scopes_disabled
from pretix.base.models import Event, Organizer, OutgoingMail

from pretix_dataprotection_monitor.audit import Status
from pretix_dataprotection_monitor.models import (
    AuditRun, DeliverySchedule, EventAuditState, NotificationSettings, Reminder,
)
from pretix_dataprotection_monitor.runs import enqueue_audit
from pretix_dataprotection_monitor.scheduler import delivery_tick, enqueue_delivery_tick

from .test_audit import event, order  # noqa: F401

pytestmark = pytest.mark.django_db
RECEIVER = "pretix_dataprotection_monitor.signals.schedule_monitor"


@pytest.mark.django_db(transaction=True)
def test_native_periodic_dry_run_discovers_new_organizer(event, order):
    call_command("runperiodic", tasks=RECEIVER, verbosity=0)
    run = AuditRun.objects.get()
    assert run.state == AuditRun.State.COMPLETED
    assert run.events_checked == 1
    assert EventAuditState.objects.get(event=event).status == Status.ACTION_REQUIRED
    assert not OutgoingMail.objects.exists()

    with scopes_disabled():
        organizer = Organizer.objects.create(name="New organizer", slug="new-organizer")
        another = Event.objects.create(
            organizer=organizer, name="New event", slug="new-event",
            date_from=event.date_from, date_to=event.date_to,
        )
    call_command("runperiodic", tasks=RECEIVER, verbosity=0)
    assert AuditRun.objects.count() == 1
    assert not EventAuditState.objects.filter(event=another).exists()
    AuditRun.objects.filter(pk=run.pk).update(finished_at=timezone.now() - timedelta(hours=24))
    call_command("runperiodic", tasks=RECEIVER, verbosity=0)
    assert AuditRun.objects.latest("pk").organizers_checked == 2
    assert EventAuditState.objects.get(event=another).status == Status.NO_KNOWN_IDENTIFIERS
    assert not Reminder.objects.exists()


def test_worker_only_claims_delivery_job_once(event, order, django_capture_on_commit_callbacks):
    NotificationSettings.objects.create(
        enabled=True, administrator_email="ops@example.invalid", initial_review_at=timezone.now(),
    )
    EventAuditState.objects.create(event=event, status=Status.ACTION_REQUIRED, checked_at=timezone.now())
    with patch("pretix_dataprotection_monitor.scheduler._dispatch") as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            assert enqueue_delivery_tick()
            assert not enqueue_delivery_tick()
    token = dispatch.call_args.args[0]
    delivery_tick(str(token))
    delivery_tick(str(token))
    assert DeliverySchedule.objects.get().lease_token is None
    assert Reminder.objects.count() == OutgoingMail.objects.count() == len(mail.outbox) == 1

    DeliverySchedule.objects.update(next_tick_at=timezone.now() - timedelta(seconds=1))
    with django_capture_on_commit_callbacks(execute=True):
        assert enqueue_delivery_tick()
    assert Reminder.objects.get().state == Reminder.State.SENT
    assert len(mail.outbox) == 1


def test_configured_frequency_is_independent_of_periodic_tick(event, django_capture_on_commit_callbacks):
    NotificationSettings.objects.create(audit_hours=12)
    with django_capture_on_commit_callbacks(execute=True):
        first, _ = enqueue_audit(automatic=True)
    AuditRun.objects.filter(pk=first.pk).update(finished_at=timezone.now() - timedelta(hours=11))
    assert not enqueue_audit(automatic=True)[1]
    AuditRun.objects.filter(pk=first.pk).update(finished_at=timezone.now() - timedelta(hours=12))
    with django_capture_on_commit_callbacks(execute=True):
        second, created = enqueue_audit(automatic=True)
    assert created and second.pk != first.pk


def test_stale_queued_delivery_can_be_reclaimed(django_capture_on_commit_callbacks):
    with patch("pretix_dataprotection_monitor.scheduler._dispatch"):
        with django_capture_on_commit_callbacks(execute=True):
            assert enqueue_delivery_tick()
        old = DeliverySchedule.objects.get().lease_token
        DeliverySchedule.objects.update(
            lease_until=timezone.now() - timedelta(seconds=1),
            next_tick_at=timezone.now() - timedelta(minutes=5),
        )
        with django_capture_on_commit_callbacks(execute=True):
            assert enqueue_delivery_tick()
        new = DeliverySchedule.objects.get().lease_token
    assert old != new
    delivery_tick(str(old))
    assert DeliverySchedule.objects.get().lease_token == new
