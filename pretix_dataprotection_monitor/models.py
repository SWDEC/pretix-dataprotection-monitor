from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q

from .audit import Status, audit_event
from .templates_mail import DEFAULT_BODY, DEFAULT_SUBJECT, validate_template


class NotificationSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    administrator_email = models.EmailField(blank=True)
    initial_review_at = models.DateTimeField(null=True, blank=True)
    subject = models.CharField(max_length=300, default=DEFAULT_SUBJECT)
    body = models.TextField(default=DEFAULT_BODY)
    reminder_days = models.PositiveIntegerField(default=14, validators=[MinValueValidator(1)])
    grace_days = models.PositiveIntegerField(default=30)
    audit_hours = models.PositiveIntegerField(default=24, validators=[MinValueValidator(1)])
    excluded_organizers = models.JSONField(default=list, blank=True)
    excluded_events = models.JSONField(default=list, blank=True)
    scope_changed_at = models.DateTimeField(null=True, blank=True)
    hourly_limit = models.PositiveIntegerField(default=10, validators=[MinValueValidator(1)])

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(pk=1), name="dp_monitor_single_settings"),
            models.CheckConstraint(
                condition=Q(enabled=False) | (
                    Q(initial_review_at__isnull=False) & ~Q(administrator_email="")
                ),
                name="dp_monitor_review_before_send",
            ),
            models.CheckConstraint(
                condition=Q(reminder_days__gte=1, hourly_limit__gte=1),
                name="dp_monitor_positive_mail_limits",
            ),
        ]

    def clean(self):
        super().clean()
        validate_template(self.subject, subject=True)
        validate_template(self.body)
        if self.enabled and (not self.administrator_email or not self.initial_review_at):
            raise ValidationError(
                "Sending requires a fallback address and acknowledgment of the initial audit."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Reminder(models.Model):
    class State(models.TextChoices):
        BLOCKED = "blocked"
        QUEUED = "queued"
        RETRYING = "retrying"
        SENT = "sent"
        FAILED = "failed"
        NEEDS_REVIEW = "needs_review"

    event = models.ForeignKey("pretixbase.Event", on_delete=models.CASCADE)
    occurrence = models.PositiveIntegerField()
    # The native row is short-lived. Keep its ID for reconciliation while
    # retaining the plugin's own state even after pretix removes that row.
    outgoing_mail_id = models.PositiveBigIntegerField(null=True, unique=True)
    state = models.CharField(max_length=20, choices=State.choices)
    queued_at = models.DateTimeField(null=True, db_index=True)
    sent_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=40, blank=True)
    recipient_source = models.CharField(max_length=12, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("event", "occurrence"), name="dp_monitor_reminder_occurrence"),
        ]
        indexes = [models.Index(fields=("state", "id"), name="dp_monitor_mail_state")]


class DeliveryAttempt(models.Model):
    outgoing_mail = models.OneToOneField(
        "pretixbase.OutgoingMail", primary_key=True, on_delete=models.CASCADE,
    )
    preceding_failure_at = models.DateTimeField(null=True)

class AuditRun(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    state = models.CharField(max_length=12, choices=State.choices, default=State.QUEUED)
    # The unique non-null value admits only one queued or running full audit.
    active_slot = models.PositiveSmallIntegerField(default=1, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    heartbeat_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    organizers_checked = models.PositiveIntegerField(default=0)
    events_checked = models.PositiveIntegerField(default=0)
    scan_errors = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=40, blank=True)


class EventAuditState(models.Model):
    event = models.OneToOneField("pretixbase.Event", on_delete=models.CASCADE)
    status = models.CharField(
        max_length=24, choices=[(status.value, status.value) for status in Status],
        blank=True, db_index=True,
    )
    checked_at = models.DateTimeField(null=True)
    event_end = models.DateTimeField(null=True)
    due_at = models.DateTimeField(null=True)
    inferred_end = models.BooleanField(default=False)
    identifiers = models.JSONField(default=dict)
    review = models.JSONField(default=dict)
    notices = models.JSONField(default=dict)
    error_code = models.CharField(max_length=40, blank=True)
    deferred_until = models.DateTimeField(null=True, db_index=True)
    deferral_reason = models.CharField(max_length=500, blank=True)

    def run_check(self, **options):
        # Isolate read failures from the transaction that stores the error status.
        with transaction.atomic():
            result = audit_event(self.event_id, **options)
            if result.error_code == "database_error":
                transaction.set_rollback(True)
        if result.status != Status.DEFERRED:
            self._record(result)
        return result

    def _record(self, result):
        self.checked_at = result.checked_at
        self.status = result.status
        self.event_end = result.schedule.end if result.schedule else None
        self.due_at = result.schedule.due_at if result.schedule else None
        self.inferred_end = bool(result.schedule and result.schedule.inferred_end)
        self.identifiers = result.identifiers
        self.review = result.review
        self.notices = result.notices
        self.error_code = result.error_code or ""
        self.save(update_fields=[
            "checked_at", "status", "event_end", "due_at", "inferred_end",
            "identifiers", "review", "notices", "error_code",
        ])


class DeliverySchedule(models.Model):
    id = models.PositiveSmallIntegerField(default=1, primary_key=True, editable=False)
    next_tick_at = models.DateTimeField()
    lease_until = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    reminder_cursor = models.PositiveBigIntegerField(default=0)
    reconcile_cursor = models.PositiveBigIntegerField(default=0)
