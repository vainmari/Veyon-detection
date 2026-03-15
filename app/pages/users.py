"""
app/pages/users.py
──────────────────
User management page  /users   (teacher only)
• Lists all users with role badges
• Create student account
• After creation: offer to retroactively assign anonymous events
• Delete student accounts
"""
from nicegui import ui

from app.core.auth import require_auth
from app.db.database import (
    create_user,
    delete_user,
    list_users,
    query_events,
)
from app.pages._nav import nav


@ui.page("/users")
def page_users() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)

    with ui.column().classes("w-full p-4 gap-4"):
        ui.label("User Management").classes("text-xl font-bold")

        # Info banner
        with ui.card().classes("w-full bg-blue-950 border border-blue-700"):
            ui.markdown(
                "**Student usernames must match their login name**"
                "  \n Events are always logged with the username, so any "
                "previously recorded events will be **automatically assigned** "
                "the moment a matching account is created."
            ).classes("text-sm text-blue-200")

        # ── Create student form ───────────────────────────────────────────────
        with ui.card().classes("w-full"):
            ui.label("Create Student Account").classes(
                "text-sm font-semibold text-gray-400 mb-2")
            with ui.row().classes("gap-3 items-end flex-wrap"):
                new_username = ui.input("Username (e.g. Jonas)").props(
                    "dense outlined").classes("w-56")
                new_password = ui.input(
                    "Password",
                    password=True,
                    password_toggle_button=True,
                ).props("dense outlined").classes("w-52")
                form_msg = ui.label("").classes("text-sm")

                def do_create() -> None:
                    uname = new_username.value.strip()
                    pwd   = new_password.value.strip()
                    if not uname or not pwd:
                        form_msg.set_text("Fill in both fields")
                        form_msg.classes(replace="text-sm text-red-400")
                        return
                    try:
                        new_uid = create_user(
                            uname, pwd, "student",
                            created_by_id=current["id"],
                        )
                        assigned = len(query_events(user_id=new_uid))
                        suffix   = (f" — {assigned} previous event(s) auto-assigned"
                                    if assigned else "")
                        form_msg.set_text(f"✅ Created '{uname}'{suffix}")
                        form_msg.classes(replace="text-sm text-green-400")
                        new_username.set_value("")
                        new_password.set_value("")
                        _refresh()
                    except Exception as e:
                        form_msg.set_text(f"Error: {e}")
                        form_msg.classes(replace="text-sm text-red-400")

                ui.button("Create", on_click=do_create).props("color=primary dense")

        # ── User table ────────────────────────────────────────────────────────
        cols = [
            {"name": "username",   "label": "Username",
             "field": "username",   "sortable": True, "align": "left"},
            {"name": "role",       "label": "Role",
             "field": "role",       "sortable": True, "align": "left"},
            {"name": "created_at", "label": "Created",
             "field": "created_at", "sortable": True, "align": "left"},
            {"name": "actions",    "label": "",
             "field": "id",         "align": "right"},
        ]
        tbl = ui.table(columns=cols, rows=[], row_key="id").classes("w-full")
        tbl.props("dense flat bordered")

        tbl.add_slot("body-cell-role", """
            <q-td :props="props">
                <q-badge :color="props.row.role === 'teacher' ? 'orange' : 'blue'">
                    {{ props.row.role }}
                </q-badge>
            </q-td>
        """)
        tbl.add_slot("body-cell-actions", """
            <q-td :props="props">
                <q-btn v-if="props.row.role === 'student'"
                    flat dense round icon="delete" color="red"
                    @click="$parent.$emit('delete_user', props.row)" />
            </q-td>
        """)

        def handle_delete(e) -> None:
            uid = e.args.get("id")
            if uid and int(uid) != current["id"]:
                delete_user(int(uid))
                ui.notify("User deleted", type="warning")
                _refresh()

        tbl.on("delete_user", handle_delete)

    def _refresh() -> None:
        tbl.rows = list_users()
        tbl.update()

    _refresh()