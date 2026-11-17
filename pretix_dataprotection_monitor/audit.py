import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import reduce
from operator import or_
from zoneinfo import ZoneInfoNotFoundError

from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError
from django.db.models import Count, Q, TextField
from django.db.models.functions import Cast, Trim
from django.utils import timezone
from django_scopes import scope, scopes_disabled
from pretix.base import models as core
from pretix.base.models.orders import CheckoutSession

from .eligibility import Schedule, event_schedule


class Status(StrEnum):
    NOT_DUE = "not_due"
    NO_KNOWN_IDENTIFIERS = "no_known_identifiers"
    ACTION_REQUIRED = "action_required"
    MANUAL_REVIEW = "manual_review"
    DEFERRED = "deferred"
    SCAN_ERROR = "scan_error"


@dataclass(frozen=True)
class AuditResult:
    event_id: int
    checked_at: datetime
    status: Status
    schedule: Schedule | None = None
    identifiers: dict[str, int] = field(default_factory=dict)
    review: dict[str, int] = field(default_factory=dict)
    notices: dict[str, int] = field(default_factory=dict)
    error_code: str | None = None


# These exact values are written by the pinned core shredders. A marker
# alongside an additional populated field must never hide that field.
REDACTED = "█"
CLEAN_JSON = ({}, {"_shredded": True}, {"_legacy": REDACTED})
DIRECT_KEYS = frozenset({
    "attendee_name", "attendee_name_cached", "attendee_name_parts",
    "attendee_email", "email", "email_repeat", "phone", "old_phone",
    "new_phone", "old_email", "new_email", "first_name", "last_name",
    "given_name", "family_name", "recipient", "recipients", "payer_email",
    "payer_name", "sender_name", "iban", "account_holder", "street",
})
FINANCIAL_KEYS = frozenset({"invoice_data", "invoice_address"})
CERTIFICATE_ACTION = "pretix_attendance_certificate_neo.sent.attendee"


def _populated(value):
    if isinstance(value, str):
        return value.strip() not in ("", REDACTED)
    if isinstance(value, dict):
        return value not in CLEAN_JSON and any(
            _populated(v) for k, v in value.items() if k not in {"_shredded", "_shreded", "_scheme"}
        )
    if isinstance(value, list):
        return any(map(_populated, value))
    return value is not None


def _has_identifier(value):
    if isinstance(value, list):
        return any(map(_has_identifier, value))
    if not isinstance(value, dict):
        return False
    return any(
        (key.lower() in DIRECT_KEYS and _populated(item)) or _has_identifier(item)
        for key, item in value.items()
        if key not in FINANCIAL_KEYS
    )


def _payload(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return value
    return value


def _nonempty(field_name, *, redacted=False):
    condition = Q(**{f"{field_name}__isnull": False}) & ~Q(**{field_name: ""})
    return condition & ~Q(**{field_name: REDACTED}) if redacted else condition


def _text(query, *fields, redacted=False):
    # Coalesce nulls at the predicate level and let the database count rows.
    aliases = {f"monitor_text_{i}": Trim(name) for i, name in enumerate(fields)}
    predicate = reduce(or_, (_nonempty(name, redacted=redacted) for name in aliases))
    return query.alias(**aliases).filter(predicate).count()


def _counts(query, **conditions):
    return query.aggregate(**{
        category: Count("pk", filter=condition) for category, condition in conditions.items()
    })


def _records(query, *fields):
    return query.order_by().values_list(*fields).iterator(chunk_size=500)


def _audit_payloads(query, field_name, category, identifiers, review):
    for (raw,) in _records(query.filter(_nonempty(field_name)), field_name):
        value = _payload(raw)
        if _has_identifier(value):
            identifiers[category] += 1
        elif _populated(value):
            review[category] += 1


def _audit_logs(event, identifiers, review, notices):
    for action, raw in _records(
        core.LogEntry.all.filter(event=event).exclude(data__in=["", "{}"]),
        "action_type", "data",
    ):
        data = _payload(raw)
        if action == "pretix.event.shredder.completed":
            notices["completed_shredder_runs"] += 1
        elif action == "pretix.event.shredder.started":
            notices["started_shredder_runs"] += 1
        else:
            participant_action = (
                action == CERTIFICATE_ACTION or "order." in action
                or "position." in action or "waitinglist" in action
            )
            if isinstance(data, dict) and any(
                _populated(data.get(key)) for key in FINANCIAL_KEYS
            ):
                review["historical_invoice_data"] += 1
            if participant_action and _has_identifier(data):
                category = "certificate_logs" if action == CERTIFICATE_ACTION else "historical_logs"
                identifiers[category] += 1
            elif _populated(data) and (
                participant_action or _has_identifier(data) or not action.startswith("pretix.")
            ):
                review["unclassified_logs"] += 1


def _audit_current_data(event, excluded_mail_ids):
    identifiers, review, notices = Counter(), Counter(), Counter()
    orders = core.Order.objects.filter(event=event)
    positions = core.OrderPosition.all.filter(order__event=event)
    waiting = core.WaitingListEntry.objects.filter(event=event)
    answers = core.QuestionAnswer.objects.filter(
        Q(order__event=event) | Q(orderposition__order__event=event)
        | Q(cartposition__event=event) | Q(checkoutsession__event=event)
    )
    invoices = core.Invoice.objects.filter(event=event)
    addresses = core.InvoiceAddress.objects.filter(order__event=event)

    identifiers.update(_counts(orders, customer_accounts=Q(customer__isnull=False)))
    identifiers["customer_emails"] = _text(orders, "email")
    identifiers["customer_phones"] = orders.filter(_nonempty("phone")).count()
    identifiers["attendee_emails"] = _text(positions, "attendee_email")
    identifiers["attendee_names"] = sum(
        _populated(cached) or _populated(parts)
        for cached, parts in _records(positions.exclude(
            Q(attendee_name_cached__isnull=True) & Q(attendee_name_parts__isnull=True)
        ), "attendee_name_cached", "attendee_name_parts")
    )
    identifiers["attendee_contact_details"] = _text(positions, "company", "street")
    review["attendee_location"] = _text(positions, "zipcode", "city", "country", "state")
    identifiers["waiting_list_contacts"] = sum(
        any(map(_populated, row))
        for row in _records(
            waiting.annotate(monitor_phone=Cast("phone", TextField())),
            "email", "monitor_phone", "name_cached", "name_parts",
        )
    )
    identifiers["question_contact_answers"] = _text(
        answers.filter(question__type=core.Question.TYPE_PHONENUMBER), "answer"
    )
    review["other_question_answers"] = answers.exclude(
        question__type=core.Question.TYPE_PHONENUMBER
    ).filter(_nonempty("answer")).count()
    review["question_uploads"] = answers.filter(_nonempty("file")).count()

    carts = core.CartPosition.objects.filter(event=event)
    review["cart_contact_fields"] = _text(
        carts, "attendee_email", "attendee_name_cached", "company", "street",
    )
    _audit_payloads(carts, "meta_info", "cart_metadata", identifiers, review)
    sessions = CheckoutSession.objects.filter(event=event)
    identifiers["checkout_customer_accounts"] = sessions.filter(customer__isnull=False).count()
    _audit_payloads(sessions, "session_data", "checkout_metadata", identifiers, review)

    review["invoice_addresses"] = addresses.count()
    review["invoice_documents"] = invoices.filter(_nonempty("file")).count()
    review["invoice_recipient_fields"] = _text(
        invoices, "invoice_to", "invoice_to_name", "invoice_to_company",
        "invoice_to_street", "invoice_to_city", "invoice_to_zipcode",
        "invoice_to_state", "invoice_to_country", "invoice_to_vat_id",
        "invoice_to_beneficiary", "custom_field", "internal_reference", redacted=True,
    )
    for column in ("invoice_to_transmission_info", "transmission_info", "plugin_data"):
        review["invoice_metadata"] += sum(
            _populated(value) for (value,) in _records(
                invoices.filter(**{f"{column}__isnull": False}).exclude(**{column: {}}), column
            )
        )
    review["invoice_line_attendees"] = _text(
        core.InvoiceLine.objects.filter(invoice__event=event), "attendee_name", redacted=True
    )

    tickets = core.CachedTicket.objects.filter(order_position__order__event=event)
    combined = core.CachedCombinedTicket.objects.filter(order__event=event)
    review["generated_tickets"] = tickets.filter(_nonempty("file")).count() + combined.filter(
        _nonempty("file")
    ).count()

    mail = core.OutgoingMail.objects.filter(
        Q(event=event) | Q(order__event=event) | Q(orderposition__order__event=event)
    ).exclude(pk__in=excluded_mail_ids)
    # Event-only mail has no reliable participant association.
    linked = Q(order__isnull=False) | Q(orderposition__isnull=False) | Q(customer__isnull=False)
    identifiers["outgoing_customer_email"] = mail.filter(linked).exclude(
        to=[], cc=[], bcc=[]
    ).count()
    review["unclassified_event_email"] = mail.filter(~linked, user__isnull=True).count()
    review["certificate_attachments"] = mail.filter(
        should_attach_cached_files__filename="certificate_of_attendance.pdf",
        **{"should_attach_cached_files__file__isnull": False},
    ).exclude(should_attach_cached_files__file="").values(
        "should_attach_cached_files"
    ).order_by().distinct().count()

    for query, name in (
        (orders, "order_metadata"), (positions, "attendee_metadata"),
    ):
        _audit_payloads(query, "meta_info", name, identifiers, review)
    for model, name in ((core.OrderPayment, "payment_metadata"), (core.OrderRefund, "refund_metadata")):
        _audit_payloads(model.objects.filter(order__event=event), "info", name, identifiers, review)

    review["order_comments"] = _text(orders, "comment")
    review["waiting_list_voucher_notes"] = _text(
        core.Voucher.objects.filter(event=event, waitinglistentries__isnull=False).distinct(),
        "comment", redacted=True,
    )
    _audit_logs(event, identifiers, review, notices)
    known_external = {
        "pretix_attendance_certificate_neo", "pretix_passbook",
        "pretix_sofort", "pretix_fontpackfree", "pretix_dataprotection_monitor",
    }
    covered_builtin = {
        "autocheckin", "badges", "banktransfer", "checkinlists", "manualpayment",
        "paypal", "paypal2", "pretixdroid", "reports", "returnurl", "sendmail",
        "statistics", "stripe", "ticketoutputpdf", "webcheckin",
    }
    configured_plugins = set(event.get_plugins()) | set(event.organizer.get_plugins())
    review["unclassified_event_plugins"] = len(configured_plugins - {
        "", *known_external, *(f"pretix.plugins.{name}" for name in covered_builtin),
    })
    notices["test_orders"] = orders.filter(testmode=True).count()
    notices["non_test_orders"] = orders.filter(testmode=False).count()
    notices["test_mode"] = int(event.testmode)
    notices["shop_online"] = int(event.live)
    return dict(+identifiers), dict(+review), dict(+notices)


def audit_event(event_id, *, now=None, grace_days=30, deferred_until=None, excluded_mail_ids=()):
    now = timezone.now() if now is None else now
    if timezone.is_naive(now) or grace_days < 0 or (
        deferred_until is not None and timezone.is_naive(deferred_until)
    ):
        raise ValueError("Use aware timestamps and a nonnegative grace period.")
    try:
        with scopes_disabled():
            event = core.Event.objects.get(pk=event_id)
        with scope(organizer=event.organizer_id):
            schedule = event_schedule(event, now=now, grace_days=grace_days)
            result = {"event_id": event_id, "checked_at": now, "schedule": schedule}
            if deferred_until is not None and now < deferred_until:
                return AuditResult(**result, status=Status.DEFERRED)
            if schedule.due_at is not None and (schedule.due_at > now or schedule.end == now):
                return AuditResult(**result, status=Status.NOT_DUE)
            if schedule.uncertain:
                return AuditResult(**result, status=Status.MANUAL_REVIEW, review={"uncertain_schedule": 1})
            identifiers, review, notices = _audit_current_data(event, excluded_mail_ids)
            status = Status.NO_KNOWN_IDENTIFIERS
            if identifiers:
                status = Status.ACTION_REQUIRED
            elif review or notices.get("shop_online"):
                status = Status.MANUAL_REVIEW
            return AuditResult(**result, status=status, identifiers=identifiers, review=review, notices=notices)
    except ObjectDoesNotExist:
        return AuditResult(event_id, now, Status.SCAN_ERROR, error_code="missing_record")
    except ZoneInfoNotFoundError:
        return AuditResult(event_id, now, Status.SCAN_ERROR, error_code="invalid_event_timezone")
    except RecursionError:
        return AuditResult(event_id, now, Status.SCAN_ERROR, error_code="invalid_payload_depth")
    except DatabaseError:
        return AuditResult(event_id, now, Status.SCAN_ERROR, error_code="database_error")
