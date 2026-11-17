# Docker integration and rollout

## Isolated integration environment

The Compose project uses the official pretix 2026.9.1 image, PostgreSQL 14
and Redis 6. It publishes no ports and mounts no deployment data. Its
credentials are fixtures used only on the private Compose network. The test
settings use Django's in-memory mail backend.

Run these commands from the repository root on a Docker host with the
Compose plugin. Use the dedicated project name shown.

```sh
docker compose -p pretix-monitor-test -f deployment/compose.integration.yaml build
docker compose -p pretix-monitor-test -f deployment/compose.integration.yaml run --rm integration -m pytest -q
docker compose -p pretix-monitor-test -f deployment/compose.integration.yaml run --rm integration -m tests.worker_smoke
```

The smoke test invokes pretix's actual periodic command, sends tasks through
Redis to a separate Celery process, checks saved findings for a synthetic
event and verifies that dry-run mode created no outgoing mail. Use a fresh
test project for a repeat of the worker smoke test. The following command
removes this isolated project's disposable test data.

```sh
docker compose -p pretix-monitor-test -f deployment/compose.integration.yaml down --volumes
```

GitHub Actions performs the same integration checks after the source is
pushed. The amd64 test image may require emulation on ARM hosts.

## Add the plugin to the existing pretix image

The deployment target is pretix 2026.9.1, Python 3.13.18, Django 5.2.19,
Celery 5.6.3 and the four pinned extensions in
`requirements-test.txt`. Keep the existing custom image and its
configuration. Install the monitor from its public Git release tag.

Add the install step after installing Git, pretix and the other extensions.
If the existing build switches to the runtime user, install before that
switch and restore the existing runtime user afterward. Adjust the
repository URL if you publish under a different owner.

```dockerfile
RUN env -u DJANGO_SETTINGS_MODULE python -m pip install --no-cache-dir --no-deps \
    "pretix-dataprotection-monitor @ git+https://github.com/SWDEC/pretix-dataprotection-monitor.git@v0.5.1" \
    && python -m pip check
```

Use the same rebuilt image for the web process, Celery worker and the
container executing pretix's periodic command. The current deployment runs
Gunicorn and Celery under Supervisor in `pretix-pretix-1`. Keep the
deployment's existing service and image layout. Keep the existing `make production` image build step so pretix collects
the monitor stylesheet along with the other static assets.

## Staging and deployment checklist

1. Test the wheel on an isolated staging stack with PostgreSQL and Redis.
   Back up the database before migrating an existing environment. Verify
   the installed versions in the new image with `python -m pip show` and
   compare them to the pinned target.
2. Pause the deployment's periodic trigger for the image switch. Run
   `python -m pretix migrate --noinput` in the new image with the target
   environment's pretix settings and database, then start the web and
   Celery processes on it. The official all-in-one image can run migrations
   at startup. Coordinate any separate migration job with that behavior.
3. Run `python -m pretix check`. Verify that
   `python -m pretix runperiodic --list-tasks` includes
   `pretix_dataprotection_monitor.signals.schedule_monitor`. Confirm that
   the host scheduler invokes the existing `pretix cron` entry point and
   can reach the correct container. The receiver checks whether an audit
   is due and limits delivery ticks to a minimum five-minute interval.
   It respects the host's actual invocation cadence. Resume the trigger
   once the deployed build is ready. If checking only this plugin manually
   in staging, run:

   ```sh
   python -m pretix runperiodic --tasks pretix_dataprotection_monitor.signals.schedule_monitor
   ```

4. In an active pretix admin session, open **Global settings → Data
   Protection Monitor**. Confirm that live sending is disabled. Use
   **Run audit now** if no scheduled audit has finished. Confirm the
   worker completes the audit and the dashboard records the result.
   Verify a few representative events using category counts and native
   control pages. Keep participant details out of deployment records.
5. Test mail only through a staging mail sink or a specifically authorized
   fallback mailbox. **Preview email** uses synthetic event data and
   **Send test email** uses the configured fallback. Verify the staging
   recipient, subject, body and mail logs. A manual audit creates no mail.
6. Keep live sending disabled until an administrator has reviewed the
   initial findings and explicitly approved enabling reminders. Record a
   valid fallback, choose the hourly cap and save the approval in the
   plugin settings. Local and CI verification does not constitute this
   approval.

Useful host commands for the existing all-in-one layout, after the
new image is running with its normal settings:

```sh
docker exec pretix-pretix-1 python -m pretix check
docker exec pretix-pretix-1 python -m pretix runperiodic --list-tasks
docker top pretix-pretix-1 -eo pid,ppid,comm
```

## Recovery and rollback

- If a worker stalls, confirm Celery and Redis health. A later periodic
  tick reclaims an expired delivery lease. A fresh audit can replace a
  run whose heartbeat has been inactive for two hours. The audit retains
  the last stored event results until a new check replaces them.
- For notification issues, disable sending first. An already queued
  reminder is checked again before delivery. A mail handoff already under
  way can finish. The event history shows pending or uncertain attempts.
  Compare uncertain attempts with the mail provider before any manual
  resend.
- Before removing the plugin from the image, leave its mail guard installed
  with sending disabled until monitor mail already in pretix's native queue
  has settled or been canceled through pretix operations. Check for monitor
  outgoing mail in `queued`, `inflight`, or `awaiting_retry`; report counts
  and statuses only. Removing the guard before draining the queue would
  allow already-enqueued pretix jobs to run without the plugin checks.
- Pause the periodic trigger for rollback, then redeploy the previously
  tested image to the web and worker processes together. Preserve the
  plugin tables for diagnosis and rollback recovery. Retest pretix and its
  scheduler before resuming the trigger. Restore a database snapshot only
  through the deployment's normal recovery procedure, with awareness of
  unrelated writes since the snapshot.
