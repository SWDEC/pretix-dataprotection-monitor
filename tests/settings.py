import os
import tempfile

_test_data = tempfile.TemporaryDirectory(prefix="pretix-monitor-tests-")
os.environ["DATA_DIR"] = _test_data.name
os.environ["PRETIX_CONFIG_FILE"] = os.devnull

from pretix.settings import *  # noqa: E402,F403

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

if os.environ.get("MONITOR_TEST_POSTGRES_PORT"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "postgres",
            "USER": os.environ["MONITOR_TEST_POSTGRES_USER"],
            "HOST": os.environ.get("MONITOR_TEST_POSTGRES_HOST", "127.0.0.1"),
            "PASSWORD": os.environ.get("MONITOR_TEST_POSTGRES_PASSWORD", ""),
            "PORT": os.environ["MONITOR_TEST_POSTGRES_PORT"],
            "TEST": {"NAME": "pretix_monitor_test"},
        },
    }

if os.environ.get("MONITOR_TEST_BROKER_URL"):
    CELERY_BROKER_URL = os.environ["MONITOR_TEST_BROKER_URL"]
    CELERY_TASK_ALWAYS_EAGER = False
