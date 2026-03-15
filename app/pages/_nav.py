"""Shared navigation bar — imported by every page module."""
from nicegui import ui


def nav() -> None:
    with ui.header().classes(
        "bg-gray-900 text-white px-6 py-2 flex items-center gap-6 shadow-md"
    ):
        ui.label("🎓 Veyon AI Monitor").classes("font-bold text-base mr-4")
        ui.link("Dashboard", "/").classes(
            "text-gray-300 hover:text-white text-sm no-underline")
        ui.link("History",   "/history").classes(
            "text-gray-300 hover:text-white text-sm no-underline")
        ui.link("Settings",  "/settings").classes(
            "text-gray-300 hover:text-white text-sm no-underline")