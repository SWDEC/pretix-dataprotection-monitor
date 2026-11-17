from django.urls import path

from .views import Dashboard, EventDetail, RunAudit, SettingsView

PREFIX = "control/global/data-protection/"
urlpatterns = [
    path(PREFIX, Dashboard.as_view(), name="dashboard"),
    path(PREFIX + "settings/", SettingsView.as_view(), name="settings"),
    path(PREFIX + "run/", RunAudit.as_view(), name="run"),
    path(PREFIX + "event/<int:event_id>/", EventDetail.as_view(), name="event"),
]
