"""
app/pages/audit.py
──────────────────
Audit Log page  /audit  (teacher + admin)

Shows the chronological record of every significant action performed
in the system: user management, model activation, alert rule changes, etc.
"""
from nicegui import ui

from app.core.auth import require_auth
from app.db.database import list_audit_log, list_users
from app.pages._nav import nav
from app.translate import t

_ACTION_COLORS = {
    "user.create":          "green",
    "user.delete":          "red",
    "user.password_change": "amber",
    "model.activate":       "blue",
    "model.import":         "cyan",
    "model.update":         "light-blue",
    "model.delete":         "red",
    "alert.rule":           "orange",
    "schedule.create":      "teal",
    "schedule.update":      "teal",
    "schedule.delete":      "red",
    "schedule.auto_start":  "green",
    "schedule.auto_stop":   "grey",
    "group.create":         "purple",
    "group.update":         "purple",
    "group.delete":         "red",
    "group.assign":         "deep-purple",
    "group.import_veyon":   "indigo",
    "monitor.start":        "green",
    "monitor.stop":         "grey",
    "settings.save":        "brown",
}


def _badge_color(action: str) -> str:
    for prefix, color in _ACTION_COLORS.items():
        if action.startswith(prefix):
            return color
    return "gray"


@ui.page("/audit")
def page_audit() -> None:
    current = require_auth(required_role="teacher_or_admin")
    if current is None:
        return
    nav(current)

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label(t("audit_title")).classes("text-xl font-bold")

        # ── Filters ───────────────────────────────────────────────────────────
        with ui.card().classes("w-full"):
            with ui.row().classes("gap-4 flex-wrap items-end"):
                users = [{"label": t("audit_all_users"), "value": ""}] + [
                    {"label": u["username"], "value": str(u["id"])}
                    for u in list_users()
                ]
                f_user = ui.select(
                    {u["value"]: u["label"] for u in users},
                    value="", label=t("audit_user"),
                ).props("dense outlined").classes("w-44")

                f_action = ui.input(
                    label=t("audit_action_filter"),
                    placeholder=t("audit_action_ph"),
                ).props("dense outlined").classes("w-44")

                f_limit = ui.number(
                    t("audit_max_rows"), value=200, min=10, max=2000,
                ).props("dense outlined").classes("w-24")

                ui.button(t("audit_filter_btn"), on_click=lambda: _load()).props(
                    "color=primary dense")

        # ── Table ─────────────────────────────────────────────────────────────
        cols = [
            {"name": "created_at", "label": t("audit_col_time"),
             "field": "created_at",  "sortable": True, "align": "left"},
            {"name": "username",   "label": t("audit_col_user"),
             "field": "username",    "sortable": True, "align": "left"},
            {"name": "action",     "label": t("audit_col_action"),
             "field": "action",      "sortable": True, "align": "left"},
            {"name": "entity",     "label": t("audit_col_entity"),
             "field": "entity",      "sortable": True, "align": "left"},
            {"name": "entity_id",  "label": t("audit_col_id"),
             "field": "entity_id",   "sortable": True, "align": "center"},
            {"name": "detail",     "label": t("audit_col_detail"),
             "field": "detail",      "sortable": False, "align": "left"},
        ]
        tbl = ui.table(columns=cols, rows=[], row_key="id").classes("w-full")
        tbl.props("dense flat bordered")

        tbl.add_slot("body-cell-action", """
            <q-td :props="props">
                <q-badge :color="props.row._color" class="q-mr-xs">
                    {{ props.row.action }}
                </q-badge>
            </q-td>""")

        count_lbl = ui.label("").classes("text-xs text-gray-500 mt-1")

        def _load() -> None:
            uid    = int(f_user.value)   if f_user.value   else None
            action = f_action.value.strip() or None
            rows   = list_audit_log(
                limit=int(f_limit.value or 200),
                user_id=uid,
                action=action,
            )
            for r in rows:
                r["username"]  = r.get("username") or t("audit_system")
                r["entity"]    = r.get("entity")   or "—"
                r["entity_id"] = r.get("entity_id") or "—"
                r["detail"]    = r.get("detail")   or "—"
                r["_color"]    = _badge_color(r["action"])
            tbl.rows = rows
            tbl.update()
            count_lbl.set_text(t("audit_entries").format(n=len(rows)))

        _load()
