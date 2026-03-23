"""
app/pages/login.py
──────────────────
Login page  /login
No nav bar — shown to unauthenticated users.
Redirects teacher → /   (dashboard)
Redirects student → /history
"""
from nicegui import ui

from app.core.auth import get_session_user, set_session_user
from app.db.database import verify_password


@ui.page("/login")
def page_login() -> None:
    # Already logged in → skip
    user = get_session_user()
    if user:
        if user["role"] == "admin":
            ui.navigate.to("/users")
        elif user["role"] == "student":
            ui.navigate.to("/history")
        else:
            ui.navigate.to("/")
        return

    with ui.column().classes(
        "absolute-center items-center gap-4 w-full"
    ).style("max-width: 360px; margin: auto;"):

        ui.label("🎓 Veyon AI Monitor").classes("text-2xl font-bold text-center")
        ui.label("Sign in to continue").classes("text-gray-400 text-sm mb-2")

        with ui.card().classes("w-full gap-3 p-6"):
            username = ui.input("Username").props(
                "outlined dense autofocus"
            ).classes("w-full")
            password = ui.input("Password", password=True, password_toggle_button=True
                                ).props("outlined dense").classes("w-full")
            error    = ui.label("").classes("text-red-400 text-sm")

            def do_login() -> None:
                u = verify_password(username.value.strip(), password.value)
                if u is None:
                    error.set_text("Invalid username or password")
                    password.set_value("")
                    return
                set_session_user(u)
                if u["role"] == "admin":
                    ui.navigate.to("/users")
                elif u["role"] == "student":
                    ui.navigate.to("/history")
                else:
                    ui.navigate.to("/")

            ui.button("Sign in", on_click=do_login).props(
                "color=primary"
            ).classes("w-full mt-2")

            # Allow pressing Enter in the password field to submit
            password.on("keydown.enter", do_login)
            username.on("keydown.enter", lambda: password.run_method("focus"))