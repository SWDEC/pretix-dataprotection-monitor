import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django_scopes import scopes_disabled
from pretix.base.models import Event, Organizer
from pretix.celery_app import app

from .audit import Status
from .discovery import event_ids
from .models import AuditRun, EventAuditState, NotificationSettings, Reminder
from .notifications import pending_reminder_ids, reconcile_reminder

logger = logging.getLogger(__name__)
STALE_AFTER = timedelta(hours=2)


def _dispatch(run_id):
    try:
        run_audit.delay(run_id)
    except Exception:
        AuditRun.objects.filter(pk=run_id, state=AuditRun.State.QUEUED).update(
            state=AuditRun.State.FAILED, active_slot=None, finished_at=timezone.now(),
            error_code="queue_unavailable",
        )
        logger.error("Data protection audit %s could not be queued", run_id)


@transaction.atomic
def enqueue_audit(*, automatic=False):
    config, _ = NotificationSettings.objects.get_or_create()
    config = NotificationSettings.objects.select_for_update().get(pk=config.pk)
    now = timezone.now()
    AuditRun.objects.filter(active_slot=1, heartbeat_at__lte=now - STALE_AFTER).update(
        active_slot=None, state=AuditRun.State.FAILED, error_code="worker_timeout",
        finished_at=now,
    )
    if automatic:
        previous = AuditRun.objects.filter(active_slot__isnull=True).order_by("-pk").first()
        if previous and (
            config.scope_changed_at is None or previous.created_at >= config.scope_changed_at
        ):
            hours = config.audit_hours if (
                previous.state == AuditRun.State.COMPLETED and not previous.scan_errors
            ) else 1
            if previous.finished_at and now < previous.finished_at + timedelta(hours=hours):
                return previous, False
    run, created = AuditRun.objects.get_or_create(active_slot=1, defaults={"heartbeat_at": now})
    if created:
        transaction.on_commit(lambda: _dispatch(run.pk))
    return run, created


@transaction.atomic
def _check_event(run_id, event_id, grace_days):
    run = AuditRun.objects.select_for_update().filter(
        pk=run_id, state=AuditRun.State.RUNNING,
    ).first()
    if run is None:
        return False
    with scopes_disabled():
        event_exists = Event.objects.filter(pk=event_id).exists()
    if not event_exists:
        return True
    state, _ = EventAuditState.objects.select_for_update().get_or_create(event_id=event_id)
    result = state.run_check(grace_days=grace_days, deferred_until=state.deferred_until)
    if result.status != Status.DEFERRED:
        run.events_checked += 1
        run.scan_errors += result.status == Status.SCAN_ERROR
    run.heartbeat_at = timezone.now()
    run.save(update_fields=["events_checked", "scan_errors", "heartbeat_at"])
    return True


@app.task(name="pretix_dataprotection_monitor.run_audit", ignore_result=True)
def run_audit(run_id):
    if not AuditRun.objects.filter(pk=run_id, state=AuditRun.State.QUEUED).update(
        state=AuditRun.State.RUNNING, heartbeat_at=timezone.now(),
    ):
        return
    try:
        config = NotificationSettings.objects.get(pk=1)
        with scopes_disabled():
            organizer_count = Organizer.objects.exclude(pk__in=config.excluded_organizers).count()
            upper_id = Event.objects.order_by("-pk").values_list("pk", flat=True).first()
        cursor = 0
        while upper_id is not None and cursor < upper_id:
            page = event_ids(
                after=cursor, excluded_organizers=config.excluded_organizers,
                excluded_events=config.excluded_events,
            )
            page = [pk for pk in page if pk <= upper_id]
            if not page:
                break
            for pk in page:
                if not _check_event(run_id, pk, config.grace_days):
                    return
            cursor = page[-1]
        for reminder_id in pending_reminder_ids(limit=100):
            try:
                reconcile_reminder(reminder_id)
            except Reminder.DoesNotExist:
                continue
        AuditRun.objects.filter(pk=run_id, state=AuditRun.State.RUNNING).update(
            organizers_checked=organizer_count, active_slot=None,
            state=AuditRun.State.COMPLETED, finished_at=timezone.now(),
        )
    except Exception:
        AuditRun.objects.filter(pk=run_id, state=AuditRun.State.RUNNING).update(
            state=AuditRun.State.FAILED, active_slot=None, finished_at=timezone.now(),
            error_code="audit_failed",
        )
        logger.error("Data protection audit %s failed", run_id)
