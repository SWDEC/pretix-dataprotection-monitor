import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, OutgoingMail

from .audit import Status
from .mailing import preview_email, queue_message, resolve_recipient, valid_address
from .models import EventAuditState, NotificationSettings, Reminder
from .signals import DELIVERY_DISABLED, DELIVERY_INELIGIBLE, DELIVERY_UNCONFIRMED

logger = logging.getLogger(__name__)
IN_PROGRESS = (Reminder.State.QUEUED, Reminder.State.RETRYING)


class Outcome(StrEnum):
    DISABLED = "disabled"
    INVALID_CONFIGURATION = "invalid_configuration"
    INELIGIBLE = "ineligible"
    TEST_DATA = "test_data"
    PENDING = "pending"
    COOLDOWN = "cooldown"
    THROTTLED = "throttled"
    BLOCKED = "blocked"
    QUEUED = "queued"


@dataclass(frozen=True)
class NotificationResult:
    outcome: Outcome
    reminder_id: int | None = None


def _sync(reminder, *, now):
    native = OutgoingMail.objects.filter(pk=reminder.outgoing_mail_id).values(
        "status", "sent", "inflight_since", "error"
    ).first()
    state, error_code, sent_at = reminder.state, reminder.error_code, reminder.sent_at
    if native is None:
        if reminder.state in (Reminder.State.SENT, Reminder.State.FAILED):
            return reminder
        state, error_code = Reminder.State.NEEDS_REVIEW, "mail_record_missing"
    elif native["status"] == OutgoingMail.STATUS_SENT and native["sent"] is not None:
        state, error_code, sent_at = Reminder.State.SENT, "", native["sent"]
    elif native["status"] == OutgoingMail.STATUS_AWAITING_RETRY:
        state, error_code = Reminder.State.RETRYING, "transient_mail_failure"
    elif native["status"] == OutgoingMail.STATUS_QUEUED:
        state, error_code = Reminder.State.QUEUED, ""
    elif native["status"] == OutgoingMail.STATUS_INFLIGHT:
        inflight = native["inflight_since"]
        if inflight and inflight <= now - timedelta(minutes=45):
            state, error_code = Reminder.State.NEEDS_REVIEW, "delivery_unconfirmed"
    elif native["status"] == OutgoingMail.STATUS_WITHHELD and native["error"] in (
        DELIVERY_DISABLED, DELIVERY_INELIGIBLE, DELIVERY_UNCONFIRMED,
    ):
        state, error_code = Reminder.State.NEEDS_REVIEW, native["error"]
    elif native["status"] in (
        OutgoingMail.STATUS_FAILED, OutgoingMail.STATUS_ABORTED,
        OutgoingMail.STATUS_BOUNCED, OutgoingMail.STATUS_WITHHELD,
    ):
        state, error_code = Reminder.State.FAILED, native["status"]
    else:
        state, error_code = Reminder.State.NEEDS_REVIEW, "unknown_mail_status"

    if (state, error_code, sent_at) != (reminder.state, reminder.error_code, reminder.sent_at):
        reminder.state, reminder.error_code, reminder.sent_at = state, error_code, sent_at
        reminder.save(update_fields=["state", "error_code", "sent_at"])
    return reminder


@transaction.atomic
def reconcile_reminder(reminder_id):
    reminder = Reminder.objects.select_for_update().get(pk=reminder_id)
    if reminder.outgoing_mail_id is not None:
        _sync(reminder, now=timezone.now())
    return reminder


def pending_reminder_ids(*, after=0, limit=100, event_id=None):
    if not 1 <= limit <= 1000:
        raise ValueError("Page size must be between 1 and 1000.")
    unsettled = Q(state__in=IN_PROGRESS) | Q(
        state=Reminder.State.NEEDS_REVIEW, error_code="delivery_unconfirmed"
    )
    reminders = Reminder.objects.filter(unsettled, pk__gt=after)
    if event_id is not None:
        reminders = reminders.filter(event_id=event_id)
    return list(reminders.order_by("pk").values_list("pk", flat=True)[:limit])


def _record_block(latest, event_id, code):
    if latest and latest.state == Reminder.State.BLOCKED:
        if latest.error_code == code:
            return latest
        latest.error_code = code
        latest.save(update_fields=["error_code"])
        row = latest
    else:
        row = Reminder.objects.create(
            event_id=event_id, occurrence=(latest.occurrence + 1 if latest else 1),
            state=Reminder.State.BLOCKED, error_code=code,
        )
    logger.warning("Data protection reminder blocked for event %s (%s)", event_id, code)
    return row


def _at_hourly_limit(config, now):
    return Reminder.objects.filter(queued_at__gt=now - timedelta(hours=1)).count() >= config.hourly_limit


@transaction.atomic
def request_reminder(event_id, *, grace_days=None, deferred_until=None, scheduled=False):
    """Queue the next eligible occurrence. All live callers share this gate."""
    config = NotificationSettings.objects.select_for_update().filter(pk=1).first()
    if config is None or not config.enabled:
        return NotificationResult(Outcome.DISABLED)
    now = timezone.now()
    if not config.initial_review_at or config.initial_review_at > now:
        return NotificationResult(Outcome.INVALID_CONFIGURATION)

    with scopes_disabled():
        event = Event.objects.select_related("organizer").filter(pk=event_id).first()
    if (
        event is None or event_id in config.excluded_events
        or event.organizer_id in config.excluded_organizers
    ):
        return NotificationResult(Outcome.INELIGIBLE)
    state, _ = EventAuditState.objects.select_for_update().get_or_create(event_id=event_id)
    if state.deferred_until and state.deferred_until > now:
        return NotificationResult(Outcome.INELIGIBLE)

    latest = Reminder.objects.select_for_update().filter(event_id=event_id).order_by(
        "-occurrence"
    ).first()
    if latest and latest.outgoing_mail_id and (
        latest.state in IN_PROGRESS or latest.error_code == "delivery_unconfirmed"
    ):
        _sync(latest, now=now)
    if latest and latest.state in IN_PROGRESS:
        return NotificationResult(Outcome.PENDING, latest.pk)
    if latest and latest.state in (Reminder.State.FAILED, Reminder.State.NEEDS_REVIEW):
        return NotificationResult(Outcome.BLOCKED, latest.pk)
    if latest and latest.sent_at and now < latest.sent_at + timedelta(days=config.reminder_days):
        return NotificationResult(Outcome.COOLDOWN, latest.pk)

    if scheduled and _at_hourly_limit(config, now):
        return NotificationResult(Outcome.THROTTLED)

    result = state.run_check(
        now=now, grace_days=config.grace_days if grace_days is None else grace_days,
        deferred_until=deferred_until,
    )
    if result.status != Status.ACTION_REQUIRED:
        return NotificationResult(Outcome.INELIGIBLE)
    # The audit counters are event-wide. If an event mixes test and live
    # orders, the origin of a specific finding requires manual review.
    if result.notices.get("test_mode") or result.notices.get("test_orders"):
        return NotificationResult(Outcome.TEST_DATA)

    if not valid_address(config.administrator_email):
        row = _record_block(latest, event_id, "invalid_administrator_email")
        return NotificationResult(Outcome.BLOCKED, row.pk)
    with scope(organizer=event.organizer_id):
        address, source = resolve_recipient(event, config.administrator_email)
        if address is None:
            row = _record_block(latest, event_id, "no_valid_recipient")
            return NotificationResult(Outcome.BLOCKED, row.pk)
        try:
            message = preview_email(config, event=event, result=result)
        except ValidationError:
            row = _record_block(latest, event_id, "invalid_template")
            return NotificationResult(Outcome.BLOCKED, row.pk)

    if not scheduled and _at_hourly_limit(config, now):
        return NotificationResult(Outcome.THROTTLED)
    outgoing = queue_message(address, message, kind="reminder")
    if outgoing is None:
        row = _record_block(latest, event_id, "mail_not_queued")
        return NotificationResult(Outcome.BLOCKED, row.pk)
    row = latest if latest and latest.state == Reminder.State.BLOCKED else Reminder(
        event_id=event_id, occurrence=(latest.occurrence + 1 if latest else 1)
    )
    row.outgoing_mail_id = outgoing.pk
    row.queued_at = now
    row.state = Reminder.State.QUEUED
    row.recipient_source = source
    row.error_code = ""
    row.save()
    return NotificationResult(Outcome.QUEUED, row.pk)
