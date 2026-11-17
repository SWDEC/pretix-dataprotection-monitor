import logging
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from pretix.celery_app import app

from .audit import Status
from .models import DeliverySchedule, EventAuditState, NotificationSettings, Reminder
from .notifications import Outcome, pending_reminder_ids, reconcile_reminder, request_reminder
from .runs import enqueue_audit

logger = logging.getLogger(__name__)
TICK_INTERVAL = timedelta(minutes=5)
LEASE_DURATION = timedelta(hours=1)
RECONCILE_LIMIT = 100
MAX_CANDIDATES = 1000


def _dispatch(token):
    try:
        delivery_tick.delay(str(token))
    except Exception:
        DeliverySchedule.objects.filter(pk=1, lease_token=token).update(
            lease_token=None, lease_until=None,
        )
        logger.error("Data protection delivery task could not be queued")


@transaction.atomic
def enqueue_delivery_tick():
    now = timezone.now()
    state, _ = DeliverySchedule.objects.select_for_update().get_or_create(
        pk=1, defaults={"next_tick_at": now},
    )
    if state.next_tick_at > now or (state.lease_until and state.lease_until > now):
        return False
    token = uuid4()
    state.next_tick_at = now + TICK_INTERVAL
    state.lease_until = now + LEASE_DURATION
    state.lease_token = token
    state.save(update_fields=["next_tick_at", "lease_until", "lease_token"])
    transaction.on_commit(lambda: _dispatch(token))
    return True


def periodic_tick():
    enqueue_audit(automatic=True)
    enqueue_delivery_tick()


def _candidate_ids(config, after):
    query = EventAuditState.objects.filter(
        event_id__gt=after, status=Status.ACTION_REQUIRED,
    ).exclude(event_id__in=config.excluded_events).exclude(
        event__organizer_id__in=config.excluded_organizers,
    ).filter(
        Q(deferred_until__isnull=True) | Q(deferred_until__lte=timezone.now()),
        Q(notices__test_orders__isnull=True) | Q(notices__test_orders=0),
        Q(notices__test_mode__isnull=True) | Q(notices__test_mode=0),
    )
    if config.scope_changed_at:
        query = query.filter(checked_at__gte=config.scope_changed_at)
    limit = min(MAX_CANDIDATES, max(100, config.hourly_limit))
    return list(query.order_by("event_id").values_list("event_id", flat=True)[:limit])


def _reconcile_page(after):
    ids = pending_reminder_ids(after=after, limit=RECONCILE_LIMIT)
    for pk in ids:
        try:
            reconcile_reminder(pk)
        except Reminder.DoesNotExist:
            continue
    return ids[-1] if len(ids) == RECONCILE_LIMIT else 0


@app.task(name="pretix_dataprotection_monitor.delivery_tick", ignore_result=True)
def delivery_tick(token):
    # Atomically replace the enqueue token with a private execution token.
    # Duplicate broker deliveries with the original token cannot take over.
    execution_token = uuid4()
    owned = DeliverySchedule.objects.filter(pk=1, lease_token=token, lease_until__gt=timezone.now())
    if not owned.update(lease_token=execution_token, lease_until=timezone.now() + LEASE_DURATION):
        return
    owned = DeliverySchedule.objects.filter(pk=1, lease_token=execution_token)
    try:
        state = owned.get()
        reconcile_cursor = _reconcile_page(state.reconcile_cursor)
        owned.update(reconcile_cursor=reconcile_cursor)
        config = NotificationSettings.objects.filter(pk=1, enabled=True).first()
        if config is None:
            return
        cursor = state.reminder_cursor
        ids = _candidate_ids(config, cursor)
        for event_id in ids:
            if not owned.filter(lease_until__gt=timezone.now()).exists():
                return
            result = request_reminder(event_id, scheduled=True)
            if result.outcome == Outcome.THROTTLED:
                break
            cursor = event_id
        else:
            # Start a new pass so earlier events can become eligible again.
            limit = min(MAX_CANDIDATES, max(100, config.hourly_limit))
            if len(ids) < limit:
                cursor = 0
        owned.update(reminder_cursor=cursor)
    finally:
        owned.update(lease_token=None, lease_until=None)
