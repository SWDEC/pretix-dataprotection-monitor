from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from django.utils import timezone
from pretix.base.models import SubEvent


@dataclass(frozen=True)
class Schedule:
    end: datetime | None
    due_at: datetime | None
    inferred_end: bool = False
    uncertain: bool = False


def event_schedule(event, *, now, grace_days):
    if grace_days < 0 or timezone.is_naive(now):
        raise ValueError("Use a nonnegative grace period and an aware clock.")
    zone = event.timezone
    dates = [(event.date_from, event.date_to)]
    if event.has_subevents:
        dates = SubEvent.objects.filter(event=event).order_by().values_list(
            "date_from", "date_to"
        ).iterator(chunk_size=500)

    latest = None
    inferred = uncertain = False
    # A future start prevents completion even if that row's end is invalid.
    latest_start = None
    for start, end in dates:
        if start and timezone.is_aware(start):
            latest_start = max(filter(None, (latest_start, start.astimezone(UTC))))
        if not start or timezone.is_naive(start) or (
            end and (timezone.is_naive(end) or end < start)
        ):
            uncertain = True
            continue
        if not end:
            end = datetime.combine(
                start.astimezone(zone).date() + timedelta(days=1), time.min, zone
            )
            inferred = True
        end = end.astimezone(UTC)
        latest = end if latest is None else max(latest, end)

    if latest_start and latest_start > now and (latest is None or latest < latest_start):
        return Schedule(latest, latest_start, inferred, True)
    if latest is None:
        return Schedule(None, None, inferred, True)
    due = (latest.astimezone(zone) + timedelta(days=grace_days)).astimezone(UTC)
    if uncertain and latest <= now:
        return Schedule(latest, None, inferred, True)
    return Schedule(latest, due, inferred, uncertain)
