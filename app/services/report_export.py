"""
app/services/report_export.py
─────────────────────────────
PDF export of a monitoring-run report (fpdf2).

UI-independent: the caller supplies a `labels` dict (built with t() in the
page handler) so the PDF comes out in the viewer's UI language and this
module stays testable without a NiceGUI session.

Fonts: DejaVu Sans is loaded from matplotlib's bundled font directory
(matplotlib is already a transitive dependency via ultralytics), giving full
Lithuanian glyph coverage. If it cannot be found, fpdf2's built-in Helvetica
is used and non-latin-1 characters degrade to '?'.
"""
from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path
from typing import Optional

from fpdf import FPDF

from app.db.database import (
    get_event_frame_annotated_b64,
    get_event_frame_b64,
    get_run,
    get_run_alerts,
    get_run_class_summary,
    get_run_computer_summary,
    get_run_student_summary,
    run_short_label,
)


def _b64_to_jpeg(data_url: Optional[str]) -> Optional[bytes]:
    """Strip a `data:image/...;base64,` prefix and decode to raw bytes."""
    if not data_url:
        return None
    try:
        return base64.b64decode(data_url.split(",", 1)[1])
    except (IndexError, ValueError):
        return None

_FONT = "helvetica"          # replaced by DejaVu when available
_unicode_ok = False


def _find_dejavu() -> Optional[tuple[Path, Path]]:
    """Return (regular, bold) DejaVu Sans paths, or None."""
    candidates: list[Path] = []
    try:
        import matplotlib
        candidates.append(Path(matplotlib.get_data_path()) / "fonts" / "ttf")
    except Exception:
        pass
    candidates += [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("C:/Windows/Fonts"),
    ]
    for base in candidates:
        reg  = base / "DejaVuSans.ttf"
        bold = base / "DejaVuSans-Bold.ttf"
        if reg.exists() and bold.exists():
            return reg, bold
    return None


def _txt(s: object) -> str:
    """Make a value safe for the current font."""
    s = "" if s is None else str(s)
    if _unicode_ok:
        return s
    return s.encode("latin-1", "replace").decode("latin-1")


class _ReportPDF(FPDF):
    def __init__(self) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        global _FONT, _unicode_ok
        fonts = _find_dejavu()
        if fonts:
            self.add_font("DejaVu", "",  str(fonts[0]))
            self.add_font("DejaVu", "B", str(fonts[1]))
            _FONT, _unicode_ok = "DejaVu", True
        else:
            _FONT, _unicode_ok = "helvetica", False
        self.set_auto_page_break(auto=True, margin=15)

    def section(self, title: str) -> None:
        self.ln(3)
        self.set_font(_FONT, "B", 11)
        self.cell(0, 7, _txt(title), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def table(self, headers: list[str], rows: list[list],
              widths: list[int]) -> None:
        # Headers one step smaller than body — the longest localized header
        # ("Frames with detections" / "Kadrai su aptikimais") must fit 38 mm.
        self.set_font(_FONT, "B", 7)
        self.set_fill_color(235, 235, 235)
        for h, w in zip(headers, widths):
            self.cell(w, 6, _txt(h), border=1, fill=True)
        self.ln()
        self.set_font(_FONT, "", 8)
        for row in rows:
            for v, w in zip(row, widths):
                self.cell(w, 6, _txt(v), border=1)
            self.ln()
        if not rows:
            self.set_font(_FONT, "", 8)
            self.cell(sum(widths), 6, "—", border=1, align="C")
            self.ln()


def build_run_pdf(run_id: int, labels: dict[str, str]) -> Optional[bytes]:
    """
    Render the full report of `run_id` as PDF bytes, or None if the run
    does not exist. `labels` keys mirror the reports page translation keys.
    """
    run = get_run(run_id)
    if not run:
        return None

    pdf = _ReportPDF()
    pdf.add_page()

    # ── Header ────────────────────────────────────────────────────────────────
    title_label = run_short_label(run, labels.get("run_word", "Run"))
    pdf.set_font(_FONT, "B", 15)
    pdf.cell(0, 9, _txt(labels["title"].format(label=title_label)),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(_FONT, "", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, _txt(labels["generated"].format(
        ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))),
        new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # ── Meta ──────────────────────────────────────────────────────────────────
    trigger_label = (
        labels["trigger_schedule"].format(name=run.get("schedule_name") or "—")
        if run["trigger_type"] == "schedule" else labels["trigger_manual"]
    )
    status_label = labels.get("status_" + run["status"], run["status"])
    pdf.set_font(_FONT, "", 9)
    meta = [
        (labels["period"],   f"{run['started_at']}  →  {run.get('ended_at') or '…'}"),
        (labels["trigger"],  trigger_label),
        (labels["group"],    run.get("group_name") or labels["all_computers"]),
        (labels["model"],    run.get("model_name") or "—"),
        (labels["status"],   status_label),
    ]
    if run.get("started_by_name"):
        meta.append((labels["started_by"], run["started_by_name"]))
    for k, v in meta:
        pdf.set_font(_FONT, "B", 9)
        pdf.cell(40, 6, _txt(k))
        pdf.set_font(_FONT, "", 9)
        pdf.cell(0, 6, _txt(v), new_x="LMARGIN", new_y="NEXT")

    # ── Summary ───────────────────────────────────────────────────────────────
    pdf.section(labels["summary"])
    pdf.table(
        [labels["sum_events"], labels["sum_hits"], labels["sum_alerts"],
         labels["sum_students"], labels["computers"]],
        [[run["total_events"], run["detection_events"], run["alert_count"],
          run["student_count"], run["computer_count"]]],
        [38, 38, 38, 38, 38],
    )

    # ── Alerts (prohibited classes) ───────────────────────────────────────────
    alerts = get_run_alerts(run_id)
    pdf.section(labels["alerts_section"] + f"  ({len(alerts)})")
    pdf.table(
        [labels["time"], labels["class"], labels["computer"], labels["student"]],
        [[a["created_at"], a["class_name"], a["computer"], a["student"]]
         for a in alerts],
        [45, 45, 45, 55],
    )

    # ── Breakdown tables ──────────────────────────────────────────────────────
    pdf.section(labels["by_class"])
    pdf.table(
        [labels["class"], labels["count"], labels["share"], labels["avg_conf"]],
        [[r["name"], r["cnt"], f"{r['pct']:.1f}%", f"{r['avg_conf']:.0%}"]
         for r in get_run_class_summary(run_id)],
        [70, 30, 45, 45],
    )

    pdf.section(labels["by_student"])
    pdf.table(
        [labels["student"], labels["frames"], labels["detections"], labels["classes"]],
        [[r["student"], r["frames"], r["hits"], r["classes"]]
         for r in get_run_student_summary(run_id)],
        [50, 25, 30, 85],
    )

    pdf.section(labels["by_computer"])
    pdf.table(
        [labels["computer"], labels["frames"], labels["detections"]],
        [[r["computer"], r["frames"], r["hits"]]
         for r in get_run_computer_summary(run_id)],
        [70, 60, 60],
    )

    # ── Alert screenshots (from page 2 onward) ────────────────────────────────
    # Keep page 1 compact (tables only); evidence images for each fired alert
    # go on dedicated pages so a teacher can verify legit vs. false positive.
    shots = [a for a in alerts if a["has_frame"]]
    if shots:
        pdf.add_page()
        pdf.set_font(_FONT, "B", 13)
        pdf.cell(0, 9, _txt(labels["shots_section"] + f"  ({len(shots)})"),
                 new_x="LMARGIN", new_y="NEXT")
        for a in shots:
            jpeg = (_b64_to_jpeg(get_event_frame_annotated_b64(a["event_id"]))
                    or _b64_to_jpeg(get_event_frame_b64(a["event_id"])))
            if not jpeg:
                continue
            _render_shot(pdf, a, jpeg)
    elif alerts:
        pdf.add_page()
        pdf.set_font(_FONT, "B", 13)
        pdf.cell(0, 9, _txt(labels["shots_section"]),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(_FONT, "", 9)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 6, _txt(labels["shot_none"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())


# Usable content width on A4 portrait with default 10 mm margins.
_CONTENT_W = 190
_SHOT_W    = 150   # screenshot render width (mm); height scales with aspect
_SHOT_MAX_H = 110  # cap so a tall frame can't overflow the page


def _render_shot(pdf: "_ReportPDF", alert: dict, jpeg: bytes) -> None:
    """One alert caption + its (annotated) screenshot, kept on one page."""
    from PIL import Image  # Pillow is already a project dependency

    img = Image.open(io.BytesIO(jpeg))
    w, h = img.size
    draw_w = _SHOT_W
    draw_h = draw_w * h / w if w else _SHOT_W
    if draw_h > _SHOT_MAX_H:
        draw_h = _SHOT_MAX_H
        draw_w = draw_h * w / h if h else _SHOT_W

    caption = (f"{alert['class_name']}  •  {alert['computer']}  •  "
               f"{alert['student']}  •  {alert['created_at']}")
    # Reserve caption + image height; start a fresh page if it won't fit.
    needed = 8 + draw_h + 4
    if pdf.get_y() + needed > pdf.h - pdf.b_margin:
        pdf.add_page()

    pdf.ln(3)
    pdf.set_font(_FONT, "B", 9)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(_CONTENT_W, 7, _txt(caption), border=0, fill=True,
             new_x="LMARGIN", new_y="NEXT")
    pdf.image(io.BytesIO(jpeg), w=draw_w, h=draw_h)
