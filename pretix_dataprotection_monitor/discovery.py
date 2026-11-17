from django_scopes import scopes_disabled
from pretix.base.models import Event, Organizer


def organizer_ids(*, after=0, limit=200, excluded=()):
    if not 1 <= limit <= 1000:
        raise ValueError("Page size must be between 1 and 1000.")
    with scopes_disabled():
        return list(Organizer.objects.filter(pk__gt=after).exclude(
            pk__in=excluded
        ).order_by("pk").values_list("pk", flat=True)[:limit])


def event_ids(*, after=0, limit=200, excluded_organizers=(), excluded_events=()):
    if not 1 <= limit <= 1000:
        raise ValueError("Page size must be between 1 and 1000.")
    with scopes_disabled():
        return list(Event.objects.filter(pk__gt=after).exclude(
            organizer_id__in=excluded_organizers
        ).exclude(pk__in=excluded_events).order_by("pk").values_list(
            "pk", flat=True
        )[:limit])
