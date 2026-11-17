from datetime import datetime, time

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django_scopes import scopes_disabled
from pretix.base.models import Event, Organizer

from .mailing import valid_address
from .models import NotificationSettings


class ExclusionsField(forms.CharField):
    widget = forms.Textarea(attrs={"rows": 2})

    def __init__(self, model, **kwargs):
        self.model = model
        super().__init__(required=False, max_length=4000, **kwargs)

    def prepare_value(self, value):
        return ", ".join(map(str, value)) if isinstance(value, list) else value

    def has_changed(self, initial, data):
        try:
            return sorted(initial or []) != self._parse(data or "")
        except ValidationError:
            return True

    def _parse(self, value):
        entries = value.replace(",", " ").split()
        if any(not entry.isascii() or not entry.isdecimal() or len(entry) > 19 for entry in entries):
            raise ValidationError("Enter numeric IDs separated by spaces or commas.")
        ids = {int(entry) for entry in entries}
        if any(not 0 < pk <= 2**63 - 1 for pk in ids):
            raise ValidationError("Enter positive IDs within the database ID range.")
        return sorted(ids)

    def clean(self, value):
        ids = set(self._parse(super().clean(value)))
        with scopes_disabled():
            existing = set(self.model.objects.filter(pk__in=ids).values_list("pk", flat=True))
        if ids != existing:
            raise ValidationError("One or more IDs do not exist.")
        return sorted(ids)


class MonitorSettingsForm(forms.ModelForm):
    excluded_events = ExclusionsField(Event, label="Excluded event IDs")
    excluded_organizers = ExclusionsField(Organizer, label="Excluded organizer IDs")
    acknowledge = forms.BooleanField(
        required=False, label="I reviewed the completed dry-run audit and approve live reminders",
    )

    class Meta:
        model = NotificationSettings
        fields = (
            "subject", "body", "administrator_email", "enabled", "grace_days",
            "reminder_days", "hourly_limit", "audit_hours", "excluded_events",
            "excluded_organizers",
        )
        labels = {
            "enabled": "Enable live reminders",
            "grace_days": "Days after completion before review",
            "reminder_days": "Days between reminders",
            "hourly_limit": "Maximum new reminders per rolling hour",
            "audit_hours": "Audit interval in hours",
        }
        widgets = {"body": forms.Textarea(attrs={"rows": 10})}

    def __init__(self, *args, approved_run=None, preview_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.approved_run = approved_run
        self.preview_only = preview_only
        self.was_enabled = self.instance.enabled

    def _post_clean(self):
        if not self.preview_only:
            super()._post_clean()

    def clean(self):
        data = super().clean()
        if self.preview_only:
            return data
        changed = {"grace_days", "excluded_events", "excluded_organizers"}.intersection(self.changed_data)
        if changed:
            self.instance.scope_changed_at = timezone.now()
            self.instance.initial_review_at = None
            if data.get("enabled"):
                self.add_error("enabled", "Disable reminders when changing the audit scope, then run a fresh audit.")
        if data.get("enabled") and not self.was_enabled and not changed:
            if self.approved_run is None or not data.get("acknowledge"):
                self.add_error("enabled", "Complete and review a dry run before enabling reminders.")
            else:
                self.instance.initial_review_at = timezone.now()
        if data.get("enabled") and not valid_address(data.get("administrator_email")):
            self.add_error("administrator_email", "Enter a valid fallback email before enabling reminders.")
        return data


class DeferralForm(forms.Form):
    resume_on = forms.DateField(label="Resume on", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event

    def clean_resume_on(self):
        deadline = datetime.combine(self.cleaned_data["resume_on"], time.min, self.event.timezone)
        if deadline <= timezone.now():
            raise ValidationError("Choose a future date in the event’s timezone.")
        return deadline
