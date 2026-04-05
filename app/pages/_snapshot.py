"""
app/pages/_snapshot.py
──────────────────────
Consolidated snapshot viewer.
- Zoom-at-Click: centers on the mouse position when zooming in fullscreen.
- Annotation toggle: switches between the raw frame and the bbox-annotated
  version.  The toggle button is hidden when no annotated version is available
  (i.e. the event had no detections).
"""
from __future__ import annotations
from typing import Optional
from nicegui import ui


def make_snapshot_dialogs():
    _src_raw:  list[str]  = [""]   # clean / raw frame  (always set)
    _src_ann:  list[str]  = [""]   # annotated frame     (empty = not available)
    _zoom:     list[bool] = [False]
    _show_ann: list[bool] = [True]

    # ── Fullscreen Dialog ─────────────────────────────────────────────────────
    with ui.dialog().props(
        "maximized persistent transition-show=fade transition-hide=fade"
    ) as fs_dlg:
        with ui.card().classes(
            "absolute-full bg-black p-0 m-0 shadow-none fs-card-wrap"
        ).style("overflow:hidden;"):

            img_wrap = ui.element("div").classes(
                "absolute-full fs-img-wrap"
            ).style(
                "display:flex; align-items:center; "
                "justify-content:center; overflow:hidden;"
            )
            with img_wrap:
                raw_img = ui.element("img").style(
                    "width:100%; height:100%; object-fit:contain; "
                    "cursor:zoom-in; transition: width 0.1s, height 0.1s;"
                )

            with ui.row().classes(
                "absolute-top items-center justify-between px-4 "
                "w-full gap-2 fs-top-bar"
            ).style(
                "height:48px; background:rgba(0,0,0,0.7); "
                "transition:opacity 0.25s; opacity:0; "
                "pointer-events:none; z-index:50;"
            ):
                fs_meta = ui.label("").classes(
                    "text-sm text-gray-300 font-mono truncate flex-1")
                ui.button(icon="close", on_click=fs_dlg.close).props(
                    "flat round dense color=white size=sm")

    # ── Zoom Logic ────────────────────────────────────────────────────────────

    def _toggle_zoom(e) -> None:
        _zoom[0] = not _zoom[0]
        click_x = e.args.get("offsetX", 0)
        click_y = e.args.get("offsetY", 0)

        if _zoom[0]:
            img_wrap.style("overflow:auto; display:block;")
            raw_img.style(
                "width:200%; height:200%; max-width:none; "
                "max-height:none; object-fit:contain; cursor:zoom-out;"
            )
            ui.run_javascript(f"""
                setTimeout(() => {{
                    const wrap = document.querySelector('.fs-img-wrap');
                    if (wrap) {{
                        const targetX = ({click_x} * 2) - (wrap.clientWidth / 2);
                        const targetY = ({click_y} * 2) - (wrap.clientHeight / 2);
                        wrap.scrollTo({{
                            left: targetX,
                            top: targetY,
                            behavior: 'smooth'
                        }});
                    }}
                }}, 50);
            """)
        else:
            img_wrap.style("overflow:hidden; display:flex;")
            raw_img.style(
                "width:100%; height:100%; object-fit:contain; cursor:zoom-in;")

    def _reset_zoom() -> None:
        _zoom[0] = False
        img_wrap.style("overflow:hidden; display:flex;")
        raw_img.style(
            "width:100%; height:100%; object-fit:contain; cursor:zoom-in;")

    raw_img.on("click", _toggle_zoom)

    # ── Preview Dialog ────────────────────────────────────────────────────────
    with ui.dialog() as snap_dlg, \
         ui.card().classes("rounded-lg m-0 p-0 shadow-none").style(
             "max-width:900px; width:95vw; overflow:hidden; "
             "background:#111; border: 1px solid #333;"
         ):

        with ui.row().classes(
            "items-center justify-between px-3 w-full gap-2"
        ).style("flex-shrink:0; height:36px; background:rgba(0,0,0,0.6);"):
            snap_meta = ui.label("").classes(
                "text-xs text-gray-300 font-mono truncate flex-1")

            with ui.row().classes("gap-1 items-center flex-shrink-0"):

                # ── Annotation toggle ─────────────────────────────────────────
                def _toggle_ann() -> None:
                    _show_ann[0] = not _show_ann[0]
                    if _show_ann[0] and _src_ann[0]:
                        snap_image.set_source(_src_ann[0])
                    else:
                        snap_image.set_source(_src_raw[0])
                    ann_btn.props(
                        "color=teal" if _show_ann[0] else "color=gray")

                ann_btn = ui.button(
                    "⊞  Annotations", on_click=_toggle_ann
                ).props("flat dense size=xs color=teal")

                # ── Fullscreen ────────────────────────────────────────────────
                def _open_fs() -> None:
                    if not _src_raw[0]:
                        return
                    _reset_zoom()
                    # Respect current annotation state in fullscreen too
                    fs_src = (
                        _src_ann[0]
                        if _show_ann[0] and _src_ann[0]
                        else _src_raw[0]
                    )
                    raw_img.props(f'src="{fs_src}"')
                    fs_meta.set_text(snap_meta.text)
                    fs_dlg.open()
                    ui.run_javascript("""
                        setTimeout(() => {
                            const card = document.querySelector('.fs-card-wrap');
                            const bar  = document.querySelector('.fs-top-bar');
                            if (!card || !bar) return;
                            let hideTimer;
                            const onMove = () => {
                                bar.style.opacity = '1';
                                bar.style.pointerEvents = 'auto';
                                clearTimeout(hideTimer);
                                hideTimer = setTimeout(() => {
                                    bar.style.opacity = '0';
                                    bar.style.pointerEvents = 'none';
                                }, 2000);
                            };
                            card.addEventListener('mousemove', onMove);
                            onMove();
                        }, 100);
                    """)

                ui.button("⛶  Full screen", on_click=_open_fs).props(
                    "flat dense size=xs color=teal")
                ui.button(icon="close", on_click=snap_dlg.close).props(
                    "flat round dense color=white size=xs")

        snap_image = ui.image("").props("fit=contain").style(
            "width:100%; max-height:68vh; background:#000;")

    # ── Public interface ──────────────────────────────────────────────────────

    def show_snapshot(
        raw_b64:       str,
        meta:          str,
        annotated_b64: Optional[str] = None,
    ) -> None:
        """
        Open the preview dialog.

        Parameters
        ----------
        raw_b64       : Base64 data-URL of the clean (unannotated) frame.
        meta          : Text shown in the dialog header.
        annotated_b64 : Base64 data-URL of the bbox-annotated frame, or None
                        if no detections exist for this event.  When None the
                        Annotations toggle button is hidden.
        """
        _src_raw[0]  = raw_b64
        _src_ann[0]  = annotated_b64 or ""
        _show_ann[0] = True
        snap_meta.set_text(meta)

        # Default to annotated view when available
        snap_image.set_source(annotated_b64 if annotated_b64 else raw_b64)

        # Show/hide the toggle depending on whether an annotated version exists
        ann_btn.set_visibility(bool(annotated_b64))
        if annotated_b64:
            ann_btn.props("color=teal")

        snap_dlg.open()

    return snap_dlg, fs_dlg, show_snapshot