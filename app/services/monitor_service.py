"""
app/services/monitor_service.py
────────────────────────────────
MonitorController  — orchestrates per-computer I/O threads + single YOLO thread.
drain_worker       — background thread that drains queues into global state + DB.
"""
from __future__ import annotations
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import torch

import app.state as state
from app.core import veyon, yolo
from app.core.imaging import postprocess, img_to_b64, save_image
from app.core.veyon import WEBAPI_BASE_TPL
from app.db.database import insert_detection


class MonitorController:

    def __init__(self, cfg: dict) -> None:
        self.cfg    = cfg
        self._stop  = threading.Event()
        self._proc: Optional[object] = None
        self._raw_q: queue.Queue = queue.Queue(maxsize=128)

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="monitor-main").start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:   self._proc.terminate(); self._proc.wait(3)
            except Exception: self._proc.kill()
            self._proc = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        state.log_q.put(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    # ── Orchestration ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        cfg = self.cfg

        try:
            key_data = Path(cfg["key_path"]).read_text(encoding="utf-8").strip()
        except OSError as e:
            self._log(f"❌ Cannot read key file: {e}"); return

        base_url = WEBAPI_BASE_TPL.format(host=cfg["host"], port=cfg["port"])

        # Optionally auto-start the Veyon WebAPI server
        if cfg["auto_start"]:
            if veyon.is_port_open(cfg["host"], cfg["port"]):
                self._log("✅ WebAPI already running")
            else:
                self._log("🚀 Launching Veyon WebAPI server…")
                proc = veyon.launch_webapi_server(cfg["veyon_cli"])
                if not proc:
                    self._log("❌ Failed to launch veyon-cli — check path in Settings")
                    return
                self._proc = proc
                for _ in range(cfg["start_wait"]):
                    if self._stop.is_set(): return
                    if veyon.is_port_open(cfg["host"], cfg["port"]):
                        self._log("✅ WebAPI online"); break
                    time.sleep(1)
                else:
                    self._log("⚠️  WebAPI not responding — check Veyon logs")

        # Pre-load the YOLO model
        self._log(f"Loading model: {cfg['model_path']}")
        try:
            yolo.get_model(cfg["model_path"])
            self._log("✅ Model ready")
        except Exception as e:
            self._log(f"❌ Model load failed: {e}"); return

        # Discover computers
        self._log("Discovering computers…")
        try:
            computers = veyon.discover_computers(cfg["veyon_cli"])
        except Exception as e:
            self._log(f"❌ Discovery failed: {e}"); return

        if not computers:
            self._log("No computers found — add them in Veyon Configurator"); return

        self._log(f"Found {len(computers)} computer(s):")
        for c in computers:
            self._log(f"  • {c['name']}  ({c['host']})")

        import os; os.makedirs(cfg["output_dir"], exist_ok=True)

        # One lightweight I/O thread per computer
        for c in computers:
            threading.Thread(
                target=self._io_worker,
                args=(c["name"], c["host"], key_data, base_url),
                daemon=True, name=f"io-{c['host']}",
            ).start()

        # Single batched YOLO thread
        threading.Thread(
            target=self._detect_worker, daemon=True, name="yolo"
        ).start()
        self._log(f"🚀 Monitoring started  (interval={cfg['interval']} s)")

    # ── Per-computer I/O worker ───────────────────────────────────────────────

    def _io_worker(self, name: str, host: str, key_data: str, base_url: str) -> None:
        cfg     = self.cfg
        session = requests.Session()
        session.headers["Connection"] = "keep-alive"
        conn_uid: Optional[str] = None

        while not self._stop.is_set():

            # (Re-)authenticate
            if conn_uid is None:
                conn_uid = veyon.authenticate(
                    session, base_url, host, cfg["key_name"], key_data
                )
                if conn_uid is None:
                    self._log(f"[{name}] Auth error — retry in 10 s")
                    self._stop.wait(10); continue
                time.sleep(2)

            raw = veyon.grab_framebuffer(
                session, base_url, conn_uid,
                cfg["img_fmt"], cfg["img_quality"], cfg["img_width"],
            )
            if raw is None:
                self._log(f"[{name}] Framebuffer failed — re-authenticating")
                conn_uid = None; continue

            img = veyon.decode_image(raw)
            if img is None:
                self._stop.wait(float(cfg["interval"])); continue

            if cfg["save_raw"]:
                save_image(cfg["output_dir"], name, img, "raw", cfg["img_fmt"])

            try:
                self._raw_q.put((name, img), timeout=1.0)
            except queue.Full:
                pass   # back-pressure: silently drop frame

            self._stop.wait(float(cfg["interval"]))

    # ── Single batched YOLO detection thread ──────────────────────────────────

    def _detect_worker(self) -> None:
        cfg    = self.cfg
        device = "cuda" if torch.cuda.is_available() else "cpu"
        max_b  = 32 if device == "cuda" else 16
        self._log(f"Detection engine: {device}  |  max_batch={max_b}")
        model = yolo.get_model(cfg["model_path"])

        while not self._stop.is_set():
            imgs:  list = []
            names: list = []
            try:
                while len(imgs) < max_b:
                    n, img = self._raw_q.get(timeout=0.05)
                    imgs.append(img); names.append(n)
            except queue.Empty:
                if not imgs: continue

            try:
                results = model(
                    imgs,
                    imgsz=cfg["detect_imgsz"],
                    conf=float(cfg["detect_conf"]),
                    iou=float(cfg["detect_iou"]),
                    verbose=False, device=device, rect=True,
                )
                for name, res, img_bgr in zip(names, results, imgs):
                    annotated, dets = postprocess(res, img_bgr, bool(cfg["keep_top1"]))
                    if cfg["save_annotated"]:
                        save_image(cfg["output_dir"], name, annotated, "det", cfg["img_fmt"])
                    state.img_q.put((name, annotated, dets))
                    if dets:
                        self._log(
                            f"[{name}] " +
                            ", ".join(f"{d['class_name']}({d['conf']:.0%})" for d in dets)
                        )
            except Exception as e:
                self._log(f"❌ Detection error: {e}")


# ─── Background drain worker ──────────────────────────────────────────────────

def drain_worker() -> None:
    """
    Runs forever in a daemon thread (started once in app startup).
    Drains log_q and img_q → updates state globals + writes to DB.
    UI timers only READ state → no queue contention between browser tabs.
    """
    while True:
        # Drain log messages
        try:
            while True:
                msg = state.log_q.get_nowait()
                state.log_buffer.append(msg)
                if len(state.log_buffer) > state.LOG_CAP:
                    state.log_buffer.pop(0)
        except queue.Empty:
            pass

        # Drain detection results
        try:
            while True:
                name, img_bgr, dets = state.img_q.get_nowait()
                for d in dets:
                    insert_detection(name, d)
                state.latest_frames[name] = (img_to_b64(img_bgr), dets)
        except queue.Empty:
            pass

        time.sleep(0.05)