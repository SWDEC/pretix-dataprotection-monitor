from django.db import transaction
from django.dispatch import receiver
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from pretix.base.models import OutgoingMail
from pretix.base.services.mail import WithholdMailException
from pretix.base.signals import global_email_filter, periodic_task
from pretix.control.signals import nav_global, nav_organizer

from .mailing import MONITOR_HEADER, valid_address
from .models import DeliveryAttempt, EventAuditState, NotificationSettings, Reminder
from .organizer import organizer_summary
from .templates_mail import linked_html
from .labels import ORGANIZER_PERMISSION

DELIVERY_UNCONFIRMED = "dp_monitor_delivery_unconfirmed"
DELIVERY_DISABLED = "dp_monitor_delivery_disabled"
DELIVERY_INELIGIBLE = "dp_monitor_delivery_ineligible"


def _claim_mail(mail_id):
    with transaction.atomic():
        outgoing = OutgoingMail.objects.select_for_update().get(pk=mail_id)
        config = NotificationSettings.objects.filter(pk=1).first()
        if (
            not config or not config.enabled or not config.initial_review_at
            or config.initial_review_at > timezone.now()
            or not valid_address(config.administrator_email)
        ):
            return DELIVERY_DISABLED

        target = Reminder.objects.filter(outgoing_mail_id=mail_id).values(
            "event_id", "event__organizer_id",
        ).first()
        if target and (
            target["event_id"] in config.excluded_events
            or target["event__organizer_id"] in config.excluded_organizers
            or EventAuditState.objects.filter(
                event_id=target["event_id"], deferred_until__gt=timezone.now(),
            ).exists()
        ):
            return DELIVERY_INELIGIBLE

        claim = DeliveryAttempt.objects.filter(outgoing_mail_id=mail_id).first()
        if claim is None:
            DeliveryAttempt.objects.create(
                outgoing_mail_id=mail_id, preceding_failure_at=outgoing.sent,
            )
            return None
        # An explicit temporary SMTP rejection allows native retry. A worker
        # crash or an uncertain disconnect provides no such evidence of failure.
        error = outgoing.error or ""
        if (
            outgoing.sent is None or outgoing.sent == claim.preceding_failure_at
            or not error.startswith("SMTP code 4")
        ):
            return DELIVERY_UNCONFIRMED
        claim.preceding_failure_at = outgoing.sent
        claim.save(update_fields=["preceding_failure_at"])
        return None


@receiver(global_email_filter, dispatch_uid="pretix_dataprotection_monitor.send_guard")
def guard_reminder_delivery(sender, *, message, outgoing_mail=None, **kwargs):
    if outgoing_mail is None or outgoing_mail.event_id is not None or outgoing_mail.organizer_id is not None:
        return message
    kind = outgoing_mail.headers.get(MONITOR_HEADER)
    if kind == "reminder":
        code = _claim_mail(outgoing_mail.pk)
        if code:
            raise WithholdMailException(code, "Administrative review required.")
    if kind in {"reminder", "test"}:
        message.attach_alternative(linked_html(message.body), "text/html")
    return message


@receiver(nav_global, dispatch_uid="pretix_dataprotection_monitor.navigation")
def navigation(sender, request, **kwargs):
    if not request.user.has_active_staff_session(request.session.session_key):
        return []
    return [{
        "label": _("Data Protection Monitor"),
        "url": reverse("plugins:pretix_dataprotection_monitor:dashboard"),
        "icon": "shield",
        "parent": reverse("control:global.settings"),
        "active": request.resolver_match.view_name.startswith("plugins:pretix_dataprotection_monitor:"),
    }]


@receiver(periodic_task, dispatch_uid="pretix_dataprotection_monitor.scheduler")
def schedule_monitor(sender, **kwargs):
    from .scheduler import periodic_tick

    periodic_tick()


@receiver(nav_organizer, dispatch_uid="pretix_dataprotection_monitor.organizer_navigation")
def organizer_navigation(sender, request, organizer, **kwargs):
    if ORGANIZER_PERMISSION not in request.orgapermset:
        return []
    rating = organizer_summary(request).rating
    label = _("Data Protection Monitor")
    if rating == "poor":
        label = format_html(
            '<link rel="stylesheet" href="{}"><span class="dp-monitor-attention">{}</span>'
            '<span class="sr-only"> · {}</span>',
            static("pretix_dataprotection_monitor/score.css"), label, _("Priority review"),
        )
    return [{
        "label": label,
        "url": reverse("plugins:pretix_dataprotection_monitor:organizer", kwargs={"organizer": organizer.slug}),
        "icon": "shield",
        "active": request.resolver_match.view_name == "plugins:pretix_dataprotection_monitor:organizer",
    }]
