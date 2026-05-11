"""
build.py
────────
Builds a self-contained single-file distribution of Veyon AI Monitor.

Usage (from repo root, inside the venv):
    python build.py

Output:
    dist/VeyonAIMonitor.exe        ← ship this
    dist/weights/                  ← copy your .onnx model here before shipping
    dist/data/                     ← created automatically (runtime DB/cache)
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path

ROOT     = Path(__file__).parent
DIST_DIR = ROOT / "dist"
EXE      = DIST_DIR / ("VeyonAIMonitor.exe" if sys.platform == "win32" else "VeyonAIMonitor")
APP_NAME = "VeyonAIMonitor"
ENTRY    = "run.py"
ICON     = ROOT / "icon.ico"
SEP      = ";" if sys.platform == "win32" else ":"


def collect_add_data() -> list[str]:
    add_data: list[str] = []

    # app/ source package
    add_data.append(f"app{SEP}app")

    # NiceGUI static frontend files (JS/CSS/fonts bundled with the package)
    try:
        import nicegui
        ng = Path(nicegui.__file__).parent
        for sub in ("templates", "static", "elements", "components"):
            p = ng / sub
            if p.exists():
                add_data.append(f"{p}{SEP}nicegui/{sub}")
    except ImportError:
        print("WARNING: nicegui not found")

    # ultralytics assets (yaml configs, default weights metadata, etc.)
    try:
        import ultralytics
        ul = Path(ultralytics.__file__).parent
        add_data.append(f"{ul}{SEP}ultralytics")
    except ImportError:
        print("WARNING: ultralytics not found")

    # OpenCV data files
    try:
        import cv2
        cv2_data = Path(cv2.__file__).parent / "data"
        if cv2_data.exists():
            add_data.append(f"{cv2_data}{SEP}cv2/data")
    except ImportError:
        pass

    return add_data


def collect_hidden_imports() -> list[str]:
    return [
        # Page modules — imported for @ui.page side-effects, invisible to analysis
        "app.pages.login",
        "app.pages.dashboard",
        "app.pages.history",
        "app.pages.analytics",
        "app.pages.alerts",
        "app.pages.models",
        "app.pages.settings",
        "app.pages.users",
        "app.pages._nav",
        "app.pages._snapshot",
        # Auth / DB
        "bcrypt",
        "bcrypt._bcrypt",
        # NiceGUI internals sometimes missed by static analysis
        "nicegui.air",
        "nicegui.native",
        "nicegui.storage",
        # Torch
        "torch",
        "torch.nn",
        "torch.jit",
        # ONNX
        "onnxruntime",
        "onnxruntime.capi._pybind_state",
        # YAML
        "yaml",
        # Misc
        "engineio.async_drivers.threading",
        "socketio.async_drivers.threading",
        "aiofiles",
    ]


def main() -> None:
    # ── 1. Clean previous build ───────────────────────────────────────────────
    for d in ("build", "__pycache__"):
        shutil.rmtree(ROOT / d, ignore_errors=True)
    if EXE.exists():
        EXE.unlink()

    # ── 2. Assemble PyInstaller command ───────────────────────────────────────
    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name",     APP_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(ROOT / "build"),
        "--noconfirm",
        "--clean",
        "--log-level", "WARN",
    ]

    if ICON.exists():
        cmd += ["--icon", str(ICON)]

    # Modules to exclude (trims ~300 MB from the bundle)
    for exc in (
        "IPython", "jupyter", "notebook",
        "PIL.ImageTk", "tkinter", "wx",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
    ):
        cmd += ["--exclude-module", exc]

    for ad in collect_add_data():
        cmd += ["--add-data", ad]

    for hi in collect_hidden_imports():
        cmd += ["--hidden-import", hi]

    cmd.append(ENTRY)

    # ── 3. Run PyInstaller ────────────────────────────────────────────────────
    print("Building — this takes 3–8 minutes…\n")
    print("Command:\n ", " ".join(f'"{c}"' if " " in c else c for c in cmd), "\n")

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\n❌ Build failed.")
        sys.exit(result.returncode)

    # ── 4. Copy runtime assets alongside the exe ─────────────────────────────
    print("\nCopying runtime assets…")

    weights_src = ROOT / "weights"
    weights_dst = DIST_DIR / "weights"
    if weights_src.exists():
        shutil.copytree(weights_src, weights_dst, dirs_exist_ok=True)
        print(f"  ✓ weights/  ({_size(weights_dst)})")
    else:
        weights_dst.mkdir(parents=True, exist_ok=True)
        print("  ⚠  weights/ not found — copy your .onnx model there before shipping")

    for fname in ("readme.md",):
        src = ROOT / fname
        if src.exists():
            shutil.copy2(src, DIST_DIR / fname)
            print(f"  ✓ {fname}")

    data_dir = DIST_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / ".gitkeep").touch()
    print("  ✓ data/")

    # ── 5. Summary ────────────────────────────────────────────────────────────
    print(f"\n✅  Build complete.")
    print(f"   Exe    : {EXE}")
    print(f"   Size   : {EXE.stat().st_size / 1e6:.0f} MB")
    print(f"\n   Ship the dist\\ folder (exe + weights\\ + data\\).")
    print(f"   Users double-click  VeyonAIMonitor.exe  — no install needed.")


def _size(path: Path) -> str:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    if total > 1_000_000_000:
        return f"{total / 1e9:.1f} GB"
    return f"{total / 1e6:.0f} MB"


if __name__ == "__main__":
    main()
