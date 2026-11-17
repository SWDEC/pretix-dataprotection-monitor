from pretix.base.plugins import PluginConfig


class DataProtectionMonitorApp(PluginConfig):
    name = "pretix_dataprotection_monitor"

    class PretixPluginMeta:
        name = "Data Protection Monitor"
        author = "SWD-EC"
        version = "0.4.0"
        description = "Installation-wide read-only audit of completed events."
        category = "FEATURE"
        visible = False
        compatibility = "pretix==2026.9.1"

    def ready(self):
        from . import runs, scheduler, signals  # noqa: F401
