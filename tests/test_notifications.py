import smtplib
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone
from django_scopes import scope
from pretix.base.models import OutgoingMail

from pretix_dataprotection_monitor.audit import audit_event
from pretix_dataprotection_monitor.mailing import preview_email, resolve_recipient, send_test_email
from pretix_dataprotection_monitor.models import NotificationSettings, Reminder
from pretix_dataprotection_monitor.notifications import (
    Outcome, pending_reminder_ids, reconcile_reminder, request_reminder,
)
from pretix_dataprotection_monitor.templates_mail import render_message

from .test_audit import NOW, event, order  # noqa: F401

pytestmark = pytest.mark.django_db
FALLBACK = "administrator@example.invalid"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("pretix_dataprotection_monitor.notifications.timezone.now", lambda: NOW)


@pytest.fixture
def config():
    return NotificationSettings.objects.create(
        enabled=True, administrator_email=FALLBACK, initial_review_at=NOW,
    )


@pytest.mark.parametrize("contact, expected", [
    ("contact@example.invalid", ("contact@example.invalid", "contact")),
    (None, (FALLBACK, "fallback")),
    ("invalid address", (FALLBACK, "fallback")),
    ("recipient@example.invalid\r\nBcc:x@example.invalid", (FALLBACK, "fallback")),
])
def test_recipient_resolution(event, contact, expected):
    with scope(organizer=event.organizer_id):
        if contact is not None:
            event.settings.contact_mail = contact
        assert resolve_recipient(event, FALLBACK) == expected


def test_inherited_event_contact(event):
    with scope(organizer=event.organizer_id):
        event.organizer.settings.contact_mail = "organizer@example.invalid"
        event.refresh_from_db()
        assert resolve_recipient(event, FALLBACK) == ("organizer@example.invalid", "contact")


def test_disabled_and_configuration_validation(event, order):
    assert request_reminder(event.pk).outcome == Outcome.DISABLED
    config = NotificationSettings.objects.create()
    assert request_reminder(event.pk).outcome == Outcome.DISABLED
    config.enabled = True
    with pytest.raises(ValidationError):
        config.save()
    config.initial_review_at = NOW
    config.administrator_email = "invalid"
    with pytest.raises(ValidationError):
        config.save()
    assert not OutgoingMail.objects.exists()
    assert not Reminder.objects.exists()


@pytest.mark.parametrize("body", [
    "{unknown}", "{event_name.__class__}", "{event_name[0]}", "{event_name!r}",
    "{event_name:}", "{event_name:{event_end}}", "{{ event_name }}{", "{% debug %}",
])
def test_unsafe_templates_rejected(body):
    with pytest.raises(ValidationError):
        render_message("Subject", body, {})


def test_replacement_is_literal_and_preview_escaped():
    value = "<script>alert(1)</script> {organizer_name}\r\nBcc: injected"
    result = render_message("{event_name}", "{{event_name}} {event_name}", {"event_name": value})
    assert "\n" not in result.subject and "\r" not in result.subject
    assert result.body == "{event_name} " + value
    assert "{organizer_name}" in result.subject
    assert "&lt;script&gt;" in result.preview_html()
    assert "<script>" not in result.preview_html()


def test_test_mail_uses_synthetic_data_and_fallback_when_disabled():
    config = NotificationSettings(administrator_email=FALLBACK)
    message = preview_email(config)
    native = send_test_email(config)
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [FALLBACK]
    assert mail.outbox[0].subject == message.subject
    assert message.body in mail.outbox[0].body
    assert native.headers["X-Pretix-Data-Protection-Monitor"] == "test"
    assert not Reminder.objects.exists()


def test_native_delivery_and_repeat_interval(event, order, config, monkeypatch):
    with scope(organizer=event.organizer_id):
        event.settings.contact_mail = "contact@example.invalid"
        event.settings.mail_bcc = "bcc@example.invalid"
    result = request_reminder(event.pk)
    assert result.outcome == Outcome.QUEUED
    assert pending_reminder_ids() == [result.reminder_id]
    row = reconcile_reminder(result.reminder_id)
    assert row.state == Reminder.State.SENT
    assert row.sent_at is not None
    assert row.recipient_source == "contact"
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["contact@example.invalid"]
    assert not mail.outbox[0].bcc
    assert "customer@example.invalid" not in mail.outbox[0].body
    assert OutgoingMail.objects.get(pk=row.outgoing_mail_id).event_id is None
    assert "unclassified_event_email" not in audit_event(event.pk, now=NOW).review
    assert request_reminder(event.pk).outcome == Outcome.COOLDOWN
    monkeypatch.setattr(timezone, "now", lambda: row.sent_at + timedelta(days=14))
    assert request_reminder(event.pk).outcome == Outcome.QUEUED
    assert list(Reminder.objects.order_by("occurrence").values_list("occurrence", flat=True)) == [1, 2]
    assert len(mail.outbox) == 2


def test_invalid_config_blocks_and_recovers_same_occurrence(event, order, config):
    NotificationSettings.objects.filter(pk=1).update(administrator_email="invalid")
    blocked = request_reminder(event.pk)
    assert blocked.outcome == Outcome.BLOCKED
    assert request_reminder(event.pk) == blocked
    assert not OutgoingMail.objects.exists()
    config.save()
    result = request_reminder(event.pk)
    assert result.outcome == Outcome.QUEUED
    assert result.reminder_id == blocked.reminder_id
    row = reconcile_reminder(result.reminder_id)
    assert row.recipient_source == "fallback"
    assert mail.outbox[0].to == [FALLBACK]


def test_transient_native_failure_retries_same_mail(event, order, config):
    from django.core.mail.backends.locmem import EmailBackend

    calls = []
    original = EmailBackend.send_messages

    def fail_once(backend, messages):
        calls.append(messages[0].extra_headers["X-PX-Correlation"])
        if len(calls) == 1:
            raise smtplib.SMTPResponseException(421, b"fixture temporary failure")
        return original(backend, messages)

    with patch.object(EmailBackend, "send_messages", fail_once):
        result = request_reminder(event.pk)
    row = reconcile_reminder(result.reminder_id)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert row.state == Reminder.State.SENT
    assert len(mail.outbox) == 1
    assert OutgoingMail.objects.count() == Reminder.objects.count() == 1


def test_permanent_failure_and_expired_mail_never_create_duplicate(event, order, config):
    from django.core.mail.backends.locmem import EmailBackend

    with patch.object(EmailBackend, "send_messages", side_effect=smtplib.SMTPResponseException(550, b"fixture permanent failure")):
        result = request_reminder(event.pk)
    row = reconcile_reminder(result.reminder_id)
    assert row.state == Reminder.State.FAILED
    assert row.sent_at is None
    OutgoingMail.objects.filter(pk=row.outgoing_mail_id).delete()
    assert reconcile_reminder(row.pk).state == Reminder.State.FAILED
    assert request_reminder(event.pk).outcome == Outcome.BLOCKED
    assert Reminder.objects.count() == 1
    assert not getattr(mail, "outbox", [])


def test_pending_and_rate_limit(event, order, config):
    config.hourly_limit = 1
    config.save()
    with patch("pretix.base.services.mail.chain"):
        result = request_reminder(event.pk)
    assert pending_reminder_ids() == [result.reminder_id]
    assert request_reminder(event.pk).outcome == Outcome.PENDING
    Reminder.objects.filter(pk=result.reminder_id).update(
        state=Reminder.State.SENT, sent_at=NOW - timedelta(days=15),
    )
    assert request_reminder(event.pk).outcome == Outcome.THROTTLED
    assert OutgoingMail.objects.count() == 1


def test_deferral_and_test_data_are_suppressed(event, order, config):
    assert request_reminder(event.pk, deferred_until=NOW + timedelta(days=1)).outcome == Outcome.INELIGIBLE
    with scope(organizer=event.organizer_id):
        order.__class__.objects.filter(pk=order.pk).update(testmode=True)
    assert request_reminder(event.pk).outcome == Outcome.TEST_DATA
    assert not OutgoingMail.objects.exists()


def test_reminder_rolls_back_with_native_mail(event, order, config, settings, django_capture_on_commit_callbacks):
    from django.db import transaction

    settings.EMAIL_BACKEND = "tests.mail_backend.EmailBackend"
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                assert request_reminder(event.pk).outcome == Outcome.QUEUED
                raise RuntimeError("fixture rollback")
    assert not callbacks
    assert not Reminder.objects.exists()
    assert not OutgoingMail.objects.exists()
    assert not getattr(mail, "outbox", [])


def test_successful_history_is_retained_after_native_cleanup(event, order, config):
    result = request_reminder(event.pk)
    row = reconcile_reminder(result.reminder_id)
    assert row.state == Reminder.State.SENT
    OutgoingMail.objects.filter(pk=row.outgoing_mail_id).delete()
    refreshed = reconcile_reminder(row.pk)
    assert refreshed.state == Reminder.State.SENT
    assert refreshed.sent_at == row.sent_at


def test_missing_unsettled_native_mail_requires_review(event, order, config):
    with patch("pretix.base.services.mail.chain"):
        result = request_reminder(event.pk)
    row = Reminder.objects.get(pk=result.reminder_id)
    OutgoingMail.objects.filter(pk=row.outgoing_mail_id).delete()
    assert reconcile_reminder(row.pk).state == Reminder.State.NEEDS_REVIEW
    assert request_reminder(event.pk).outcome == Outcome.BLOCKED
    assert Reminder.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_overlapping_requests_create_one_reminder(event, order, config):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep

    from django.db import OperationalError, connection, connections

    barrier = Barrier(2)
    sqlite = connection.vendor == "sqlite"

    def run():
        try:
            barrier.wait(timeout=10)
            for attempt in range(20):
                try:
                    return request_reminder(event.pk).outcome
                except OperationalError as error:
                    # SQLite uses database/table locking. PostgreSQL takes
                    # the row lock and waits without retrying the transaction.
                    if not sqlite or "locked" not in str(error) or attempt == 19:
                        raise
                    sleep(0.02 * (attempt + 1))
        finally:
            connections.close_all()

    with patch("pretix.base.services.mail.chain"), ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(run), pool.submit(run)
        assert {first.result(timeout=20), second.result(timeout=20)} == {Outcome.QUEUED, Outcome.PENDING}
    assert OutgoingMail.objects.count() == Reminder.objects.count() == 1


def test_native_requeue_after_unconfirmed_attempt_is_withheld(event, order, config):
    from pretix.base.services.mail import mail_send_task

    from pretix_dataprotection_monitor.models import DeliveryAttempt

    with patch("pretix.base.services.mail.chain"):
        result = request_reminder(event.pk)
    row = Reminder.objects.get(pk=result.reminder_id)
    DeliveryAttempt.objects.create(outgoing_mail_id=row.outgoing_mail_id)
    # This models the native watchdog requeuing an interrupted worker's
    # outgoing row. There is no recorded SMTP rejection to authorize replay.
    assert mail_send_task.apply(kwargs={"outgoing_mail": row.outgoing_mail_id}).get() is False
    refreshed = reconcile_reminder(row.pk)
    assert refreshed.state == Reminder.State.NEEDS_REVIEW
    assert refreshed.error_code == "dp_monitor_delivery_unconfirmed"
    assert not getattr(mail, "outbox", [])


def test_disabled_after_queue_withholds_native_delivery(event, order, config):
    from pretix.base.services.mail import mail_send_task

    with patch("pretix.base.services.mail.chain"):
        result = request_reminder(event.pk)
    row = Reminder.objects.get(pk=result.reminder_id)
    config.enabled = False
    config.save()
    assert mail_send_task.apply(kwargs={"outgoing_mail": row.outgoing_mail_id}).get() is False
    assert reconcile_reminder(row.pk).error_code == "dp_monitor_delivery_disabled"
    assert not getattr(mail, "outbox", [])


def test_ambiguous_transport_error_does_not_reach_smtp_twice(event, order, config):
    from django.core.mail.backends.locmem import EmailBackend

    with patch.object(EmailBackend, "send_messages", side_effect=OSError("fixture uncertain disconnect")) as send:
        result = request_reminder(event.pk)
    assert send.call_count == 1
    assert reconcile_reminder(result.reminder_id).error_code == "dp_monitor_delivery_unconfirmed"


def test_test_email_has_safe_clickable_html_and_plain_text():
    body = ('Open [the guide](https://example.org/guide?a=1&b=2), or visit '
            'https://example.org/help. <script>example</script> [unsafe](javascript:alert(1))')
    config = NotificationSettings(administrator_email=FALLBACK, body=body)
    send_test_email(config)
    message = mail.outbox[0]
    assert body in message.body
    rendered, mimetype = message.alternatives[0]
    assert mimetype == "text/html"
    assert '<a href="https://example.org/guide?a=1&amp;b=2">the guide</a>' in rendered
    assert '<a href="https://example.org/help">https://example.org/help</a>.' in rendered
    assert "&lt;script&gt;example&lt;/script&gt;" in rendered
    assert '<a href="javascript:' not in rendered
