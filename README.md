# pretix Data Protection Monitor

The monitor implements a read-only audit and a notification service for
pretix 2026.9.1 and 2026.9.2. The audit returns event IDs, timestamps, issue categories
and counts. Notifications are disabled by default. The administrative UI supports manual audits, and pretix’s periodic
scheduler discovers and rechecks events automatically.

## Run an audit

Install the package into a pretix 2026.9.1 or 2026.9.2 environment. Plugin discovery uses
the `pretix.plugin` entry point and does not require enabling the plugin on
each event. These commands emit one aggregate JSON object per event.

```sh
python -m pretix audit_data_protection --event-id 123
python -m pretix audit_data_protection --limit 100 --after 0 --grace-days 30
```

Continue a page by passing the last emitted `event_id` as `--after`. Scans
use the event's effective timezone. When an end is absent, the inferred end
is the next local midnight after the start. The effective end of a series is
the latest subevent end, including inactive dates. A future date prevents
completion. Empty series and unresolved schedules go to manual review.
The grace period is measured in local calendar days and defaults to 30.

Python callers can use `discovery.organizer_ids()`, `discovery.event_ids()`
and `audit.audit_event()`. Discovery accepts bounded page sizes and explicit
exclusions. The audit accepts an aware deferral deadline and trusted monitor
mail IDs from the caller. The administration views persist scope settings and review deferrals. They
apply to manual runs and manual reminder eligibility.

## Results and coverage

- **Not due** means completion or the grace deadline is in the future.
- **No known identifiers** means supported checks found no residual finding.
- **Action required** means at least one supported check found an identifier.
- **Manual review** covers data of uncertain identifiability or retention,
  uncertain timing, or a live ticket shop without a confirmed identifier.
- **Deferred** means the caller supplied an active deferral.
- **Scan error** means the audit could not finish reliably.

The engine checks customer contacts and account links, attendee data
including canceled positions, order and attendee metadata, waiting-list
contacts, answers and uploads, retained cart and checkout data, invoice
records and references, payment and refund metadata, event logs, outgoing
mail and generated document references. Financial findings have separate
categories. Recognized identifiers in payment payloads and opaque residual
provider metadata both require review, with different confidence levels.

Counts are per category and can overlap. The invoice metadata count is a
count of populated metadata fields, one per record and supported field.
Invoice lines and attachment counts have their own units. Shredder start and
completion counts provide historical context. Field checks use current data
and run even after pretix records a completed shredding operation.

File references indicate that records point to files. The engine does not
open PDFs, certificate attachments, ticket files or question uploads.
Attendance Certificate Neo mail attachments are checked while event-linked;
expired links and orphaned files cannot be attributed. Passbook uses pretix's
ticket caches. Sofort's shredder marker does not suppress residual payment
metadata. The Free Font Pack adds no independent participant-data store
covered by this audit.

Payload checks recognize named structured fields and send opaque relevant
payloads to manual review. They do not infer identity from arbitrary free
text, reconstruct identities from arbitrary field combinations, or inspect
external archives, backups or integrations. Unknown enabled plugins are
reported for review. Data in unknown schemas and records without an event
link can remain outside coverage, including data from deactivated plugins.
Findings reflect the rows observed during the scan. Concurrent edits can
change the next result. The engine makes no legal determination.

## Notification service

Apply the plugin's Django migration in an isolated development or staging
environment before calling the notification service. The migration creates
monitor settings, audit, and reminder tables, with delivery disabled. An absent
settings row also disables delivery. A valid administrator fallback and a
recorded acknowledgment of the initial dry-run findings are required before
enabling live reminders through the settings page.

- `mailing.preview_email(config)` uses synthetic event values by default.
  Pass a matching event and audit result for an event preview. HTML previews
  escape both rendered fields.
- `mailing.send_test_email(config)` is an explicit test action. It uses
  synthetic values and the validated fallback address, even when live
  reminders are disabled. Run development tests with the in-memory backend.
- `notifications.request_reminder(event_id, grace_days=30, deferred_until=None)`
  rechecks eligibility and shares one gate for future manual and scheduled
  callers. It respects deferrals, suppresses events containing test orders
  (including mixed events, because findings are event-wide), and requires
  at least one confirmed identifier.
- `notifications.pending_reminder_ids(after=0, limit=100)` pages through
  outstanding delivery records. Call `reconcile_reminder(id)` to record
  pretix's native outcome before the native mail record expires.

Templates accept `{event_name}`, `{organizer_name}`, `{event_end}`,
`{days_since_event}`, `{issues_summary}`, `{event_url}` and `{due_date}`.
Double braces render literal braces. Substitution runs once. Email includes a plain-text version and an HTML
version with clickable links. Paste a full HTTP(S) URL or use
`[Link text](https://example.org/guide)`. HTML entered into the template
is displayed as text. It goes through the global pretix mail configuration with no order
association or automatic event BCC. The reminder recipient is the effective
event contact, with a validated administrator fallback.

The default cooldown is 14 days from the last confirmed mail handoff. The
default global cap is 10 newly queued reminders per rolling hour. A settings
row lock serializes the decision, and database uniqueness protects reminder
occurrences and outgoing-mail references. Explicit temporary SMTP rejections retry the same outgoing mail using
pretix's native retry cadence. A terminal failure or an unconfirmed delivery
blocks a new occurrence pending administrative review. The plugin stores
the recipient source and a status code. Recipient addresses and bodies
remain in pretix's native mail infrastructure.

The enable switch gates new reminder creation and is checked again before
an already queued message reaches the mail backend. Delivery already in
progress can finish. A durable delivery claim lets the monitor withhold
native watchdog replays after a crash or ambiguous disconnect. An explicit
temporary SMTP rejection is required to authorize a subsequent attempt.
The monitor cannot learn whether an ambiguous attempt reached the mail
server. Those attempts require administrative review. Successful handoff
does not establish inbox delivery.

## Administration

With the plugin migration applied, start a pretix admin session and open
**Global settings → Data Protection Monitor**. The dashboard shows counts,
dates, event status filters and aggregated findings. **Run audit now**
dispatches a manual audit to the pretix worker. A manual audit never sends
reminders. The worker stores one current aggregate state per event and
operational summaries of full runs.

Open an event for findings, native pretix deletion-page access, reminder
history, preview, manual reminder and deferral controls. The monitor only
links to the deletion page. A deferral expires at local midnight on the
chosen resume date. Use **Resume monitoring** to end it early. Enter
operational reasons without participant data.

The settings page supports dry-run acknowledgment, safe draft preview and
synthetic test mail, recipient fallback, exclusions by ID, grace period,
reminder interval, audit frequency and the hourly sending cap. Changes to
the audit scope require a fresh successful run and acknowledgment before
re-enabling live reminders. Audit frequency controls automatic full audits. The native periodic trigger
also dispatches bounded reminder and delivery-status checks. Existing pending mail is checked against current
delivery settings and deferrals before a worker hands it off.

The administration interface follows pretix’s active language. English and
German are included. Localizations for other languages can be added under
`pretix_dataprotection_monitor/locale/`. Rebuild a catalog after editing it
with `msgfmt -o django.mo django.po` from its `LC_MESSAGES` directory.

The organizer sidebar has a read-only **Data Protection Monitor** view for
users with organizer settings permission. The rating only uses events for
which the user has order access. It averages 10 for no known identifiers,
5 for manual review and 0 for action required, rounded to a whole number.
Deferred, not-due, excluded, unaudited events and scan errors do not count.
The score reflects supported audit checks. Scores of 8–10 are green, 5–7
are orange, and 0–4 are red. A low score briefly pulses the organizer’s
sidebar item when the browser allows motion. Users need order-write
permission on a flagged event to see its pretix deletion-page link.

## Regression tests

Use Python 3.13.18 to reproduce the first test environment.

```sh
python -m pip install -r requirements-test.txt
python -m pip install --no-deps -e .
python -m pytest -q
```

The default suite uses a fresh SQLite test database and in-memory email.
To run the same suite on a disposable local PostgreSQL server, set
`MONITOR_TEST_POSTGRES_PORT` and `MONITOR_TEST_POSTGRES_USER`. The role must
be able to create and drop the `pretix_monitor_test` database on that server.
The settings load no deployment config.

## Docker and GitHub

See [Docker integration and rollout](deployment/README.md) for the isolated
PostgreSQL and Celery checks and the existing-image deployment procedure.
Push this source folder after review. The GitHub workflow runs the integration
checks using synthetic fixtures and no deployment secrets.

## License

Licensed under the [Apache License 2.0](LICENSE).
