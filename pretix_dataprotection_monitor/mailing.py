from urllib.parse import urljoin

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.core.validators import validate_email
from django.urls import reverse
from pretix.base.services.mail import INVALID_ADDRESS, mail
from pretix.helpers.format import FormattedString

from .templates_mail import render_message

MONITOR_HEADER = "X-Pretix-Data-Protection-Monitor"
BODY_TEMPLATE = "pretix_dataprotection_monitor/reminder.txt"

SAMPLE_VALUES = {
    "event_name": "Sample Event",
    "organizer_name": "Sample Organizer",
    "event_end": "15 Aug 2026, 18:00",
    "days_since_event": 35,
    "issues_summary": "Customer emails: 2\nAttendee names: 4",
    "event_url": "https://example.invalid/control/event/sample/example/",
    "due_date": "14 Sep 2026, 18:00",
}


def valid_address(raw):
    if not isinstance(raw, str) or any(c in raw for c in "\r\n\x00"):
        return None
    address = raw.strip()
    try:
        validate_email(address)
    except ValidationError:
        return None
    return address if address != INVALID_ADDRESS else None


def resolve_recipient(event, fallback):
    address = valid_address(event.settings.contact_mail)
    return (address, "contact") if address else (valid_address(fallback), "fallback")


def preview_email(config, *, event=None, result=None):
    if event is None and result is None:
        return render_message(config.subject, config.body, SAMPLE_VALUES)
    if event is None or result is None or result.event_id != event.pk:
        raise ValueError("Provide a matching event and audit result.")
    schedule = result.schedule
    zone = event.timezone
    end = schedule.end.astimezone(zone) if schedule and schedule.end else None
    due = schedule.due_at.astimezone(zone) if schedule and schedule.due_at else None
    path = reverse("control:event.index", kwargs={
        "organizer": event.organizer.slug, "event": event.slug,
    })
    issues = [
        f"{category.replace('_', ' ').capitalize()}: {count}"
        for category, count in sorted(result.identifiers.items())
    ]
    issues.extend(
        f"{category.replace('_', ' ').capitalize()} (manual review): {count}"
        for category, count in sorted(result.review.items())
    )
    if result.notices.get("shop_online"):
        issues.append("The ticket shop is still online.")
    return render_message(config.subject, config.body, {
        "event_name": str(event.name),
        "organizer_name": str(event.organizer.name),
        "event_end": end.isoformat(sep=" ", timespec="minutes") if end else "Unknown",
        "days_since_event": (result.checked_at.astimezone(zone).date() - end.date()).days if end else "Unknown",
        "issues_summary": "\n".join(issues) or "No findings",
        "event_url": urljoin(settings.SITE_URL, path),
        "due_date": due.isoformat(sep=" ", timespec="minutes") if due else "Unknown",
    })


def queue_message(address, message, *, kind):
    # System mail has no event, order or customer linkage. It uses the system
    # transport and avoids event BCC, Reply-To, prefix and signature settings.
    return mail(
        address, FormattedString(message.subject), BODY_TEMPLATE,
        {"rendered_body": message.body},
        plain_text_only=True, no_order_links=True, sensitive=True,
        headers={MONITOR_HEADER: kind},
    )


def send_test_email(config):
    address = valid_address(config.administrator_email)
    if address is None:
        raise ValidationError(_("Enter a valid administrator fallback email before sending a test."))
    return queue_message(address, preview_email(config), kind="test")
