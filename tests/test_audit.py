from datetime import UTC, datetime, timedelta
from importlib.metadata import version

import pytest
from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_scopes import scope, scopes_disabled
from pretix.base import models as core

from pretix_dataprotection_monitor.audit import Status, audit_event
from pretix_dataprotection_monitor.discovery import event_ids, organizer_ids

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


@pytest.fixture
def event():
    with scopes_disabled():
        organizer = core.Organizer.objects.create(name="Fixture", slug="fixture")
        return core.Event.objects.create(
            organizer=organizer, name="Audit fixture", slug="fixture",
            date_from=NOW - timedelta(days=90), date_to=NOW - timedelta(days=89),
        )


@pytest.fixture
def order(event):
    with scope(organizer=event.organizer_id):
        event.organizer.create_default_sales_channels()
        return core.Order.objects.create(
            event=event, organizer=event.organizer, total=0, status=core.Order.STATUS_PAID,
            email="customer@example.invalid",
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )


def test_runtime():
    expected = {
        "pretix": "2026.9.2", "Django": "5.2.19", "celery": "5.6.3",
        "pretix-passbook": "1.14.1", "pretix-attendance-certificate-neo": "1.0.1",
        "pretix-fontpack-free": "1.11.2", "pretix-sofort": "1.4.2",
    }
    assert {package: version(package) for package in expected} == expected
    for app in ("pretix_passbook", "pretix_attendance_certificate_neo", "pretix_fontpackfree",
                "pretix_sofort", "pretix_dataprotection_monitor"):
        assert apps.is_installed(app)


def test_future_and_grace(event):
    with scope(organizer=event.organizer_id):
        event.date_to = NOW + timedelta(days=1)
        event.save()
        assert audit_event(event.pk, now=NOW, grace_days=0).status == Status.NOT_DUE
        event.date_to = NOW - timedelta(days=10)
        event.save()
        assert audit_event(event.pk, now=NOW).status == Status.NOT_DUE


def test_empty_and_live(event):
    assert audit_event(event.pk, now=NOW).status == Status.NO_KNOWN_IDENTIFIERS
    event.live = True
    event.save()
    result = audit_event(event.pk, now=NOW)
    assert result.status == Status.MANUAL_REVIEW
    assert result.notices["shop_online"] == 1


def test_series_future_inactive_date_and_missing_dates(event):
    event.has_subevents = True
    event.save()
    assert audit_event(event.pk, now=NOW).status == Status.MANUAL_REVIEW
    with scope(organizer=event.organizer_id):
        core.SubEvent.objects.create(event=event, name="Earlier", date_from=NOW - timedelta(days=70))
        next_date = core.SubEvent.objects.create(
            event=event, name="Future", date_from=NOW + timedelta(days=1), active=False,
        )
        assert audit_event(event.pk, now=NOW).status == Status.NOT_DUE
        next_date.delete()
        result = audit_event(event.pk, now=NOW)
    assert result.status == Status.NO_KNOWN_IDENTIFIERS
    assert result.schedule.inferred_end


def test_cross_organizer_discovery_and_exclusions(event):
    with scopes_disabled():
        second = core.Organizer.objects.create(name="Another", slug="another")
        other = core.Event.objects.create(
            organizer=second, name="Another event", slug="another",
            date_from=event.date_from,
        )
    with scope(organizer=event.organizer_id):
        assert event_ids(limit=1) == [event.pk]
        assert event_ids(after=event.pk) == [other.pk]
        assert event_ids(excluded_organizers=[second.pk]) == [event.pk]
        assert event_ids(excluded_events=[event.pk]) == [other.pk]
        assert organizer_ids() == [event.organizer_id, second.pk]


def test_partial_shredding_and_reintroduced_data(event, order):
    with scope(organizer=event.organizer_id):
        item = core.Item.objects.create(event=event, name="Ticket", default_price=0)
        position = core.OrderPosition.all.create(
            order=order, item=item, price=0, positionid=1,
            attendee_name_parts={"_legacy": "Fixture Attendee"}, canceled=True,
        )
        assert audit_event(event.pk, now=NOW).identifiers == {
            "customer_emails": 1, "attendee_names": 1,
        }
        core.Order.objects.filter(pk=order.pk).update(email=None)
        event.log_action("pretix.event.shredder.completed", data={"indexdata": {"shredders": ["order_emails"]}})
        assert audit_event(event.pk, now=NOW).identifiers == {"attendee_names": 1}
        core.OrderPosition.all.filter(pk=position.pk).update(
            attendee_name_cached=None, attendee_name_parts={"_shredded": True},
        )
        result = audit_event(event.pk, now=NOW)
        assert result.status == Status.NO_KNOWN_IDENTIFIERS
        assert result.notices["completed_shredder_runs"] == 1
        core.OrderPosition.all.filter(pk=position.pk).update(
            attendee_name_parts={"_shredded": True, "given_name": "Reintroduced"},
        )
        assert audit_event(event.pk, now=NOW).identifiers == {"attendee_names": 1}


def test_deferral_expires_and_missing_event(event, order):
    assert audit_event(event.pk, now=NOW, deferred_until=NOW + timedelta(days=1)).status == Status.DEFERRED
    assert audit_event(event.pk, now=NOW, deferred_until=NOW).status == Status.ACTION_REQUIRED
    assert audit_event(999999, now=NOW).status == Status.SCAN_ERROR


def test_installed_plugin_data(event, order):
    with scope(organizer=event.organizer_id):
        order.log_action("pretix_attendance_certificate_neo.sent.attendee", data={
            "recipient": "certificate@example.invalid", "message": "Fixture",
        })
        core.LogEntry.all.filter(action_type="pretix_attendance_certificate_neo.sent.attendee").update(
            shredded=True
        )
        core.CachedCombinedTicket.objects.create(order=order, provider="passbook", file="fixture.pkpass")
        core.OrderPayment.objects.create(
            order=order, provider="sofort", amount=0,
            info='{"_shreded": true, "transaction": "reference"}',
        )
        result = audit_event(event.pk, now=NOW)
    assert result.identifiers["certificate_logs"] == 1
    assert result.review["generated_tickets"] == 1
    assert result.review["payment_metadata"] == 1
    assert "example.invalid" not in repr(result)


def test_large_event_has_bounded_queries_and_read_only_sql(event, order):
    with scope(organizer=event.organizer_id):
        item = core.Item.objects.create(event=event, name="Ticket", default_price=0)
        core.OrderPosition.all.bulk_create([
            core.OrderPosition(
                order=order, organizer_id=event.organizer_id, item=item,
                price=0, tax_value=0, tax_rate=0, positionid=i,
                attendee_email="fixture@example.invalid", secret=f"fixture-{i}",
                pseudonymization_id=f"audit-{i}",
            )
            for i in range(1, 2501)
        ])
    with CaptureQueriesContext(connection) as queries:
        result = audit_event(event.pk, now=NOW)
    assert result.identifiers["attendee_emails"] == 2500
    assert len(queries) < 55
    for query in queries:
        sql = query["sql"].lstrip().upper()
        if connection.vendor == "postgresql" and sql.startswith("DECLARE "):
            # PostgreSQL's server-side iterator wraps SELECT in a cursor.
            assert " CURSOR " in sql and " FOR SELECT " in sql
        else:
            assert sql.startswith("SELECT"), sql.split(maxsplit=1)[0]


def test_real_email_shredder_leaves_attendee_name(event, order, monkeypatch):
    from pretix.base import shredder

    monkeypatch.setattr(shredder.time, "sleep", lambda _: None)
    with scope(organizer=event.organizer_id):
        item = core.Item.objects.create(event=event, name="Ticket", default_price=0)
        core.OrderPosition.all.create(
            order=order, item=item, price=0, positionid=1,
            attendee_name_parts={"_legacy": "Fixture Attendee"},
            attendee_email="attendee@example.invalid",
        )
        assert audit_event(event.pk, now=NOW).identifiers["attendee_emails"] == 1
        shredder.EmailAddressShredder(event).shred_data()
        assert audit_event(event.pk, now=NOW).identifiers == {"attendee_names": 1}
        shredder.AttendeeInfoShredder(event).shred_data()
        assert audit_event(event.pk, now=NOW).status == Status.NO_KNOWN_IDENTIFIERS


def test_mail_exclusion_and_certificate_attachment(event, order):
    with scope(organizer=event.organizer_id):
        reminder = core.OutgoingMail.objects.create(
            event=event, organizer=event.organizer, to=["admin@example.invalid"],
            subject="Fixture reminder", body_plain="Aggregate findings",
        )
        certificate = core.OutgoingMail.objects.create(
            event=event, organizer=event.organizer, order=order,
            to=["attendee@example.invalid"], subject="Fixture certificate", body_plain="Fixture",
        )
        attachment = core.CachedFile.objects.create(
            filename="certificate_of_attendance.pdf", file="fixture.pdf",
            expires=NOW + timedelta(days=1),
        )
        certificate.should_attach_cached_files.add(attachment)
    result = audit_event(event.pk, now=NOW, excluded_mail_ids=[reminder.pk])
    assert result.identifiers["outgoing_customer_email"] == 1
    assert result.review["certificate_attachments"] == 1
    assert "unclassified_event_email" not in result.review


def test_question_types_and_testmode(event, order):
    with scope(organizer=event.organizer_id):
        phone = core.Question.objects.create(
            event=event, question="Fixture phone", type=core.Question.TYPE_PHONENUMBER,
        )
        text = core.Question.objects.create(
            event=event, question="Fixture free text", type=core.Question.TYPE_TEXT,
        )
        core.QuestionAnswer.objects.create(order=order, question=phone, answer="+12025550123")
        core.QuestionAnswer.objects.create(order=order, question=text, answer="An example")
        core.Order.objects.filter(pk=order.pk).update(testmode=True, email=None)
    result = audit_event(event.pk, now=NOW)
    assert result.identifiers == {"question_contact_answers": 1}
    assert result.review == {"other_question_answers": 1}
    assert result.notices["test_orders"] == 1


def test_command_returns_counts(event, order):
    import io
    import json

    from django.core.management import call_command

    stdout = io.StringIO()
    call_command("audit_data_protection", event_id=event.pk, grace_days=0, stdout=stdout)
    payload = json.loads(stdout.getvalue())
    assert payload["event_id"] == event.pk
    assert payload["identifiers"] == {"customer_emails": 1}
    assert "example.invalid" not in stdout.getvalue()


def test_fallback_and_grace_use_event_local_calendar(event):
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    event.settings.timezone = "Europe/Berlin"
    event.date_from = datetime(2026, 3, 28, 17, tzinfo=berlin)
    event.date_to = None
    event.save()
    result = audit_event(
        event.pk, now=datetime(2026, 3, 29, 22, tzinfo=UTC), grace_days=1,
    )
    assert result.schedule.inferred_end
    assert result.schedule.end == datetime(2026, 3, 28, 23, tzinfo=UTC)
    assert result.schedule.due_at == datetime(2026, 3, 29, 22, tzinfo=UTC)
    assert result.status == Status.NO_KNOWN_IDENTIFIERS


def test_invoice_and_waiting_list(event, order):
    with scope(organizer=event.organizer_id):
        core.Order.objects.filter(pk=order.pk).update(email=None)
        core.InvoiceAddress.objects.create(order=order, name_cached="Fixture buyer")
        result = audit_event(event.pk, now=NOW)
        assert result.status == Status.MANUAL_REVIEW
        assert result.review["invoice_addresses"] == 1

        item = core.Item.objects.create(event=event, name="Ticket", default_price=0)
        waiting = core.WaitingListEntry.objects.create(
            event=event, item=item, email="waiting@example.invalid",
        )
        assert audit_event(event.pk, now=NOW).identifiers["waiting_list_contacts"] == 1
        core.WaitingListEntry.objects.filter(pk=waiting.pk).update(
            email="█", phone="█", name_cached=None, name_parts={"_shredded": True},
        )
        assert audit_event(event.pk, now=NOW).status == Status.MANUAL_REVIEW
