from collections import Counter

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, DateTimeField, OuterRef, Q, Subquery, Value, When
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy
from django.views import View
from django_scopes import scopes_disabled
from pretix.base.models import Event
from pretix.control.permissions import AdministratorPermissionRequiredMixin

from .audit import Status, audit_event
from .forms import DeferralForm, MonitorSettingsForm
from .labels import DELIVERY_LABELS, ISSUE_LABELS, RUN_LABELS, STATUS_LABELS
from .mailing import preview_email, send_test_email
from .models import AuditRun, EventAuditState, NotificationSettings, Reminder
from .notifications import pending_reminder_ids, reconcile_reminder, request_reminder
from .runs import enqueue_audit
from .templates_mail import PLACEHOLDERS

NAMESPACE = "plugins:pretix_dataprotection_monitor:"
TEMPLATES = "pretix_dataprotection_monitor/"



def _config():
    return NotificationSettings.objects.filter(pk=1).first() or NotificationSettings()


def _event(event_id):
    with scopes_disabled():
        return get_object_or_404(Event.objects.select_related("organizer"), pk=event_id)


def _excluded(event, config):
    return event.pk in config.excluded_events or event.organizer_id in config.excluded_organizers


def _successful_runs(config):
    runs = AuditRun.objects.filter(state=AuditRun.State.COMPLETED, scan_errors=0)
    if config.scope_changed_at:
        runs = runs.filter(created_at__gte=config.scope_changed_at)
    return runs.order_by("-finished_at")


def _display_status(state):
    if state and state.deferred_until and state.deferred_until > timezone.now():
        return STATUS_LABELS[Status.DEFERRED]
    return STATUS_LABELS[state.status if state else ""]


class AdminView(AdministratorPermissionRequiredMixin, View):
    def show(self, request, template, **context):
        return render(request, TEMPLATES + template, context)


class Dashboard(AdminView):
    def get(self, request):
        config = _config()
        sent = Reminder.objects.filter(event_id=OuterRef("event_id"), sent_at__isnull=False).order_by("-sent_at")
        states = EventAuditState.objects.exclude(event_id__in=config.excluded_events).exclude(
            event__organizer_id__in=config.excluded_organizers,
        ).annotate(display_status=Case(
            When(deferred_until__gt=timezone.now(), then=Value(Status.DEFERRED.value)),
            default="status",
        ))
        counts = Counter(dict(states.values("display_status").annotate(n=Count("pk")).values_list(
            "display_status", "n",
        )))
        selected = request.GET.get("status", "")
        query = request.GET.get("q", "").strip()[:200]
        if selected and selected in STATUS_LABELS:
            states = states.filter(display_status=selected)
        if query:
            states = states.filter(Q(event__slug__icontains=query) | Q(
                event__name__icontains=query
            ) | Q(event__organizer__name__icontains=query))
        states = states.select_related("event__organizer").annotate(
            last_reminder=Subquery(sent.values("sent_at")[:1], output_field=DateTimeField())
        ).order_by("-event_end", "event_id")
        page = Paginator(states, 50).get_page(request.GET.get("page"))
        for state in page:
            state.status_label = STATUS_LABELS[state.display_status]
        latest = AuditRun.objects.order_by("-created_at").first()
        return self.show(
            request, "dashboard.html", config=config, page=page, latest=latest,
            successful=_successful_runs(config).first(), run_status=RUN_LABELS.get(latest.state) if latest else None,
            selected=selected, query=query,
            status_choices=[(key, label) for key, label in STATUS_LABELS.items() if key],
            checked=sum(counts.values()) - counts[""], clean=counts[Status.NO_KNOWN_IDENTIFIERS],
            action=counts[Status.ACTION_REQUIRED], manual=counts[Status.MANUAL_REVIEW],
            scan_errors=counts[Status.SCAN_ERROR],
            mail_errors=Reminder.objects.filter(state__in=[
                Reminder.State.BLOCKED, Reminder.State.FAILED, Reminder.State.NEEDS_REVIEW,
            ]).count(),
            last_mail=Reminder.objects.filter(sent_at__isnull=False).order_by("-sent_at").values_list(
                "sent_at", flat=True,
            ).first(),
        )


class RunAudit(AdminView):
    def post(self, request):
        _, created = enqueue_audit()
        messages.info(request, gettext_lazy("Audit queued. Refresh this page to see progress.") if created else gettext_lazy("An audit is already active."))
        return redirect(NAMESPACE + "dashboard")


class SettingsView(AdminView):
    def _show(self, request, form, config, preview=None):
        return self.show(
            request, "settings.html", form=form, preview=preview,
            placeholders=sorted(PLACEHOLDERS), approved_run=_successful_runs(config).first(),
        )

    def get(self, request):
        config = _config()
        return self._show(request, MonitorSettingsForm(instance=config), config)

    def post(self, request):
        action = request.POST.get("action")
        if action not in {"save", "preview", "test"}:
            return HttpResponseBadRequest(gettext_lazy("Unknown settings action."))
        if action != "save":
            config = _config()
            # Preview and test use the submitted draft subject, body and fallback only.
            draft = NotificationSettings(
                subject=request.POST.get("subject", ""), body=request.POST.get("body", ""),
                administrator_email=request.POST.get("administrator_email", ""),
            )
            form = MonitorSettingsForm(request.POST, instance=config, preview_only=True)
            try:
                preview = preview_email(draft)
                if action == "test":
                    send_test_email(draft)
                    messages.success(request, gettext_lazy("Synthetic test email submitted to the mail service."))
            except ValidationError as error:
                form.add_error(None, error)
                preview = None
            return self._show(request, form, config, preview)

        with transaction.atomic():
            config, _ = NotificationSettings.objects.select_for_update().get_or_create(pk=1)
            form = MonitorSettingsForm(
                request.POST, instance=config, approved_run=_successful_runs(config).first(),
            )
            if form.is_valid():
                form.save()
                messages.success(request, gettext_lazy("Settings saved."))
                return redirect(NAMESPACE + "settings")
        return self._show(request, form, config)


class EventDetail(AdminView):
    def _show(self, request, event, config, *, form=None, preview=None):
        state = EventAuditState.objects.filter(event=event).first()
        route = "control:event.shredder.start"
        findings = [
            (ISSUE_LABELS.get(key, key.replace("_", " ").capitalize()), count, assessment)
            for categories, assessment in (
                (state.identifiers if state else {}, gettext_lazy("Identifier found")),
                (state.review if state else {}, gettext_lazy("Manual review")),
            )
            for key, count in sorted(categories.items())
        ]
        history = Paginator(Reminder.objects.filter(event=event).order_by("-occurrence"), 20).get_page(
            request.GET.get("page")
        )
        for reminder in history:
            reminder.status_label = DELIVERY_LABELS.get(reminder.state, reminder.state)
            reminder.recipient_label = {"contact": gettext_lazy("Event contact"), "fallback": gettext_lazy("Administrator fallback")}.get(
                reminder.recipient_source, "",
            )
        return self.show(
            request, "event.html", event=event, state=state, status_label=_display_status(state),
            history=history,
            shredder_url=reverse(route, kwargs={"organizer": event.organizer.slug, "event": event.slug}),
            form=form if form is not None else DeferralForm(event=event),
            excluded=_excluded(event, config), preview=preview, findings=findings, enabled=config.enabled,
        )

    def get(self, request, event_id):
        return self._show(request, _event(event_id), _config())

    def post(self, request, event_id):
        event, config = _event(event_id), _config()
        action = request.POST.get("action")
        if action == "preview":
            result = audit_event(event_id, grace_days=config.grace_days)
            with scopes_disabled():
                preview = preview_email(config, event=event, result=result)
            return self._show(request, event, config, preview=preview)
        if action == "send":
            result = request_reminder(event_id)
            message = {
                "queued": gettext_lazy("Reminder submitted to the mail service."),
                "disabled": gettext_lazy("Live reminders are disabled."),
                "ineligible": gettext_lazy("This event is currently ineligible for a reminder."),
                "test_data": gettext_lazy("This event contains test data and requires review."),
                "pending": gettext_lazy("A reminder is already in progress."),
                "cooldown": gettext_lazy("This event is still within the reminder interval."),
                "throttled": gettext_lazy("The hourly reminder limit has been reached."),
                "blocked": gettext_lazy("The reminder requires administrative attention."),
                "invalid_configuration": gettext_lazy("Review the notification settings before sending."),
            }[result.outcome]
            messages.info(request, message)
        elif action == "refresh_delivery":
            ids = Reminder.objects.filter(event=event, pk__in=pending_reminder_ids(event_id=event_id, limit=100)).values_list(
                "pk", flat=True,
            )
            for reminder_id in ids:
                reconcile_reminder(reminder_id)
            messages.info(request, gettext_lazy("Reminder delivery status refreshed."))
        elif action in {"defer", "resume"}:
            form = DeferralForm(request.POST, event=event)
            if action == "defer" and not form.is_valid():
                return self._show(request, event, config, form=form)
            with transaction.atomic():
                state, _ = EventAuditState.objects.select_for_update().get_or_create(event=event)
                state.deferred_until = form.cleaned_data["resume_on"] if action == "defer" else None
                state.deferral_reason = form.cleaned_data["reason"] if action == "defer" else ""
                state.save(update_fields=["deferred_until", "deferral_reason"])
            messages.success(request, gettext_lazy("Review deferred.") if action == "defer" else gettext_lazy("Monitoring resumed."))
        else:
            return HttpResponseBadRequest(gettext_lazy("Unknown event action."))
        return redirect(NAMESPACE + "event", event_id=event_id)
