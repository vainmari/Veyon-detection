"""
app/pages/reports.py
────────────────────
Reports page  /reports  (teacher only)

One row per monitoring run (manual or scheduler-triggered). Selecting a run
opens its detection report: summary cards, an alerts section (prohibited
classes, grouped per class, with per-alert screenshot viewing), and
collapsible per-class / per-student / per-computer breakdowns. The report
can be exported as CSV (every individual detection) or PDF (the full
report, print-ready). Reports are computed live from the run's
detection_event rows, so they are always consistent with the History page.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from nicegui import ui

from app.core.auth import require_auth
from app.db.database import (
    get_event_frame_annotated_b64,
    get_event_frame_b64,
    get_run,
    get_run_alerts,
    get_run_class_summary,
    get_run_computer_summary,
    get_run_detections,
    get_run_student_summary,
    list_runs,
    run_short_label,
)
from app.pages._nav import nav
from app.pages._snapshot import make_snapshot_dialogs
from app.services.report_export import build_run_pdf
from app.translate import t

_TS_FMT = "%Y-%m-%d %H:%M:%S"

_STATUS_COLORS = {
    "running":     "green",
    "finished":    "blue",
    "interrupted": "orange",
}


def _duration(started_at: str, ended_at: str | None) -> str:
    """Human-readable run length; live runs measure against now."""
    try:
        start = datetime.strptime(started_at, _TS_FMT)
        end   = datetime.strptime(ended_at, _TS_FMT) if ended_at else datetime.now()
    except (TypeError, ValueError):
        return "—"
    secs = max(0, int((end - start).total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _run_csv(run_id: int) -> bytes:
    """All individual detections of a run as CSV (Excel-friendly UTF-8 BOM)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["detected_at", "computer", "student", "class",
                "confidence", "box_x1", "box_y1", "box_x2", "box_y2"])
    for d in get_run_detections(run_id):
        w.writerow([
            d["detected_at"], d["computer"], d["student"], d["class_name"],
            f"{d['confidence']:.3f}",
            d["box_x1"], d["box_y1"], d["box_x2"], d["box_y2"],
        ])
    return buf.getvalue().encode("utf-8-sig")


@ui.page("/reports")
def page_reports() -> None:
    current = require_auth(required_role="teacher")
    if current is None:
        return
    nav(current)

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(t("reports_title")).classes("text-xl font-bold")
            ui.button(t("reports_refresh"), icon="refresh",
                      on_click=lambda: _load()).props("flat dense")

        cols = [
            {"name": "started_at", "label": t("reports_col_started"),
             "field": "started_at", "sortable": True, "align": "left"},
            {"name": "duration",   "label": t("reports_col_duration"),
             "field": "duration",   "sortable": False, "align": "left"},
            {"name": "trigger",    "label": t("reports_col_trigger"),
             "field": "trigger",    "sortable": True, "align": "left"},
            {"name": "group",      "label": t("reports_col_group"),
             "field": "group",      "sortable": True, "align": "left"},
            {"name": "model",      "label": t("reports_col_model"),
             "field": "model",      "sortable": True, "align": "left"},
            {"name": "computers",  "label": t("reports_col_computers"),
             "field": "computer_count", "sortable": True, "align": "center"},
            {"name": "events",     "label": t("reports_col_events"),
             "field": "total_events",     "sortable": True, "align": "center"},
            {"name": "hits",       "label": t("reports_col_detections"),
             "field": "detection_events", "sortable": True, "align": "center"},
            {"name": "alerts",     "label": t("reports_col_alerts"),
             "field": "alert_count",      "sortable": True, "align": "center"},
            {"name": "status",     "label": t("reports_col_status"),
             "field": "status_label",     "sortable": True, "align": "left"},
            {"name": "actions",    "label": "", "field": "id", "align": "right"},
        ]
        tbl = ui.table(columns=cols, rows=[], row_key="id").classes("w-full")
        tbl.props("dense flat bordered")

        tbl.add_slot("body-cell-status", """
            <q-td :props="props">
                <q-badge :color="props.row._status_color">
                    {{ props.row.status_label }}
                </q-badge>
            </q-td>""")
        tbl.add_slot("body-cell-actions", """
            <q-td :props="props">
                <q-btn flat dense size="sm" color="primary" icon="summarize"
                       :label="props.row._view_label"
                       @click="$parent.$emit('view', props.row)" />
            </q-td>""")

        count_lbl = ui.label("").classes("text-xs text-gray-500")

        # ── Snapshot viewer (shared dialogs, same as the notification bell) ────
        _snap_dlg, _fs_dlg, show_snapshot = make_snapshot_dialogs()

        # ── Report detail dialog ───────────────────────────────────────────────
        # The card has a DEFINITE height; detail_body is a flex child with
        # min-height:0 + overflow-y:auto so the collapsible sections get the
        # remaining space and scroll. (A bare `flex:1` against a max-height-only
        # parent collapses to zero — which hid the whole report body.)
        with ui.dialog() as detail_dlg, \
             ui.card().classes("gap-3").style(
                 "max-width: 900px; width: 92vw; height: 85vh"):
            detail_title = ui.label("").classes("text-lg font-bold")
            detail_meta  = ui.label("").classes(
                "text-xs text-gray-500 dark:text-gray-400 font-mono")
            summary_row  = ui.row().classes("w-full gap-3 flex-wrap")
            detail_body  = ui.column().classes("w-full gap-2").style(
                "flex: 1 1 0; min-height: 0; overflow-y: auto")
            with ui.row().classes("w-full justify-end gap-2"):
                pdf_btn = ui.button(t("reports_pdf"), icon="picture_as_pdf").props(
                    "color=red-7 dense")
                csv_btn = ui.button(t("reports_csv"), icon="download").props(
                    "color=primary dense")
                ui.button(t("reports_close"), on_click=detail_dlg.close).props(
                    "flat dense")

        def _summary_card(label: str, value, color: str) -> None:
            with summary_row:
                with ui.card().classes("px-4 py-2 gap-0 items-center"):
                    ui.label(str(value)).classes(
                        f"text-xl font-bold {color}")
                    ui.label(label).classes(
                        "text-xs text-gray-500 dark:text-gray-400")

        def _breakdown_table(title: str, columns: list[dict],
                             rows: list[dict]) -> None:
            """Collapsible breakdown section (collapsed by default)."""
            with detail_body:
                with ui.expansion(f"{title}  ({len(rows)})", icon="table_chart") \
                        .classes("w-full border rounded"):
                    if not rows:
                        ui.label(t("reports_no_detections")).classes(
                            "text-xs text-gray-500")
                        return
                    sub = ui.table(columns=columns, rows=rows).classes("w-full")
                    sub.props("dense flat bordered hide-bottom" if len(rows) <= 10
                              else "dense flat bordered")

        def _view_alert_shot(alert: dict) -> None:
            raw_b64 = get_event_frame_b64(alert["event_id"])
            if not raw_b64:
                ui.notify(t("reports_no_screenshot"), type="warning")
                return
            ann_b64 = get_event_frame_annotated_b64(alert["event_id"])
            meta = (f"{alert['class_name']}  •  {alert['computer']}"
                    f"  •  {alert['student']}  •  {alert['created_at']}")
            show_snapshot(raw_b64, meta, ann_b64)

        _ALERT_COLS = [
            {"name": "created_at", "label": t("reports_col_time"),
             "field": "created_at", "sortable": True, "align": "left"},
            {"name": "computer",   "label": t("reports_col_computer"),
             "field": "computer",   "sortable": True, "align": "left"},
            {"name": "student",    "label": t("reports_col_student"),
             "field": "student",    "sortable": True, "align": "left"},
            {"name": "shot",       "label": "", "field": "id", "align": "right"},
        ]

        def _alerts_section(run_id: int) -> None:
            """
            Prohibited-class alerts, grouped into one collapsible panel per
            class: when each alert fired, on which computer, which student
            caused it, and a button to view the stored screenshot.
            """
            alerts = get_run_alerts(run_id)
            with detail_body:
                with ui.expansion(
                    f'{t("reports_alerts_section")}  ({len(alerts)})',
                    icon="notification_important", value=bool(alerts),
                ).classes("w-full border rounded"):
                    if not alerts:
                        ui.label(t("reports_alerts_empty")).classes(
                            "text-xs text-gray-500")
                        return
                    by_class: dict[str, list[dict]] = {}
                    for a in alerts:
                        by_class.setdefault(a["class_name"], []).append(a)
                    for cls_name, items in sorted(
                        by_class.items(), key=lambda kv: -len(kv[1])
                    ):
                        students = sorted({a["student"] for a in items})
                        header = (f"{cls_name} — {len(items)} × "
                                  f"({', '.join(students)})")
                        with ui.expansion(header, icon="warning").classes(
                            "w-full"
                        ).style(
                            f"border-left: 4px solid {items[0]['class_color']}"
                        ):
                            atbl = ui.table(
                                columns=_ALERT_COLS, rows=items, row_key="id",
                            ).classes("w-full")
                            atbl.props("dense flat bordered hide-bottom"
                                       if len(items) <= 10
                                       else "dense flat bordered")
                            atbl.add_slot("body-cell-shot", """
                                <q-td :props="props">
                                    <q-btn v-if="props.row.has_frame" flat dense
                                           size="sm" color="blue" icon="image"
                                           @click="$parent.$emit('shot', props.row)" />
                                </q-td>""")
                            atbl.on("shot", lambda e: _view_alert_shot(e.args))

        def _pdf_labels() -> dict[str, str]:
            return {
                "title":              t("reports_detail_title"),
                "run_word":           t("reports_run_word"),
                "generated":          t("reports_generated"),
                "period":             t("reports_period"),
                "trigger":            t("reports_col_trigger"),
                "group":              t("reports_col_group"),
                "model":              t("reports_col_model"),
                "status":             t("reports_col_status"),
                "started_by":         t("reports_lbl_started_by"),
                "all_computers":      t("reports_all_computers"),
                "trigger_manual":     t("reports_trigger_manual"),
                "trigger_schedule":   t("reports_trigger_schedule"),
                "status_running":     t("reports_status_running"),
                "status_finished":    t("reports_status_finished"),
                "status_interrupted": t("reports_status_interrupted"),
                "summary":            t("reports_summary"),
                "sum_events":         t("reports_sum_events"),
                "sum_hits":           t("reports_sum_hits"),
                "sum_alerts":         t("reports_sum_alerts"),
                "sum_students":       t("reports_sum_students"),
                "computers":          t("reports_col_computers"),
                "alerts_section":     t("reports_alerts_section"),
                "time":               t("reports_col_time"),
                "class":              t("reports_col_class"),
                "computer":           t("reports_col_computer"),
                "student":            t("reports_col_student"),
                "by_class":           t("reports_by_class"),
                "count":              t("reports_col_count"),
                "avg_conf":           t("reports_col_avg_conf"),
                "share":              t("reports_col_share"),
                "shots_section":      t("reports_shots_section"),
                "shot_none":          t("reports_shots_none"),
                "by_student":         t("reports_by_student"),
                "frames":             t("reports_col_frames"),
                "detections":         t("reports_col_detections"),
                "classes":            t("reports_col_classes"),
                "by_computer":        t("reports_by_computer"),
            }

        # Export buttons are bound once; they always export the run whose
        # report is currently open (re-binding per open would stack handlers).
        _open_run_id: list[int | None] = [None]

        def _download_csv() -> None:
            rid = _open_run_id[0]
            if rid is not None:
                ui.download(_run_csv(rid), f"monitoring-run-{rid}-detections.csv")

        def _download_pdf() -> None:
            rid = _open_run_id[0]
            if rid is None:
                return
            pdf = build_run_pdf(rid, _pdf_labels())
            if pdf is None:
                ui.notify(t("reports_not_found"), type="warning")
                return
            ui.download(pdf, f"monitoring-run-{rid}-report.pdf")

        csv_btn.on_click(_download_csv)
        pdf_btn.on_click(_download_pdf)

        def _open_report(run_id: int) -> None:
            run = get_run(run_id)
            if not run:
                ui.notify(t("reports_not_found"), type="warning")
                return
            _open_run_id[0] = run_id

            trigger = (
                t("reports_trigger_schedule").format(
                    name=run.get("schedule_name") or "—")
                if run["trigger_type"] == "schedule"
                else t("reports_trigger_manual")
            )
            started_by = run.get("started_by_name")
            meta_parts = [
                f"{run['started_at']} → {run.get('ended_at') or '…'}",
                t("reports_col_duration") + ": " +
                _duration(run["started_at"], run.get("ended_at")),
                trigger,
                run.get("group_name") or t("reports_all_computers"),
                run.get("model_name") or "—",
            ]
            if started_by:
                meta_parts.append(
                    t("reports_started_by").format(name=started_by))

            detail_title.set_text(
                t("reports_detail_title").format(
                    label=run_short_label(run, t("reports_run_word"))))
            detail_meta.set_text("  •  ".join(meta_parts))

            summary_row.clear()
            _summary_card(t("reports_sum_events"),
                          run["total_events"], "text-blue-500")
            _summary_card(t("reports_sum_hits"),
                          run["detection_events"], "text-red-500")
            _summary_card(t("reports_sum_alerts"),
                          run["alert_count"], "text-orange-500")
            _summary_card(t("reports_sum_students"),
                          run["student_count"], "text-green-500")
            _summary_card(t("reports_col_computers"),
                          run["computer_count"], "text-purple-500")

            detail_body.clear()

            _alerts_section(run_id)

            cls_rows = get_run_class_summary(run_id)
            for r in cls_rows:
                r["avg_conf"] = f"{r['avg_conf']:.0%}"
                r["share"]    = f"{r['pct']:.1f}%"
            _breakdown_table(t("reports_by_class"), [
                {"name": "name",     "label": t("reports_col_class"),
                 "field": "name",     "sortable": True, "align": "left"},
                {"name": "cnt",      "label": t("reports_col_count"),
                 "field": "cnt",      "sortable": True, "align": "center"},
                {"name": "share",    "label": t("reports_col_share"),
                 "field": "share",    "sortable": True, "align": "center"},
                {"name": "avg_conf", "label": t("reports_col_avg_conf"),
                 "field": "avg_conf", "sortable": True, "align": "center"},
            ], cls_rows)

            stu_rows = get_run_student_summary(run_id)
            _breakdown_table(t("reports_by_student"), [
                {"name": "student", "label": t("reports_col_student"),
                 "field": "student", "sortable": True, "align": "left"},
                {"name": "frames",  "label": t("reports_col_frames"),
                 "field": "frames",  "sortable": True, "align": "center"},
                {"name": "hits",    "label": t("reports_col_detections"),
                 "field": "hits",    "sortable": True, "align": "center"},
                {"name": "classes", "label": t("reports_col_classes"),
                 "field": "classes", "sortable": False, "align": "left"},
            ], stu_rows)

            comp_rows = get_run_computer_summary(run_id)
            _breakdown_table(t("reports_by_computer"), [
                {"name": "computer", "label": t("reports_col_computer"),
                 "field": "computer", "sortable": True, "align": "left"},
                {"name": "frames",   "label": t("reports_col_frames"),
                 "field": "frames",   "sortable": True, "align": "center"},
                {"name": "hits",     "label": t("reports_col_detections"),
                 "field": "hits",     "sortable": True, "align": "center"},
            ], comp_rows)

            detail_dlg.open()

        def _load() -> None:
            rows = list_runs(limit=200)
            for r in rows:
                r["duration"] = _duration(r["started_at"], r.get("ended_at"))
                r["trigger"] = (
                    t("reports_trigger_schedule").format(
                        name=r.get("schedule_name") or "—")
                    if r["trigger_type"] == "schedule"
                    else t("reports_trigger_manual")
                )
                r["group"]         = r.get("group_name") or t("reports_all_computers")
                r["model"]         = r.get("model_name") or "—"
                r["status_label"]  = t(f"reports_status_{r['status']}")
                r["_status_color"] = _STATUS_COLORS.get(r["status"], "grey")
                r["_view_label"]   = t("reports_view")
            tbl.rows = rows
            tbl.update()
            count_lbl.set_text(t("reports_entries").format(n=len(rows)))
            if not rows:
                count_lbl.set_text(t("reports_empty"))

        tbl.on("view", lambda e: _open_report(int(e.args["id"])))

        _load()
