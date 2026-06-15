"""
app/main.py
───────────
Application entry point.
Registers startup hooks and imports all pages (side-effect: @ui.page routes fire).
"""
import logging
import sys
import threading

from nicegui import app as nicegui_app, ui

from app.config import (
    BIND_HOST,
    BIND_PORT,
    INITIAL_ADMIN_PASSWORD,
    INITIAL_ADMIN_USERNAME,
    STORAGE_SECRET,
)
from app.db._core import _conn
from app.db.database import (
    ensure_default_admin,
    finish_stale_runs,
    init_db,
    seed_classes,
)
from app.services.monitor_service import drain_worker
from app.services.schedule_service import start_scheduler

log = logging.getLogger(__name__)

# Register pages
import app.pages.login      # noqa: F401
import app.pages.dashboard  # noqa: F401
import app.pages.history    # noqa: F401
import app.pages.analytics  # noqa: F401
import app.pages.alerts     # noqa: F401
import app.pages.reports    # noqa: F401
import app.pages.groups     # noqa: F401
import app.pages.schedules  # noqa: F401
import app.pages.models     # noqa: F401
import app.pages.audit      # noqa: F401
import app.pages.settings   # noqa: F401
import app.pages.users      # noqa: F401


def _bootstrap_admin() -> None:
    """
    On a fresh database (zero users), create the initial admin account
    using credentials supplied via the INITIAL_ADMIN_USERNAME /
    INITIAL_ADMIN_PASSWORD environment variables.

    Behaviour:
      • If a user already exists → no-op, returns immediately.
      • If DB is empty AND INITIAL_ADMIN_PASSWORD is unset → prints a clear
        instruction and exits with a non-zero status code.

    Note: in practice the exit path only triggers when an operator removed
    the password from an existing .env — app/config.py auto-creates a
    missing .env with the documented admin/admin defaults, so a fresh
    install always boots with those credentials. Change the admin password
    through the UI after the first login.
    """
    c = _conn()
    user_count = c.execute("SELECT COUNT(*) FROM user").fetchone()[0]
    if user_count > 0:
        return  # already initialised — env var is irrelevant from now on

    if not INITIAL_ADMIN_PASSWORD:
        sys.stderr.write(
            "\n"
            "ERROR: The database has no users yet and INITIAL_ADMIN_PASSWORD\n"
            "is not set. Add the following to your .env file:\n\n"
            "    INITIAL_ADMIN_USERNAME=admin\n"
            "    INITIAL_ADMIN_PASSWORD=<choose a strong password>\n\n"
            "Then restart. The variable is only consulted on a fresh database;\n"
            "once an account exists you can manage users through the UI.\n"
        )
        sys.exit(1)

    ensure_default_admin(INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD)
    log.info(
        "bootstrap: created initial admin %r from environment variable",
        INITIAL_ADMIN_USERNAME,
    )


@nicegui_app.on_startup
def _startup() -> None:
    init_db()
    seed_classes()
    _bootstrap_admin()
    # Close monitoring_run rows left 'running' by a crash/hard kill — must
    # happen before the scheduler can open a new run.
    n_stale = finish_stale_runs()
    if n_stale:
        log.info("startup: marked %d stale monitoring run(s) as interrupted", n_stale)
    threading.Thread(target=drain_worker, daemon=True, name="drain").start()
    start_scheduler()


ui.run(
    title          = "Veyon AI Monitor",
    # Explicit binding: 0.0.0.0 keeps the UI reachable from other machines on
    # the LAN (the normal deployment); override via BIND_HOST/BIND_PORT in .env.
    host           = BIND_HOST,
    port           = BIND_PORT,
    dark           = True,
    storage_secret = STORAGE_SECRET,
    favicon        = "🎓",
    show           = True,
    reload         = False,
)
