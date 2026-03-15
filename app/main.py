"""
app/main.py
───────────
Application factory.
  • Registers startup hooks (DB init, drain thread)
  • Imports pages so their @ui.page decorators fire
  • Calls ui.run() — NiceGUI mounts onto FastAPI internally
"""
import threading

from nicegui import app as nicegui_app, ui

from app.config import STORAGE_SECRET
from app.db.database import init_db
from app.services.monitor_service import drain_worker

# Import pages — side-effect: registers @ui.page routes
import app.pages.dashboard   # noqa: F401
import app.pages.history     # noqa: F401
import app.pages.settings    # noqa: F401


@nicegui_app.on_startup
def _startup() -> None:
    init_db()
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