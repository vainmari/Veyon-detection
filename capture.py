"""
Veyon WebAPI Monitor — GUI Edition
===================================
Polls student computers via Veyon WebAPI, runs YOLO detection,
and displays annotated screenshots in a three-panel Tkinter interface.

Layout:
    LEFT   — editable settings + Launch / Stop controls
    MIDDLE — colour-coded console log
    RIGHT  — live annotated preview (per-computer dropdown)

Requirements:
    pip install requests ultralytics opencv-python pillow

Setup:
    1. Fill in default paths below (or change them in the GUI at runtime).
    2. Run:  python veyon_monitor_gui.py
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk
from ultralytics import YOLO
import torch


# ─── Constants ────────────────────────────────────────────────────────────────

WEBAPI_BASE_TPL = "http://{host}:{port}/api/v1"
KEY_AUTH_UUID   = "0c69b301-81b4-42d6-8fae-128cdd113314"

# BGR bounding-box colours, one per class index
BOX_COLORS: list[tuple[int, int, int]] = [
    (  0, 255,   0),    # 0  DI               — green
    (255, 128,   0),    # 1  Ekrano nuotraukos — orange
    (  0, 128, 255),    # 2  Narsykle          — blue
    (128,   0, 255),    # 3  Notepad           — purple
    (  0, 255, 255),    # 4  Paint             — yellow
    (255,   0, 128),    # 5  PowerPoint        — pink
    ( 64, 200,  64),    # 6  Word              — light green
]


# ─── YOLO model (process-wide singleton, shared across threads) ───────────────

_model:      Optional[YOLO] = None
_model_lock: threading.Lock = threading.Lock()


def get_model(model_path: str) -> YOLO:
    """Load the YOLO model once; return the cached instance on subsequent calls."""
    global _model
    with _model_lock:
        if _model is None:
            _model = YOLO(model_path)
    return _model


def reset_model() -> None:
    """Force a fresh model load on the next get_model() call (e.g. after path change)."""
    global _model
    with _model_lock:
        _model = None


# ─── Post-processing ───────────────────

def _postprocess_result(
    res,                    # ultralytics.engine.results.Results
    img_bgr: np.ndarray,
    keep_top1: bool,
) -> tuple[np.ndarray, list[dict]]:
    """
    Draw boxes/labels and extract detection list.
    """
    names = res.names

    # Collect raw detections from the result tensor
    raw: list[dict] = [
        {
            "cls_id": int(box.cls[0]),
            "conf":   float(box.conf[0]),
            "xyxy":   list(map(int, box.xyxy[0].tolist())),
        }
        for box in res.boxes
    ]

    # Optionally keep only the highest-confidence box per class
    if keep_top1:
        top: dict[int, dict] = {}
        for b in raw:
            if b["cls_id"] not in top or b["conf"] > top[b["cls_id"]]["conf"]:
                top[b["cls_id"]] = b
        raw = list(top.values())

    annotated  = img_bgr.copy()
    detections: list[dict] = []

    for b in raw:
        cls_id          = b["cls_id"]
        conf_v          = b["conf"]
        x1, y1, x2, y2 = b["xyxy"]
        label           = names.get(cls_id, str(cls_id))
        color           = BOX_COLORS[cls_id % len(BOX_COLORS)]

        # Bounding box
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

        # Label pill above the box
        text        = f"{label} {conf_v:.0%}"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(annotated, (x1, y1 - th - 6), (x1 + tw + 4, y1), color, -1)
        cv2.putText(annotated, text, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)

        detections.append({
            "class_id":   cls_id,
            "class_name": label,
            "conf":       round(conf_v, 3),
            "box":        [x1, y1, x2, y2],
        })

    return annotated, detections


# ─── Veyon WebAPI helpers ─────────────────────────────────────────────────────

def is_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """Return True if something is already listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def launch_webapi_server(veyon_cli: str) -> Optional[subprocess.Popen]:
    """Spawn veyon-cli webapi runserver in a hidden console window."""
    try:
        return subprocess.Popen(
            [veyon_cli, "webapi", "runserver"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return None


def discover_computers(veyon_cli: str) -> list[dict]:
    """
    Read the computer list from Veyon's built-in directory via veyon-cli config.

    Returns:
        [{"name": str, "host": str}, ...]  — only entries with a host address.
    """
    result = subprocess.run(
        [veyon_cli, "config", "get", "BuiltinDirectory/NetworkObjects"],
        capture_output=True, text=True, timeout=10,
    )
    raw = result.stdout.strip()
    if not raw:
        raise RuntimeError(result.stderr.strip() or "veyon-cli returned empty output")
    if "=" in raw:
        raw = raw.split("=", 1)[1].strip()

    objects = json.loads(raw)
    return [
        {"name": o["Name"], "host": o["HostAddress"]}
        for o in objects
        if o.get("Type") == 3 and o.get("HostAddress")
    ]


def decode_image(raw: bytes) -> Optional[np.ndarray]:
    """Decode JPEG/PNG bytes into an OpenCV BGR array."""
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def save_image(
    output_dir: str,
    name:       str,
    img:        np.ndarray,
    suffix:     str,
    fmt:        str,
) -> str:
    """Write img to <output_dir>/<name>/<timestamp>_<suffix>.<ext>."""
    folder = os.path.join(output_dir, name.replace(" ", "_"))
    os.makedirs(folder, exist_ok=True)
    ext  = "jpg" if fmt == "jpeg" else "png"
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(folder, f"{ts}_{suffix}.{ext}")
    cv2.imwrite(path, img)
    return path


# ─── Monitor controller (REAL-TIME OPTIMIZED) ─────────────────────────────────

class MonitorController:
    """
    Orchestrates WebAPI server lifecycle, computer discovery, and per-computer
    worker threads.

    REAL-TIME OPTIMISATIONS:
        • Single dedicated batch-detection thread
        • Persistent HTTP sessions with connection reuse
        • Automatic CUDA / CPU device selection
        • Back-pressure queue (max 128 frames) with safe drop on overload
        • No disk I/O in the hot detection path
        • Short timeouts everywhere for low latency

    All heavy CPU work is isolated to ONE thread → the other worker threads
    stay almost idle (pure I/O).
    """

    def __init__(
        self,
        cfg:   dict,
        log_q: "queue.Queue[str]",
        img_q: "queue.Queue[tuple[str, np.ndarray, list]]",
    ) -> None:
        self.cfg   = cfg
        self.log_q = log_q
        self.img_q = img_q

        self._stop    = threading.Event()
        self._proc:   Optional[subprocess.Popen]  = None
        self._threads: list[threading.Thread]     = []

        # Real-time core: raw images queue + single detection thread
        self._raw_img_queue: queue.Queue[tuple[str, np.ndarray]] = queue.Queue(maxsize=128)
        self._detection_thread: Optional[threading.Thread] = None

    # ── Public interface ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the controller in a background thread."""
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="monitor-main").start()

    def stop(self) -> None:
        """Signal all workers to finish and clean up the WebAPI process."""
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(3)
            except Exception:
                self._proc.kill()
            self._proc = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        """Push a timestamped message onto the log queue."""
        self.log_q.put(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _run(self) -> None:
        """Main orchestration: key load → WebAPI → model → discovery → workers + detection engine."""
        cfg = self.cfg

        # Load the private key from disk
        try:
            with open(cfg["key_path"], "r", encoding="utf-8") as f:
                key_data = f.read().strip()
        except OSError as e:
            self._log(f"❌ Cannot read key file: {e}")
            return

        base_url = WEBAPI_BASE_TPL.format(host=cfg["host"], port=cfg["port"])

        # Optionally auto-start the Veyon WebAPI server
        if cfg["auto_start"]:
            if is_port_open(cfg["host"], cfg["port"]):
                self._log("✅ WebAPI server already running")
            else:
                self._log("🚀 Launching WebAPI server…")
                proc = launch_webapi_server(cfg["veyon_cli"])
                if not proc:
                    self._log("❌ Failed to launch veyon-cli — check the path")
                    return
                self._proc = proc
                self._log(f"   Server PID {proc.pid} — waiting for ready…")
                for _ in range(cfg["start_wait"]):
                    if self._stop.is_set():
                        return
                    if is_port_open(cfg["host"], cfg["port"]):
                        self._log("✅ WebAPI is online")
                        break
                    time.sleep(1)
                else:
                    self._log("⚠️  Server still not responding — check Veyon logs")
        else:
            self._log("Manual mode — ensure the WebAPI server is running externally")

        # Pre-load the YOLO model (warm-up)
        self._log(f"Loading YOLO model: {cfg['model_path']}")
        try:
            get_model(cfg["model_path"])
            self._log("✅ Model ready")
        except Exception as e:
            self._log(f"❌ Model load failed: {e}")
            return

        # Discover computers from Veyon's built-in directory
        self._log("Discovering computers…")
        try:
            computers = discover_computers(cfg["veyon_cli"])
        except Exception as e:
            self._log(f"❌ Discovery failed: {e}")
            return

        if not computers:
            self._log("No computers found — add them in Veyon Configurator → Locations & Computers")
            return

        self._log(f"Found {len(computers)} computer(s):")
        for c in computers:
            self._log(f"  • {c['name']}  ({c['host']})")

        os.makedirs(cfg["output_dir"], exist_ok=True)

        # Spawn one lightweight I/O worker per discovered computer
        self._threads = []
        for c in computers:
            t = threading.Thread(
                target=self._worker,
                args=(c["name"], c["host"], key_data, base_url),
                name=f"worker-{c['host']}",
                daemon=True,
            )
            self._threads.append(t)
            t.start()

        # Start the SINGLE batched detection engine (the real-time heart)
        self._detection_thread = threading.Thread(
            target=self._detection_worker,
            daemon=True,
            name="yolo-batch-detector",
        )
        self._detection_thread.start()
        self._log("🚀 Batched detection engine started (real-time core)")

        self._log(f"Polling every {cfg['interval']}s — press Stop to quit\n")

    # ── Per-computer I/O worker (pure network + decode) ───────────────────────

    def _worker(self, name: str, host: str, key_data: str, base_url: str) -> None:
        """
        One thread per computer – does ONLY network I/O and decode.
        Detection is offloaded → this thread uses almost zero CPU.
        Uses persistent session for maximum throughput.
        """
        cfg = self.cfg
        session = requests.Session()
        session.headers.update({"Connection": "keep-alive"})

        conn_uid: Optional[str] = None

        while not self._stop.is_set():

            # (Re-)authenticate when needed
            if conn_uid is None:
                self._log(f"[{name}] Authenticating…")
                conn_uid = self._authenticate_with_session(session, base_url, host, cfg["key_name"], key_data)
                if conn_uid is None:
                    self._log(f"[{name}] Auth failed — retrying in 10 s")
                    self._stop.wait(10)
                    continue
                time.sleep(2)   # let VNC session stabilise

            # Grab framebuffer (fast, persistent connection)
            raw = self._grab_framebuffer_with_session(
                session, base_url, conn_uid,
                cfg["img_fmt"], cfg["img_quality"], cfg["img_width"],
            )
            if raw is None:
                self._log(f"[{name}] Framebuffer failed — re-authenticating")
                conn_uid = None
                continue

            img = decode_image(raw)
            if img is None:
                self._log(f"[{name}] Image decode error — skipping frame")
                self._stop.wait(cfg["interval"])
                continue

            # Optional raw save
            if cfg["save_raw"]:
                save_image(cfg["output_dir"], name, img, "raw", cfg["img_fmt"])

            # Queue for batched detection (non-blocking with back-pressure)
            try:
                self._raw_img_queue.put((name, img), timeout=1.0)
            except queue.Full:
                self._log(f"[{name}] Detection queue full – dropping frame (backpressure)")

            self._stop.wait(cfg["interval"])

    # ── Session helpers (persistent, low-latency) ─────────────────────────────

    def _authenticate_with_session(
        self, session: requests.Session, base_url: str, host: str, key_name: str, key_data: str
    ) -> Optional[str]:
        """Perform key-based auth using persistent session."""
        try:
            r = session.post(
                f"{base_url}/authentication/{host}",
                json={
                    "method":      KEY_AUTH_UUID,
                    "credentials": {"keyname": key_name, "keydata": key_data},
                },
                timeout=5,
            )
            if r.status_code == 200:
                return r.json().get("connection-uid")
        except requests.RequestException:
            pass
        return None

    def _grab_framebuffer_with_session(
        self,
        session: requests.Session,
        base_url: str,
        conn_uid: str,
        fmt: str,
        quality: int,
        width: int,
    ) -> Optional[bytes]:
        """Request framebuffer using persistent session."""
        params: dict = {"format": fmt, "quality": quality}
        if width:
            params["width"] = width
        try:
            r = session.get(
                f"{base_url}/framebuffer",
                params=params,
                headers={"Connection-Uid": conn_uid},
                timeout=5,
            )
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
        except requests.RequestException:
            pass
        return None

    # ── SINGLE BATCHED DETECTION THREAD ────────

    def _detection_worker(self) -> None:
        """
        Thread for handling YOLO inference.

        Uses GPU if it is available, if not - CPU
        """
        cfg = self.cfg

        device = "cuda" if torch.cuda.is_available() else "cpu"

        MAX_BATCH = 16

        if device == "cuda":
            MAX_BATCH = 32

        self._log(f"🚀 Detection engine using device: {device} | max batch size: {MAX_BATCH}")

        model = get_model(cfg["model_path"])

        while not self._stop.is_set():
            batch_imgs: list[np.ndarray] = []
            batch_names: list[str] = []

            # Collect batch (quick timeout for responsiveness)
            try:
                while len(batch_imgs) < MAX_BATCH:
                    name, img = self._raw_img_queue.get(timeout=0.05)
                    batch_imgs.append(img)
                    batch_names.append(name)
            except queue.Empty:
                if not batch_imgs:
                    continue

            # ── BATCH INFERENCE ──────────────────────────────────────────────
            try:
                t0 = time.perf_counter()
                results = model(
                    batch_imgs,
                    imgsz=cfg["detect_imgsz"],
                    conf=cfg["detect_conf"],
                    iou=cfg["detect_iou"],
                    agnostic_nms=False,
                    verbose=False,
                    device=device,
                    rect=True,
                )
                inference_ms = (time.perf_counter() - t0) * 1000
                avg_ms = inference_ms / len(batch_imgs)

                # Post-process every image in the batch
                for name, res, img_bgr in zip(batch_names, results, batch_imgs):
                    annotated, detections = _postprocess_result(
                        res, img_bgr, cfg["keep_top1"]
                    )

                    # Optional annotated save
                    if cfg["save_annotated"]:
                        save_image(cfg["output_dir"], name, annotated, "det", cfg["img_fmt"])

                    # Push to GUI queue for live preview
                    self.img_q.put((name, annotated, detections))

                    # Console summary
                    if detections:
                        summary = ", ".join(
                            f"{d['class_name']}({d['conf']:.0%})" for d in detections
                        )
                        self._log(f"[{name}] {len(detections)} det(s) in {avg_ms:.0f} ms → {summary}")
                    else:
                        self._log(f"[{name}] No detections ({avg_ms:.0f} ms)")

            except Exception as e:
                self._log(f"❌ Batch detection error: {e}")


# ─── Tkinter GUI ──────────────────────────────────────────────────────────────

class App(tk.Tk):
    """
    Three-panel Tkinter application.

    LEFT   — Scrollable settings + Launch / Stop buttons
    MIDDLE — Dark colour-coded console log
    RIGHT  — Live annotated preview with computer selector
    """

    # Default values pre-filled into every settings widget
    DEFAULTS: dict = {
        "key_name":      "class",
        "key_path":      r"class.pem",
        "veyon_cli":     r"C:\Program Files\Veyon\veyon-cli.exe",
        "host":          "localhost",
        "port":          "11080",
        "auto_start":    True,
        "start_wait":    "10",
        "interval":      "1",
        "img_fmt":       "jpeg",
        "img_quality":   "85",
        "img_width":     "480",
        "model_path":    "ONNX_FP32.onnx",
        "detect_conf":   "0.40",
        "detect_iou":    "0.20",
        "detect_imgsz":  "480",
        "keep_top1":     True,
        "output_dir":    "./veyon_screenshots",
        "save_raw":      False,
        "save_annotated":True,
    }

    def __init__(self) -> None:
        super().__init__()
        self.title("Veyon Monitor")
        self.geometry("1550x860")
        self.minsize(1100, 600)

        # Runtime state
        self._monitor:     Optional[MonitorController]                     = None
        self._log_q:       queue.Queue[str]                                 = queue.Queue()
        self._img_q:       queue.Queue[tuple[str, np.ndarray, list]]        = queue.Queue()
        self._latest_imgs: dict[str, tuple[np.ndarray, list]]              = {}
        self._tk_img:      Optional[ImageTk.PhotoImage]                    = None

        # Tkinter variable map: settings key → tk.Variable
        self._vars: dict[str, tk.Variable] = {}

        self._build_ui()
        self._poll()                                    # kick off the 50 ms UI update loop
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        left  = ttk.Frame(paned, width=310)
        mid   = ttk.Frame(paned)
        right = ttk.Frame(paned, width=540)

        paned.add(left,  weight=0)
        paned.add(mid,   weight=1)
        paned.add(right, weight=1)

        self._build_settings(left)
        self._build_console(mid)
        self._build_preview(right)

    # ── Left panel ────────────────────────────────────────────────────────────

    def _build_settings(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Settings", font=("Segoe UI", 11, "bold")).pack(
            anchor="w", padx=8, pady=(8, 2))

        # Canvas + scrollbar to make the settings list scrollable
        canvas = tk.Canvas(parent, highlightthickness=0, bg=self.cget("bg"))
        vsb    = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right",  fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas)
        win   = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",  lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1 * e.delta / 120), "units"))

        # ── Helper closures ───────────────────────────────────────────────────

        def section(title: str) -> None:
            ttk.Separator(inner, orient="horizontal").pack(fill="x", pady=(10, 2))
            ttk.Label(inner, text=title, font=("Segoe UI", 9, "bold")).pack(
                anchor="w", padx=6)

        def row(
            key:     str,
            label:   str,
            kind:    str            = "entry",
            choices: list[str]|None = None,
            browse:  str|None       = None,
        ) -> None:
            """
            Add one labelled settings row.

            kind   — "entry" | "check" | "combo"
            browse — "file" | "dir"  (adds a … button next to the entry)
            """
            frm = ttk.Frame(inner)
            frm.pack(fill="x", padx=6, pady=2)
            ttk.Label(frm, text=label, width=17, anchor="w").pack(side="left")

            default = self.DEFAULTS.get(key, "")

            if kind == "check":
                var = tk.BooleanVar(value=bool(default))
                ttk.Checkbutton(frm, variable=var).pack(side="left")
            elif kind == "combo":
                var = tk.StringVar(value=str(default))
                ttk.Combobox(frm, textvariable=var, values=choices or [],
                             state="readonly", width=10).pack(side="left", fill="x", expand=True)
            else:
                var = tk.StringVar(value=str(default))
                ttk.Entry(frm, textvariable=var).pack(side="left", fill="x", expand=True)
                if browse:
                    def _pick(b=browse, v=var):
                        path = filedialog.askopenfilename() if b == "file" \
                               else filedialog.askdirectory()
                        if path:
                            v.set(path)
                    ttk.Button(frm, text="…", width=2, command=_pick).pack(side="left")

            self._vars[key] = var

        # ── Settings rows ─────────────────────────────────────────────────────
        section("Authentication")
        row("key_name", "Key name")
        row("key_path", "Key file",     browse="file")

        section("Veyon CLI")
        row("veyon_cli", "veyon-cli",   browse="file")

        section("WebAPI")
        row("host",       "Host")
        row("port",       "Port")
        row("auto_start", "Auto-start", kind="check")
        row("start_wait", "Start wait (s)")

        section("Capture")
        row("interval",    "Interval (s)")
        row("img_fmt",     "Format",      kind="combo", choices=["jpeg", "png"])
        row("img_quality", "Quality")
        row("img_width",   "Width (px)")

        section("YOLO Model")
        row("model_path",   "Model file",    browse="file")
        row("detect_conf",  "Confidence")
        row("detect_iou",   "IoU")
        row("detect_imgsz", "imgsz")
        row("keep_top1",    "Top-1/class",   kind="check")

        section("Output")
        row("output_dir",     "Save dir",        browse="dir")
        row("save_raw",       "Save raw",         kind="check")
        row("save_annotated", "Save annotated",   kind="check")

        ttk.Separator(inner, orient="horizontal").pack(fill="x", pady=10)

        # Launch / Stop buttons
        btn_row = ttk.Frame(inner)
        btn_row.pack(fill="x", padx=6, pady=4)
        self._btn_launch = ttk.Button(btn_row, text="▶  Launch", command=self._on_launch)
        self._btn_launch.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._btn_stop = ttk.Button(btn_row, text="■  Stop", command=self._on_stop,
                                    state="disabled")
        self._btn_stop.pack(side="left", expand=True, fill="x")

        # Small status label
        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(inner, textvariable=self._status_var,
                  foreground="gray", font=("Segoe UI", 8)).pack(
            anchor="w", padx=6, pady=(4, 10))

    # ── Middle panel — console ────────────────────────────────────────────────

    def _build_console(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(header, text="Console", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(header, text="Clear", command=self._clear_log).pack(side="right")

        self._console = tk.Text(
            parent,
            state="disabled",
            wrap="word",
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            relief="flat",
            borderwidth=0,
        )
        vsb = ttk.Scrollbar(parent, orient="vertical", command=self._console.yview)
        self._console.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._console.pack(fill="both", expand=True, padx=(8, 0), pady=(0, 8))

        # Colour tags for different message types
        self._console.tag_config("ok",   foreground="#4ec9b0")   # teal   — success
        self._console.tag_config("err",  foreground="#f44747")   # red    — error
        self._console.tag_config("warn", foreground="#dcdcaa")   # yellow — warning
        self._console.tag_config("info", foreground="#9cdcfe")   # blue   — informational

    def _append_log(self, msg: str) -> None:
        """Colour-code and append a message to the console widget."""
        low = msg.lower()
        if any(k in low for k in ("✅", "loaded", "online", "ready", "model ready", "🚀")):
            tag = "ok"
        elif any(k in low for k in ("❌", "error", "failed", "cannot")):
            tag = "err"
        elif any(k in low for k in ("⚠️", "warn", "timeout", "retry", "full")):
            tag = "warn"
        else:
            tag = "info"

        self._console.configure(state="normal")
        self._console.insert(tk.END, msg + "\n", tag)
        self._console.configure(state="disabled")
        self._console.see(tk.END)

    def _clear_log(self) -> None:
        self._console.configure(state="normal")
        self._console.delete("1.0", tk.END)
        self._console.configure(state="disabled")

    # ── Right panel — live preview ────────────────────────────────────────────

    def _build_preview(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Live Preview", font=("Segoe UI", 11, "bold")).pack(
            anchor="w", padx=8, pady=(8, 2))

        # Computer selector
        sel = ttk.Frame(parent)
        sel.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(sel, text="Computer:").pack(side="left")
        self._pc_var   = tk.StringVar()
        self._pc_combo = ttk.Combobox(sel, textvariable=self._pc_var,
                                      state="readonly", width=26)
        self._pc_combo.pack(side="left", padx=6)
        self._pc_combo.bind("<<ComboboxSelected>>", self._on_pc_selected)

        # Image canvas — fills remaining vertical space
        self._img_label = ttk.Label(parent, background="#1e1e1e", anchor="center")
        self._img_label.pack(fill="both", expand=True, padx=8)

        # Detection summary below the image
        self._det_text = tk.Text(
            parent,
            height=4,
            state="disabled",
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#9cdcfe",
            relief="flat",
            borderwidth=0,
        )
        self._det_text.pack(fill="x", padx=8, pady=(4, 8))

    def _update_preview(self, name: str, img: np.ndarray, dets: list) -> None:
        """Resize and render an annotated frame in the right panel."""
        # Keep the dropdown list up to date
        known = list(self._pc_combo["values"])
        if name not in known:
            known.append(name)
            self._pc_combo["values"] = known
        if not self._pc_var.get():
            self._pc_var.set(name)

        # Only render if this computer is currently selected
        if self._pc_var.get() != name:
            return

        # BGR → RGB → PIL → scale to fit the label widget
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        lw  = max(self._img_label.winfo_width(),  480)
        lh  = max(self._img_label.winfo_height(), 320)
        pil.thumbnail((lw, lh), Image.LANCZOS)

        self._tk_img = ImageTk.PhotoImage(pil)          # must keep a reference!
        self._img_label.configure(image=self._tk_img)

        # Update detection summary text
        self._det_text.configure(state="normal")
        self._det_text.delete("1.0", tk.END)
        if dets:
            for d in dets:
                self._det_text.insert(
                    tk.END,
                    f"  [{d['class_name']}]  conf={d['conf']:.0%}  "
                    f"box=({d['box'][0]},{d['box'][1]})→({d['box'][2]},{d['box'][3]})\n",
                )
        else:
            self._det_text.insert(tk.END, "  — no detections —")
        self._det_text.configure(state="disabled")

    def _on_pc_selected(self, _event=None) -> None:
        """Immediately display the cached frame for the newly selected computer."""
        name = self._pc_var.get()
        if name and name in self._latest_imgs:
            img, dets = self._latest_imgs[name]
            self._update_preview(name, img, dets)

    # ── Button handlers ───────────────────────────────────────────────────────

    def _collect_config(self) -> dict:
        """Read all settings widgets into a typed config dict."""
        v = self._vars
        return {
            "key_name":      v["key_name"].get(),
            "key_path":      v["key_path"].get(),
            "veyon_cli":     v["veyon_cli"].get(),
            "host":          v["host"].get(),
            "port":          int(v["port"].get()),
            "auto_start":    bool(v["auto_start"].get()),
            "start_wait":    int(v["start_wait"].get()),
            "interval":      float(v["interval"].get()),
            "img_fmt":       v["img_fmt"].get(),
            "img_quality":   int(v["img_quality"].get()),
            "img_width":     int(v["img_width"].get()),
            "model_path":    v["model_path"].get(),
            "detect_conf":   float(v["detect_conf"].get()),
            "detect_iou":    float(v["detect_iou"].get()),
            "detect_imgsz":  int(v["detect_imgsz"].get()),
            "keep_top1":     bool(v["keep_top1"].get()),
            "output_dir":    v["output_dir"].get(),
            "save_raw":      bool(v["save_raw"].get()),
            "save_annotated":bool(v["save_annotated"].get()),
        }

    def _on_launch(self) -> None:
        if self._monitor:
            return  # already running

        try:
            cfg = self._collect_config()
        except (ValueError, KeyError) as e:
            messagebox.showerror("Configuration error", str(e))
            return

        reset_model()   # ensures fresh model load if path was changed
        self._monitor = MonitorController(cfg, self._log_q, self._img_q)
        self._monitor.start()

        self._btn_launch.configure(state="disabled")
        self._btn_stop.configure(state="normal")
        self._status_var.set("Running…")

    def _on_stop(self) -> None:
        if self._monitor:
            self._monitor.stop()
            self._monitor = None

        self._btn_launch.configure(state="normal")
        self._btn_stop.configure(state="disabled")
        self._status_var.set("Stopped")

    def _on_close(self) -> None:
        self._on_stop()
        self.destroy()

    # ── UI update loop ────────────────────────────────────────────────────────

    def _poll(self) -> None:
        """
        Drain log and image queues every 50 ms on the main thread.
        Using after() instead of thread-to-widget calls keeps Tkinter thread-safe.
        """
        # Flush all pending log messages
        try:
            while True:
                self._append_log(self._log_q.get_nowait())
        except queue.Empty:
            pass

        # Flush all pending image frames (keep only the latest per computer)
        try:
            while True:
                name, img, dets = self._img_q.get_nowait()
                self._latest_imgs[name] = (img, dets)
                self._update_preview(name, img, dets)
        except queue.Empty:
            pass

        self.after(50, self._poll)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()