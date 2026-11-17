from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pretix.base.models.auth import StaffSession, User

from pretix_dataprotection_monitor.audit import Status
from pretix_dataprotection_monitor.models import AuditRun, EventAuditState, NotificationSettings, Reminder
from pretix_dataprotection_monitor.runs import enqueue_audit, run_audit

from .test_audit import event, order  # noqa: F401

pytestmark = pytest.mark.django_db
PREFIX = "plugins:pretix_dataprotection_monitor:"


@pytest.fixture(autouse=True)
def monitor_static_manifest(monkeypatch):
    from django.contrib.staticfiles import finders
    from django.contrib.staticfiles.storage import staticfiles_storage

    asset = "pretix_dataprotection_monitor/score.css"
    assert finders.find(asset)
    # The test runtime ships pretix's manifest. The monitor is collected
    # separately during the custom production image build.
    monkeypatch.setitem(staticfiles_storage.hashed_files, asset, asset)


def url(name, event_id=None):
    return reverse(PREFIX + name, kwargs={"event_id": event_id} if event_id else None)


@pytest.fixture
def admin(client):
    user = User.objects.create_user("admin@example.invalid", password="fixture password", is_staff=True)
    client.force_login(user)
    StaffSession.objects.create(user=user, session_key=client.session.session_key, comment="Fixture audit")
    return client


def settings_data(**overrides):
    config = NotificationSettings()
    return {
        "subject": config.subject, "body": config.body,
        "administrator_email": "admin@example.invalid", "grace_days": 30,
        "reminder_days": 14, "hourly_limit": 50, "audit_hours": 24,
        "excluded_events": "", "excluded_organizers": "", "action": "save",
        **overrides,
    }


def test_all_routes_require_active_staff_session(client, event):
    urls = [url("dashboard"), url("settings"), url("run"), url("event", event.pk)]
    for path in urls:
        assert client.get(path).status_code in {302, 403}
        assert client.post(path).status_code in {302, 403}
    user = User.objects.create_user("organizer@example.invalid", password="fixture password")
    client.force_login(user)
    for path in urls:
        assert client.get(path).status_code == 403
        assert client.post(path).status_code == 403
    user.is_staff = True
    user.save()
    for path in urls:
        response = client.get(path)
        assert response.status_code == 302
        assert reverse("control:user.sudo") in response.url


def test_post_requires_csrf(admin, event):
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = admin.cookies.copy()
    assert strict.post(url("run")).status_code == 403
    assert strict.post(url("event", event.pk), {"action": "send"}).status_code == 403
    assert strict.post(url("settings"), settings_data(action="test")).status_code == 403
    assert not AuditRun.objects.exists()
    assert not Reminder.objects.exists()


def test_run_dashboard_and_event_details(admin, event, order, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assert admin.post(url("run")).status_code == 302
        assert admin.post(url("run")).status_code == 302
    run = AuditRun.objects.get()
    assert run.state == AuditRun.State.COMPLETED
    assert run.events_checked == run.organizers_checked == 1
    assert not Reminder.objects.exists()
    state = EventAuditState.objects.get()
    assert state.status == Status.ACTION_REQUIRED
    assert state.identifiers == {"customer_emails": 1}
    dashboard = admin.get(url("dashboard"), {"status": "action_required", "q": "fixture"})
    assert "Audit fixture" in dashboard.content.decode()
    assert "Audit fixture" not in admin.get(url("dashboard"), {"status": "manual_review"}).content.decode()
    detail = admin.get(url("event", event.pk))
    assert "Customer emails" in detail.content.decode()
    assert "customer@example.invalid" not in detail.content.decode()
    assert reverse("control:event.shredder.start", kwargs={"organizer": "fixture", "event": "fixture"}) in detail.content.decode()
    run_audit(run.pk)
    run.refresh_from_db()
    assert run.events_checked == 1


def test_enable_requires_audit_and_explicit_approval(admin, django_capture_on_commit_callbacks):
    assert admin.post(url("settings"), settings_data(enabled="on", acknowledge="on")).status_code == 200
    assert not NotificationSettings.objects.get().enabled
    with django_capture_on_commit_callbacks(execute=True):
        admin.post(url("run"))
    assert admin.post(url("settings"), settings_data(enabled="on")).status_code == 200
    assert admin.post(url("settings"), settings_data(enabled="on", acknowledge="on")).status_code == 302
    config = NotificationSettings.objects.get()
    assert config.enabled and config.initial_review_at and config.hourly_limit == 50
    assert admin.post(url("settings"), settings_data(enabled="on", hourly_limit=75)).status_code == 302
    assert NotificationSettings.objects.get().hourly_limit == 75
    assert admin.post(url("settings"), settings_data(enabled="on", grace_days=35)).status_code == 200
    assert NotificationSettings.objects.get().grace_days == 30


def test_preview_and_test_use_draft_without_saving(admin):
    data = settings_data(subject="Draft {event_name}", body="<script>example</script> {organizer_name}")
    preview = admin.post(url("settings"), {**data, "action": "preview"})
    contents = preview.content.decode()
    assert "Draft Sample Event" in contents
    assert "&lt;script&gt;example&lt;/script&gt;" in contents
    assert "<script>example</script>" not in contents
    assert not NotificationSettings.objects.exists()
    response = admin.post(url("settings"), {**data, "action": "test"})
    assert response.status_code == 200
    assert mail.outbox[0].to == ["admin@example.invalid"]
    assert mail.outbox[0].subject == "Draft Sample Event"
    assert not NotificationSettings.objects.exists()


def test_deferral_and_exclusions_block_manual_send(admin, event, order):
    NotificationSettings.objects.create(
        enabled=True, administrator_email="admin@example.invalid", initial_review_at=timezone.now()
    )
    tomorrow = (timezone.now().astimezone(event.timezone).date() + timedelta(days=1)).isoformat()
    response = admin.post(url("event", event.pk), {"action": "defer", "resume_on": tomorrow, "reason": "Retention review"})
    assert response.status_code == 302
    state = EventAuditState.objects.get(event=event)
    assert state.deferral_reason == "Retention review"
    assert state.deferred_until > timezone.now()
    assert admin.post(url("event", event.pk), {"action": "send"}).status_code == 302
    assert not Reminder.objects.exists()
    assert admin.post(url("event", event.pk), {"action": "resume"}).status_code == 302
    state.refresh_from_db()
    assert state.deferred_until is None
    assert admin.post(url("settings"), settings_data(excluded_events=str(event.pk))).status_code == 302
    config = NotificationSettings.objects.get()
    assert config.excluded_events == [event.pk]
    assert config.initial_review_at is None
    assert config.scope_changed_at is not None
    assert not config.enabled


def test_manual_reminder_history(admin, event, order):
    NotificationSettings.objects.create(
        enabled=True, administrator_email="admin@example.invalid", initial_review_at=timezone.now()
    )
    assert admin.post(url("event", event.pk), {"action": "preview"}).status_code == 200
    assert admin.post(url("event", event.pk), {"action": "send"}).status_code == 302
    assert admin.post(url("event", event.pk), {"action": "refresh_delivery"}).status_code == 302
    row = Reminder.objects.get(event=event)
    assert row.state == Reminder.State.SENT
    history = admin.get(url("event", event.pk)).content.decode()
    assert "Sent" in history
    assert "customer@example.invalid" not in history
    assert "administrator@example.invalid" not in history


def test_worker_respects_exclusions(event, django_capture_on_commit_callbacks):
    NotificationSettings.objects.create(excluded_events=[event.pk])
    with django_capture_on_commit_callbacks(execute=True):
        run, _ = enqueue_audit()
    run.refresh_from_db()
    assert run.events_checked == 0
    assert run.organizers_checked == 1
    assert not EventAuditState.objects.exists()


def test_deferral_applied_after_queue_withholds_delivery(admin, event, order):
    from unittest.mock import patch

    from pretix.base.services.mail import mail_send_task

    NotificationSettings.objects.create(
        enabled=True, administrator_email="admin@example.invalid", initial_review_at=timezone.now()
    )
    with patch("pretix.base.services.mail.chain"):
        assert admin.post(url("event", event.pk), {"action": "send"}).status_code == 302
    row = Reminder.objects.get(event=event)
    tomorrow = (timezone.now().astimezone(event.timezone).date() + timedelta(days=1)).isoformat()
    data = {"action": "defer", "resume_on": tomorrow, "reason": "Documented exception"}
    assert admin.post(url("event", event.pk), data).status_code == 302
    assert mail_send_task.apply(kwargs={"outgoing_mail": row.outgoing_mail_id}).get() is False
    assert admin.post(url("event", event.pk), {"action": "refresh_delivery"}).status_code == 302
    row.refresh_from_db()
    assert row.error_code == "dp_monitor_delivery_ineligible"
    assert row.sent_at is None


def test_rendered_dashboard_links_and_audit_button(admin, event, django_capture_on_commit_callbacks):
    from bs4 import BeautifulSoup

    response = admin.get(url("dashboard"))
    page = BeautifulSoup(response.content, "html.parser")
    settings_link = page.select_one(f'a[href="{url("settings")}"]')
    assert settings_link and admin.get(settings_link["href"]).status_code == 200
    global_link = page.select_one(f'a[href="{reverse("control:global.settings")}"]')
    assert global_link and admin.get(global_link["href"]).status_code == 200

    run_form = page.select_one(f'form[action="{url("run")}"]')
    assert run_form and run_form["method"] == "post"
    assert run_form.select_one('button[type="submit"]').get_text(strip=True) == "Run audit now"
    with django_capture_on_commit_callbacks(execute=True):
        result = admin.post(run_form["action"], follow=True)
    assert result.status_code == 200
    assert AuditRun.objects.get().state == AuditRun.State.COMPLETED


def test_german_monitor_labels_follow_admin_language(admin, event):
    user = User.objects.get(email="admin@example.invalid")
    user.locale = "de"
    user.save(update_fields=["locale"])
    EventAuditState.objects.create(
        event=event, status=Status.ACTION_REQUIRED,
        identifiers={"attendee_names": 1}, checked_at=timezone.now(),
    )
    dashboard = admin.get(url("dashboard"), HTTP_ACCEPT_LANGUAGE="de").content.decode()
    assert "Datenschutzmonitor" in dashboard
    assert "Prüfung jetzt starten" in dashboard
    assert "Handlungsbedarf" in dashboard
    settings = admin.get(url("settings"), HTTP_ACCEPT_LANGUAGE="de").content.decode()
    assert "Datenschutzeinstellungen" in settings
    assert "E-Mail-Betreff" in settings
    details = admin.get(url("event", event.pk), HTTP_ACCEPT_LANGUAGE="de").content.decode()
    assert "Namen von Teilnehmenden" in details
    assert "Prüfung zurückstellen" in details
    assert "Audit fixture" in details


def test_organizer_score_and_shredder_links_are_permission_scoped(client, event):
    from django_scopes import scopes_disabled
    from pretix.base.models import Event, Organizer, Team

    with scopes_disabled():
        second = Event.objects.create(
            organizer=event.organizer, slug="second", name="Clean accessible event",
            date_from=event.date_from, date_to=event.date_to,
        )
        hidden = Event.objects.create(
            organizer=event.organizer, slug="hidden", name="Hidden event",
            date_from=event.date_from, date_to=event.date_to,
        )
        other_org = Organizer.objects.create(name="Other organizer", slug="other-organizer")
        other_event = Event.objects.create(
            organizer=other_org, slug="other-event", name="Other organizer event",
            date_from=event.date_from, date_to=event.date_to,
        )
        team = Team.objects.create(
            organizer=event.organizer, name="Organizer leads",
            limit_organizer_permissions={"organizer.settings.general:write": True},
            limit_event_permissions={"event.orders:write": True},
        )
        user = User.objects.create_user("lead@example.invalid", password="fixture password")
        team.members.add(user)
        team.limit_events.add(event, second)
    for fixture, status in (
        (event, Status.ACTION_REQUIRED), (second, Status.NO_KNOWN_IDENTIFIERS),
        (hidden, Status.ACTION_REQUIRED), (other_event, Status.ACTION_REQUIRED),
    ):
        EventAuditState.objects.create(event=fixture, status=status, checked_at=timezone.now())
    client.force_login(user)
    path = reverse(PREFIX + "organizer", kwargs={"organizer": event.organizer.slug})
    organizer_home = client.get(reverse("control:organizer", kwargs={"organizer": event.organizer.slug}))
    assert path in organizer_home.content.decode()
    response = client.get(path)
    assert response.status_code == 200
    content = response.content.decode()
    assert "5 / 10" in content
    assert "Hidden event" not in content
    assert "Other organizer event" not in content
    shredder = reverse("control:event.shredder.start", kwargs={"organizer": event.organizer.slug, "event": event.slug})
    assert shredder in content
    assert client.get(reverse(PREFIX + "organizer", kwargs={"organizer": other_org.slug})).status_code == 404
    assert client.get(url("dashboard")).status_code == 403
    team.limit_event_permissions = {"event.orders:read": True}
    team.save(update_fields=["limit_event_permissions"])
    content = client.get(path).content.decode()
    assert "5 / 10" in content
    assert shredder not in content
    assert "Ask an event administrator" in content


def test_settings_places_preview_and_controls_beside_body(admin):
    response = admin.post(url("settings"), settings_data(
        action="preview", body="Read [the guide](https://example.org/guide)",
    ))
    content = response.content.decode()
    markers = ['id="id_body"', "Available placeholders:", "Preview email", "Send test email",
               "Email preview", 'id="id_administrator_email"']
    positions = [content.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "Reminders go here if an event has no valid contact email." in content
    assert '<a href="https://example.org/guide">the guide</a>' in content


@pytest.mark.parametrize(("status", "score", "rating", "pulse"), [
    (Status.NO_KNOWN_IDENTIFIERS, 10, "good", False),
    (Status.MANUAL_REVIEW, 5, "fair", False),
    (Status.ACTION_REQUIRED, 0, "poor", True),
    (Status.NOT_DUE, None, "unrated", False),
])
def test_organizer_rating_color_and_sidebar_pulse(admin, event, status, score, rating, pulse):
    EventAuditState.objects.create(event=event, status=status, checked_at=timezone.now())
    organizer = {"organizer": event.organizer.slug}
    response = admin.get(reverse(PREFIX + "organizer", kwargs=organizer))
    assert response.status_code == 200
    assert response.context["score"] == score
    assert response.context["rating"] == rating
    content = response.content.decode()
    if score is not None:
        assert f"dp-monitor-score-{rating}" in content
    else:
        assert "A score will appear" in content
    home = admin.get(reverse("control:organizer", kwargs=organizer)).content.decode()
    assert ('class="dp-monitor-attention"' in home) is pulse
    if pulse:
        assert "pretix_dataprotection_monitor/score.css" in home
