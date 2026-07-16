"""
app/pages/users.py
──────────────────
User management page  /users  (teacher + admin)

Heavy DB work (auto-assigning historical events on create, nullifying events
on delete) runs in background asyncio tasks so the UI returns immediately.
"""
import asyncio
from nicegui import ui
from nicegui import context as _ctx

from app.core.auth import is_admin, require_auth
from app.db.database import (
    MIN_PASSWORD_LENGTH,
    activate_user,
    auto_assign_user_events,
    create_user,
    delete_user,
    list_users,
    nullify_user_events,
)
from app.pages._nav import nav
from app.translate import t


@ui.page("/users")
def page_users() -> None:
    current = require_auth(required_role="teacher_or_admin")
    if current is None:
        return
    nav(current)
    admin = is_admin(current)

    with ui.column().classes("w-full p-4 gap-4"):
        ui.label(t("users_title")).classes("text-xl font-bold")

        # ── Info banner ───────────────────────────────────────────────────────
        with ui.card().classes(
            "w-full "
            "bg-blue-50 border border-blue-300 "
            "dark:bg-blue-950 dark:border-blue-700"
        ):
            ui.markdown(
                t("users_info_admin") if admin else t("users_info_teacher")
            ).classes("text-sm text-blue-900 dark:text-blue-200")

        # ── Create user form ──────────────────────────────────────────────────
        with ui.card().classes("w-full"):
            ui.label(t("users_create_section")).classes(
                "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-2")
            with ui.row().classes("gap-3 items-end flex-wrap"):
                new_username = ui.input(t("users_username")).props(
                    "dense outlined").classes("w-52")
                new_password = ui.input(
                    t("users_temp_password"),
                    password=True,
                    password_toggle_button=True,
                ).props("dense outlined").classes("w-52")

                if admin:
                    new_role = ui.select(
                        {"student": t("users_role_student"),
                         "teacher": t("users_role_teacher")},
                        value="student", label=t("users_role"),
                    ).props("dense outlined").classes("w-32")
                    role_hint = ui.label(t("users_hint_windows")).classes(
                        "text-xs text-gray-500 dark:text-gray-500 self-end pb-1")

                    def _update_hint() -> None:
                        role_hint.set_text(
                            t("users_hint_student") if new_role.value == "student"
                            else t("users_hint_any")
                        )
                    new_role.on_value_change(lambda _: _update_hint())

                form_msg   = ui.label("").classes("text-sm")
                create_btn = ui.button(t("users_create_btn")).props("color=primary dense")

                async def do_create() -> None:
                    uname = new_username.value.strip()
                    pwd   = new_password.value.strip()
                    role  = new_role.value if admin else "student"

                    if not uname or not pwd:
                        form_msg.set_text(t("users_fill_fields"))
                        form_msg.classes(replace="text-sm text-red-500")
                        return

                    # Same policy as the DB layer (validate_password) —
                    # checked here too for a friendlier inline error.
                    if len(pwd) < MIN_PASSWORD_LENGTH:
                        form_msg.set_text(
                            t("pwd_err_short").format(n=MIN_PASSWORD_LENGTH))
                        form_msg.classes(replace="text-sm text-red-500")
                        return

                    create_btn.props("loading")
                    form_msg.set_text(t("users_creating"))
                    form_msg.classes(replace="text-sm text-gray-500 dark:text-gray-400")

                    loop = asyncio.get_event_loop()
                    try:
                        new_uid = await loop.run_in_executor(
                            None, lambda: create_user(
                                uname, pwd, role,
                                created_by_id=current["id"],
                            )
                        )
                        # Show success immediately — event assignment runs in background
                        if role == "student":
                            form_msg.set_text(
                                t("users_created_linking").format(uname=uname))
                        else:
                            form_msg.set_text(
                                t("users_created_teacher").format(uname=uname))
                        form_msg.classes(replace="text-sm text-green-600")
                        new_username.set_value("")
                        new_password.set_value("")
                        _refresh()

                        if role == "student":
                            asyncio.create_task(
                                _assign_events_bg(new_uid, uname, form_msg, _ctx.client))

                    except Exception as e:
                        form_msg.set_text(t("users_error").format(e=e))
                        form_msg.classes(replace="text-sm text-red-500")
                    finally:
                        create_btn.props(remove="loading")

                create_btn.on_click(do_create)

        # ── User table ────────────────────────────────────────────────────────
        cols = [
            {"name": "username",   "label": t("users_col_username"),
             "field": "username",   "sortable": True, "align": "left"},
            {"name": "role",       "label": t("users_col_role"),
             "field": "role",       "sortable": True, "align": "left"},
            {"name": "is_active",  "label": t("users_col_status"),
             "field": "is_active",  "sortable": True, "align": "left"},
            {"name": "created_at", "label": t("users_col_created"),
             "field": "created_at", "sortable": True, "align": "left"},
            {"name": "actions",    "label": "",
             "field": "id",         "align": "right"},
        ]
        tbl = ui.table(columns=cols, rows=[], row_key="id").classes("w-full")
        tbl.props("dense flat bordered")

        tbl.add_slot("body-cell-role", """
            <q-td :props="props">
                <q-badge :color="props.row.role === 'admin'    ? 'red'    :
                                 props.row.role === 'teacher'  ? 'orange' : 'blue'">
                    {{ props.row.role }}
                </q-badge>
            </q-td>
        """)

        tbl.add_slot("body-cell-is_active", """
            <q-td :props="props">
                <q-badge :color="props.row.is_active ? 'green' : 'grey'">
                    {{ props.row.is_active ? '""" + t("users_status_active") + """' : '""" + t("users_status_inactive") + """' }}
                </q-badge>
            </q-td>
        """)

        if admin:
            tbl.add_slot("body-cell-actions", """
                <q-td :props="props">
                    <q-btn
                        v-if="props.row.role === 'student' && !props.row.is_active"
                        flat dense round icon="lock_open" color="primary"
                        @click="$parent.$emit('activate_user', props.row)"
                    />
                    <q-btn
                        v-if="props.row.role !== 'admin'"
                        flat dense round icon="delete" color="red"
                        @click="$parent.$emit('delete_user', props.row)"
                    />
                </q-td>
            """)
        else:
            tbl.add_slot("body-cell-actions", """
                <q-td :props="props">
                    <q-btn
                        v-if="props.row.role === 'student' && !props.row.is_active"
                        flat dense round icon="lock_open" color="primary"
                        @click="$parent.$emit('activate_user', props.row)"
                    />
                    <q-btn
                        v-if="props.row.role === 'student'"
                        flat dense round icon="delete" color="red"
                        @click="$parent.$emit('delete_user', props.row)"
                    />
                </q-td>
            """)

        async def handle_delete(e) -> None:
            row  = e.args
            uid  = row.get("id")
            role = row.get("role", "")
            uname = row.get("username", str(uid))
            if not uid or int(uid) == current["id"]:
                return
            can_delete = (
                (admin and role in ("teacher", "student")) or
                (not admin and role == "student")
            )
            if not can_delete:
                return
            # Remove from table immediately so the user can't double-click
            tbl.rows = [r for r in tbl.rows if r.get("id") != uid]
            tbl.update()
            client = _ctx.client
            asyncio.create_task(_delete_user_bg(int(uid), uname, client))

        tbl.on("delete_user", handle_delete)

        # ── Set password / activate dialog ────────────────────────────────────
        _activate_row: list[dict] = [{}]
        with ui.dialog() as activate_dialog, ui.card().classes("w-96"):
            ui.label(t("users_activate_title")).classes("text-base font-semibold mb-2")
            activate_name_lbl = ui.label("").classes(
                "font-mono text-sm text-yellow-600 dark:text-yellow-300 mb-1")
            act_pwd  = ui.input(
                t("users_temp_password"),
                password=True, password_toggle_button=True,
            ).props("dense outlined").classes("w-full")
            act_msg  = ui.label("").classes("text-sm")
            with ui.row().classes("gap-2 mt-2"):
                async def do_activate() -> None:
                    pwd = act_pwd.value.strip()
                    if len(pwd) < MIN_PASSWORD_LENGTH:
                        act_msg.set_text(
                            t("pwd_err_short").format(n=MIN_PASSWORD_LENGTH))
                        act_msg.classes(replace="text-sm text-red-500")
                        return
                    uid = _activate_row[0].get("id")
                    if not uid:
                        return
                    loop = asyncio.get_event_loop()
                    try:
                        await loop.run_in_executor(
                            None, lambda: activate_user(int(uid), pwd)
                        )
                        act_msg.set_text(t("users_activated").format(
                            uname=_activate_row[0].get("username", "")))
                        act_msg.classes(replace="text-sm text-green-600")
                        act_pwd.set_value("")
                        _refresh()
                    except Exception as e:
                        act_msg.set_text(t("users_error").format(e=e))
                        act_msg.classes(replace="text-sm text-red-500")

                ui.button(t("users_activate_btn"), on_click=do_activate).props(
                    "color=primary dense")
                ui.button(t("users_create_section_cancel"), on_click=activate_dialog.close).props(
                    "flat dense")

        def handle_activate(e) -> None:
            row = e.args
            _activate_row[0] = row
            activate_name_lbl.set_text(row.get("username", ""))
            act_pwd.set_value("")
            act_msg.set_text("")
            activate_dialog.open()

        tbl.on("activate_user", handle_activate)

    def _refresh() -> None:
        tbl.rows = list_users()
        tbl.update()

    _refresh()

    # ── Background tasks ──────────────────────────────────────────────────────

    async def _assign_events_bg(
        user_id: int, username: str, msg_label: ui.label, client,
    ) -> None:
        """Assign historical events to a newly created student in the background."""
        loop = asyncio.get_event_loop()
        try:
            n = await loop.run_in_executor(
                None, lambda: auto_assign_user_events(username, user_id)
            )
            with client:
                if n:
                    msg_label.set_text(
                        t("users_auto_assigned_done").format(uname=username, n=n))
                else:
                    msg_label.set_text(
                        t("users_created_student").format(uname=username, suffix=""))
        except Exception:
            pass  # non-critical — user was already created successfully

    async def _delete_user_bg(user_id: int, username: str, client) -> None:
        """Nullify events in batches then delete the user row."""
        loop = asyncio.get_event_loop()
        with client:
            notif = ui.notify(
                t("users_deleting_bg").format(uname=username),
                type="ongoing", spinner=True, timeout=5000,
            )
        try:
            await loop.run_in_executor(
                None, lambda: nullify_user_events(user_id)
            )
            await loop.run_in_executor(
                None, lambda: delete_user(user_id)
            )
            with client:
                if notif is not None:
                    notif.dismiss()
                ui.notify(t("users_deleted"), type="warning", timeout=5000)
                _refresh()
        except Exception as ex:
            with client:
                if notif is not None:
                    notif.dismiss()
                ui.notify(t("users_delete_failed").format(e=ex), type="negative", timeout=5000)
                _refresh()  # re-add the row if delete failed
