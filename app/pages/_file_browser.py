"""
app/pages/_file_browser.py
─────────────────────
Generic file browser (server-side, reusable)
"""
from pathlib import Path
from nicegui import ui

def _list_entries(
    p: Path,
    extensions: list[str] | None,
    mode: str,
) -> list[tuple[str, str, Path]]:
    """
    Return sorted (kind, name, path) entries for directory p.
    kind is 'dir' or 'file'. A '..' entry is prepended for non-root paths.
    Files are omitted entirely when mode=='folder'.
    Extension filtering only applies to files.
    PermissionError is silently swallowed.
    """
    entries: list[tuple[str, str, Path]] = []
    if p.parent != p:
        entries.append(("dir", "..", p.parent))
    try:
        for child in sorted(
            p.iterdir(),
            key=lambda x: (not x.is_dir(), x.name.lower()),
        ):
            if child.is_dir():
                entries.append(("dir", child.name, child))
            else:
                if mode == "folder":
                    continue
                if extensions and child.suffix.lower() not in extensions:
                    continue
                entries.append(("file", child.name, child))
    except PermissionError:
        pass
    return entries


def browse_file(
    input_widget,
    title: str | None = None,
    extensions: list[str] | None = None,
    mode: str = "file",  # "file", "folder", or "both"
) -> None:
    """
    Generic file/folder browser dialog.
    mode: "file" (default) - select files only
          "folder"         - select folders only
          "both"           - allow selecting either
    """
    raw = input_widget.value
    start = Path(raw).resolve().parent if raw else Path.cwd()
    state = {"path": start if start.is_dir() else Path.cwd(), "selected": None}

    with ui.dialog() as dlg, ui.card().classes("w-full max-w-2xl"):
        if title is not None:
            ui.label(title).classes("text-base font-semibold mb-2")
        else:
            ui.label(
                f"Browse for {'folders' if mode=='folder' else 'files' if mode=='file' else 'files or folders'}"
            ).classes("text-base font-semibold mb-2")

        path_lbl = ui.label(str(state["path"])).classes(
            "text-xs text-gray-400 font-mono mb-2 break-all"
        )

        listing = ui.column().classes(
            "w-full gap-0 border rounded overflow-y-auto"
        ).style("max-height: 20rem")


        def render(p: Path) -> None:
            state["path"] = p
            state["selected"] = None
            path_lbl.set_text(str(p))
            listing.clear()
            rows: dict[str, ui.element] = {}

            def set_selected(path_str: str) -> None:
                if state["selected"] and state["selected"] in rows:
                    rows[state["selected"]].classes(remove="bg-blue-100 dark:bg-blue-900")
                state["selected"] = path_str
                if path_str in rows:
                    rows[path_str].classes(add="bg-blue-100 dark:bg-blue-900")

            entries = _list_entries(p, extensions, mode)

            with listing:
                for kind, name, ep in entries:
                    icon = "folder" if kind == "dir" else "insert_drive_file"

                    def on_single_click(ep=ep, kind=kind, name=name):
                        if name == "..":
                            return  # only double-click navigates up
                        if kind == "dir" and mode in ("folder", "both"):
                            input_widget.set_value(str(ep))
                            set_selected(str(ep))
                        elif kind == "file" and mode in ("file", "both"):
                            input_widget.set_value(str(ep))
                            set_selected(str(ep))

                    def on_double_click(ep=ep, kind=kind):
                        if kind == "dir":
                            render(ep)
                        elif kind == "file" and mode in ("file", "both"):
                            input_widget.set_value(str(ep))
                            dlg.close()

                    row_el = (
                        ui.row()
                        .classes("w-full items-center gap-2 px-3 py-1 cursor-pointer "
                                 "hover:bg-gray-100 dark:hover:bg-gray-800")
                        .on("click", on_single_click)
                        .on("dblclick", on_double_click)
                    )
                    rows[str(ep)] = row_el
                    with row_el:
                        ui.icon(icon, size="xs").classes(
                            "text-yellow-500" if kind == "dir" else "text-blue-400"
                        )
                        ui.label(name).classes("text-sm font-mono")

        render(state["path"])

        with ui.row().classes("w-full justify-end mt-3"):
            # Add explicit folder select button for folder/both mode
            if mode in ("folder", "both"):
                ui.button("Select this folder", on_click=lambda: (
                    input_widget.set_value(str(state["path"])), dlg.close()
                )).props("color=primary")
            ui.button("Cancel", on_click=dlg.close).props("flat")

    dlg.open()
