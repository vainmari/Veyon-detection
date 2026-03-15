"""
app/main.py
───────────
Application entry point.
Registers startup hooks and imports all pages (side-effect: @ui.page routes fire).
"""
import threading

from nicegui import app as nicegui_app, ui

from app.config import STORAGE_SECRET
from app.db.database import ensure_default_teacher, init_db, seed_classes
from app.services.monitor_service import drain_worker

# Register pages
import app.pages.login      # noqa: F401
import app.pages.dashboard  # noqa: F401
import app.pages.history    # noqa: F401
import app.pages.settings   # noqa: F401
import app.pages.users      # noqa: F401


@nicegui_app.on_startup
def _startup() -> None:
    init_db()
    seed_classes()
    ensure_default_teacher()  # creates admin/admin if no users exist yet
    threading.Thread(target=drain_worker, daemon=True, name="drain").start()


ui.run(
    title          = "Veyon AI Monitor",
    port           = 8080,
    dark           = True,
    storage_secret = STORAGE_SECRET,
    favicon        = "🎓",
    show           = True,
    reload         = False,
)