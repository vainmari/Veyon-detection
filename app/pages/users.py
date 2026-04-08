"""
app/pages/users.py
──────────────────
User management page  /users  (teacher + admin)
"""
import asyncio
from nicegui import ui

from app.core.auth import is_admin, require_auth
from app.db.database import (
    create_user,
    delete_user,
    list_users,
    query_events,
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

                    create_btn.props("disable")
                    form_msg.set_text(t("users_creating"))
                    form_msg.classes(
                        replace="text-sm text-gray-500 dark:text-gray-400")

                    loop = asyncio.get_event_loop()
                    try:
                        new_uid = await loop.run_in_executor(
                            None, lambda: create_user(
                                uname, pwd, role,
                                created_by_id=current["id"],
                            )
                        )
                        if role == "student":
                            assigned = await loop.run_in_executor(
                                None, lambda: len(query_events(user_id=new_uid))
                            )
                            suffix = (
                                t("users_auto_assigned").format(n=assigned)
                                if assigned else ""
                            )
                            form_msg.set_text(
                                t("users_created_student").format(
                                    uname=uname, suffix=suffix))
                        else:
                            form_msg.set_text(
                                t("users_created_teacher").format(uname=uname))

                        form_msg.classes(replace="text-sm text-green-600")
                        new_username.set_value("")
                        new_password.set_value("")
                        await loop.run_in_executor(None, _refresh)

                    except Exception as e:
                        form_msg.set_text(t("users_error").format(e=e))
                        form_msg.classes(replace="text-sm text-red-500")
                    finally:
                        create_btn.props(remove="disable")

                create_btn.on_click(do_create)

        # ── User table ────────────────────────────────────────────────────────
        cols = [
            {"name": "username",   "label": t("users_col_username"),
             "field": "username",   "sortable": True, "align": "left"},
            {"name": "role",       "label": t("users_col_role"),
             "field": "role",       "sortable": True, "align": "left"},
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

        if admin:
            tbl.add_slot("body-cell-actions", """
                <q-td :props="props">
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
            if not uid or int(uid) == current["id"]:
                return
            can_delete = (
                (admin and role in ("teacher", "student")) or
                (not admin and role == "student")
            )
            if not can_delete:
                return
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    None, lambda: delete_user(int(uid))
                )
                ui.notify(t("users_deleted"), type="warning")
                await loop.run_in_executor(None, _refresh)
            except Exception as ex:
                ui.notify(t("users_delete_failed").format(e=ex), type="negative")

        tbl.on("delete_user", handle_delete)

    def _refresh() -> None:
        tbl.rows = list_users()
        tbl.update()

    _refresh()
