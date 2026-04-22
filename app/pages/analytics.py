"""
app/pages/analytics.py
──────────────────────
Analytics page  /analytics

Teacher view  — full filters (computer, student), all-student charts
Student view  — own data only, no student selector
"""
from __future__ import annotations

from datetime import date, timedelta

from nicegui import ui

from app.core.auth import require_auth
from app.db.database import (
    get_class_distribution,
    get_daily_detections,
    get_student_activity,
    get_summary_stats,
    list_computers,
    list_users,
)
from app.pages._nav import nav
from app.translate import t


@ui.page("/analytics")
def page_analytics() -> None:
    current = require_auth()
    if current is None:
        return
    nav(current)
    is_teacher = current["role"] == "teacher"

    with ui.column().classes("w-full p-4 gap-4"):
        ui.label(t("analytics_title")).classes("text-2xl font-bold")

        with ui.card().classes("w-full"):
            with ui.row().classes("gap-4 flex-wrap items-end"):

                today        = date.today()
                default_from = (today - timedelta(days=29)).isoformat()
                default_to   = today.isoformat()

                f_from = ui.input(t("analytics_from"), value=default_from).props(
                    "dense outlined type=date").classes("w-40")
                f_to   = ui.input(t("analytics_to"),   value=default_to).props(
                    "dense outlined type=date").classes("w-40")

                if is_teacher:
                    computers = [{"label": t("analytics_all_computers"), "value": ""}] + [
                        {"label": c["name"], "value": str(c["id"])}
                        for c in list_computers()
                    ]
                    f_computer = ui.select(
                        {c["value"]: c["label"] for c in computers},
                        value="", label=t("analytics_computer"),
                    ).props("dense outlined").classes("w-44")

                    students = [{"label": t("analytics_all_students"), "value": ""}] + [
                        {"label": u["username"], "value": str(u["id"])}
                        for u in list_users() if u["role"] == "student"
                    ]
                    f_student = ui.select(
                        {s["value"]: s["label"] for s in students},
                        value="", label=t("analytics_student"),
                    ).props("dense outlined").classes("w-44")

                ui.button(t("analytics_apply"), on_click=lambda: _refresh()).props("color=primary")

        # ── Summary stat cards ────────────────────────────────────────────────
        with ui.row().classes("w-full gap-4 flex-wrap"):
            card_total    = _stat_card(t("analytics_total_events"),     "—", "gray")
            card_hits     = _stat_card(t("analytics_detection_events"), "—", "red")
            card_students = _stat_card(t("analytics_active_students"),  "—", "blue")
            card_top      = _stat_card(t("analytics_most_detected"),    "—", "green")

        # ── Charts row ────────────────────────────────────────────────────────
        with ui.row().classes("w-full gap-4 flex-wrap items-start"):

            with ui.card().classes("flex-1 min-w-72"):
                ui.label(t("analytics_class_dist")).classes(
                    "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-2")
                donut = ui.echart({
                    "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
                    "legend":  {"bottom": 0, "textStyle": {"color": "#888"}},
                    "series": [{
                        "type":   "pie",
                        "radius": ["45%", "70%"],
                        "label":  {"color": "#888"},
                        "data":   [],
                    }],
                }).classes("w-full").style("height:320px")

            with ui.card().classes("flex-2 min-w-96"):
                ui.label(t("analytics_daily_activity")).classes(
                    "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-2")
                bar_daily = ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "legend":  {"data": [t("analytics_all_events"), t("analytics_detections")],
                                "textStyle": {"color": "#888"}},
                    "grid":    {"left": "3%", "right": "4%",
                                "bottom": "3%", "containLabel": True},
                    "xAxis":   {"type": "category", "data": [],
                                "axisLabel": {"color": "#888", "rotate": 35}},
                    "yAxis":   {"type": "value",
                                "axisLabel": {"color": "#888"}},
                    "series": [
                        {"name": t("analytics_all_events"), "type": "bar",
                         "data": [], "itemStyle": {"color": "#4b8cf5"},
                         "barMaxWidth": 32},
                        {"name": t("analytics_detections"), "type": "bar",
                         "data": [], "itemStyle": {"color": "#f56262"},
                         "barMaxWidth": 32},
                    ],
                }).classes("w-full").style("height:320px")

        if is_teacher:
            with ui.card().classes("w-full"):
                ui.label(t("analytics_student_hits")).classes(
                    "text-sm font-semibold text-gray-600 dark:text-gray-400 mb-2")
                bar_students = ui.echart({
                    "tooltip": {"trigger": "axis",
                                "axisPointer": {"type": "shadow"}},
                    "grid":    {"left": "3%", "right": "4%",
                                "bottom": "3%", "containLabel": True},
                    "xAxis":   {"type": "category", "data": [],
                                "axisLabel": {"color": "#888", "rotate": 30,
                                              "interval": 0}},
                    "yAxis":   {"type": "value",
                                "axisLabel": {"color": "#888"}},
                    "series":  [{"name": t("analytics_detections"), "type": "bar",
                                 "data": [],
                                 "itemStyle": {"color": "#a78bfa"},
                                 "barMaxWidth": 40}],
                }).classes("w-full").style("height:300px")
        else:
            bar_students = None

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh() -> None:
        fd  = f_from.value or ""
        td  = f_to.value   or ""
        # Reject inverted ranges — don't filter, just tell the user.
        if fd and td and fd > td:
            ui.notify(t("analytics_err_date_range"), type="negative")
            return
        cid = int(f_computer.value) if is_teacher and f_computer.value else None
        uid = int(f_student.value)  if is_teacher and f_student.value  else None
        if not is_teacher:
            uid = current["id"]

        stats = get_summary_stats(cid, uid, fd, td)
        card_total["value"].set_text(str(stats["total_events"]))
        card_hits["value"].set_text(str(stats["detection_events"]))
        card_students["value"].set_text(str(stats["active_students"]))
        card_top["value"].set_text(stats["top_class"])

        dist = get_class_distribution(cid, uid, fd, td)
        donut.options["series"][0]["data"] = [
            {"name": d["name"], "value": d["count"],
             "itemStyle": {"color": d["color"]}}
            for d in dist
        ]
        donut.update()

        daily = get_daily_detections(cid, uid, fd, td)
        bar_daily.options["xAxis"]["data"]     = [r["day"]   for r in daily]
        bar_daily.options["series"][0]["data"] = [r["total"] for r in daily]
        bar_daily.options["series"][1]["data"] = [r["hits"]  for r in daily]
        bar_daily.update()

        if is_teacher and bar_students:
            sa = get_student_activity(cid, fd, td)
            bar_students.options["xAxis"]["data"]     = [r["student"] for r in sa]
            bar_students.options["series"][0]["data"] = [r["hits"]    for r in sa]
            bar_students.update()

    _refresh()


# ── Stat card helper ──────────────────────────────────────────────────────────

def _stat_card(label: str, value: str, color: str) -> dict:
    themes = {
        "gray":  ("bg-gray-100  dark:bg-gray-800",
                  "text-gray-700  dark:text-gray-300"),
        "red":   ("bg-red-50    dark:bg-red-950",
                  "text-red-600   dark:text-red-300"),
        "blue":  ("bg-blue-50   dark:bg-blue-950",
                  "text-blue-600  dark:text-blue-300"),
        "green": ("bg-green-50  dark:bg-green-950",
                  "text-green-700 dark:text-green-300"),
    }
    bg, fg = themes.get(color, themes["gray"])
    with ui.card().classes(
        f"flex-1 min-w-36 {bg} border border-gray-200 dark:border-gray-700"
    ):
        ui.label(label).classes(
            "text-xs text-gray-500 dark:text-gray-400")
        val_lbl = ui.label(value).classes(
            f"text-3xl font-bold {fg} mt-1")
    return {"value": val_lbl}
