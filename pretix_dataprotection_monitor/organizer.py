from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

from django.core.paginator import Paginator
from django.db.models import Case, Count, Q, QuerySet, Value, When
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from pretix.control.permissions import OrganizerPermissionRequiredMixin

from .audit import Status
from .models import EventAuditState, NotificationSettings
from .labels import ORGANIZER_PERMISSION, STATUS_LABELS

RATING = {Status.NO_KNOWN_IDENTIFIERS: 10, Status.MANUAL_REVIEW: 5, Status.ACTION_REQUIRED: 0}


class OrganizerSummary(NamedTuple):
    states: QuerySet
    excluded: bool
    counts: dict[str, int]
    scored: int
    score: Decimal | None
    rating: str


def organizer_summary(request):
    existing = getattr(request, "_dp_monitor_summary", None)
    if existing is not None:
        return existing
    current = request.organizer
    config = NotificationSettings.objects.filter(pk=1).first()
    allowed = request.user.get_events_with_permission(
        ("event.orders:read", "event.orders:write"), request=request,
    ).filter(organizer=current).order_by().values("pk")
    states = EventAuditState.objects.filter(event_id__in=allowed, event__organizer=current)
    excluded = config is not None and current.pk in config.excluded_organizers
    if excluded:
        states = states.none()
    elif config:
        states = states.exclude(event_id__in=config.excluded_events)
    now = timezone.now()
    states = states.annotate(display_status=Case(
        When(deferred_until__gt=now, then=Value(Status.DEFERRED.value)),
        default="status",
    ))
    if config and config.scope_changed_at:
        states = states.filter(Q(checked_at__gte=config.scope_changed_at) | Q(display_status=Status.DEFERRED))
    counts = dict(states.values("display_status").annotate(total=Count("pk")).values_list(
        "display_status", "total",
    ))
    scored = sum(counts.get(status, 0) for status in RATING)
    score = None
    if scored:
        points = sum(counts.get(status, 0) * value for status, value in RATING.items())
        score = (Decimal(points) / scored).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    rating = (
        "unrated" if score is None else "good" if score >= 8 else "fair" if score >= 5 else "poor"
    )
    request._dp_monitor_summary = OrganizerSummary(states, excluded, counts, scored, score, rating)
    return request._dp_monitor_summary


class OrganizerMonitor(OrganizerPermissionRequiredMixin, View):
    permission = ORGANIZER_PERMISSION

    def get(self, request, organizer):
        current = request.organizer
        states, excluded, counts, scored, score, rating = organizer_summary(request)
        issues = [Status.ACTION_REQUIRED, Status.MANUAL_REVIEW, Status.SCAN_ERROR]
        page = Paginator(
            states.filter(display_status__in=issues).select_related("event").order_by("event_end", "event_id"),
            50,
        ).get_page(request.GET.get("page"))
        writable = set(request.user.get_events_with_permission(
            "event.orders:write", request=request,
        ).filter(organizer=current, pk__in=[state.event_id for state in page]).values_list("pk", flat=True))
        for state in page:
            state.status_label = STATUS_LABELS[state.display_status]
            if state.event_id in writable and state.display_status != Status.SCAN_ERROR:
                state.control_url = reverse(
                    "control:event.shredder.start",
                    kwargs={"organizer": current.slug, "event": state.event.slug},
                )
        return render(request, "pretix_dataprotection_monitor/organizer.html", {
            "score": score, "rating": rating, "scored": scored, "excluded": excluded, "page": page,
            "action_count": counts.get(Status.ACTION_REQUIRED, 0),
            "manual_count": counts.get(Status.MANUAL_REVIEW, 0),
            "error_count": counts.get(Status.SCAN_ERROR, 0),
        })
