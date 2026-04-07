"""
app/services/monitor_service.py
────────────────────────────────
MonitorController  — per-computer I/O threads + single YOLO batch thread.
drain_worker       — drains queues into global state (no DB writes here).

DB writes happen inside the detect worker so each frame → event is atomic.
Frames are stored as JPEG BLOBs directly in the database (no disk I/O).

Queue discipline
────────────────
  _raw_q  (maxsize=128) — back-pressure on I/O threads; oldest work dropped
                          when full via put(timeout=1.0).
  state.img_q (maxsize=64) — bounded; detect worker uses put_nowait and drops
                              frames silently if drain_worker falls behind.
                              This prevents unbounded memory growth under load.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Optional

import cv2
import requests
import torch

import app.state as state
from app.core import veyon, yolo
from app.core.imaging import postprocess, img_to_b64
from app.core.veyon import WEBAPI_BASE_TPL
from app.services import alert_service
from app.db.database import (
    get_active_model,
    get_user_by_id,
    get_user_by_username,
    insert_event,
    upsert_computer,
)


def _parse_os_username(raw: str) -> str:
    """Strip domain/host prefix: 'DOMAIN\\Jonas' → 'Jonas'."""
    return raw.split("\\")[-1].strip()


class MonitorController:

    def __init__(
        self,
        cfg:       dict,
        computers: Optional[list[dict]] = None,
    ) -> None:
        """
        cfg       — settings dict from collect_cfg()
        computers — optional pre-filtered list of {"name": str, "host": str}.
                    When None the controller discovers all computers from Veyon.
        """
        self.cfg       = cfg
        self._computers = computers   # None = discover; list = use as-is
        self._stop = threading.Event()
        self._proc: Optional[object] = None
        # raw_q carries (computer_name, raw_jpeg_bytes) decoded in detect worker
        self._raw_q: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=128)

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="monitor-main").start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        state.log_q.put(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    # ── Orchestration ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        cfg = self.cfg

        # Validate auth config up-front
        if cfg.get("auth_method", "key") == "key" and not cfg.get("key_data"):
            self._log("❌ Key file missing or empty — check Settings")
            return
        if cfg.get("auth_method") == "logon" and not cfg.get("logon_username"):
            self._log("❌ Logon username is empty — check Settings")
            return

        base_url = WEBAPI_BASE_TPL.format(host=cfg["host"], port=cfg["port"])

        if cfg["auto_start"]:
            if veyon.is_port_open(cfg["host"], cfg["port"]):
                self._log("✅ WebAPI already running")
            else:
                self._log("🚀 Launching Veyon WebAPI server…")
                proc = veyon.launch_webapi_server(cfg["veyon_cli"])
                if not proc:
                    self._log(
                        "❌ Failed to launch veyon-cli — check path in Settings")
                    return
                self._proc = proc
                for _ in range(cfg["start_wait"]):
                    if self._stop.is_set():
                        return
                    if veyon.is_port_open(cfg["host"], cfg["port"]):
                        self._log("✅ WebAPI online")
                        break
                    time.sleep(1)
                else:
                    self._log("⚠️  WebAPI not responding — check Veyon logs")

        self._log(f"Loading model: {cfg['model_path']}")
        try:
            yolo.get_model(cfg["model_path"])
            self._log("✅ Model ready")
        except Exception as e:
            self._log(f"❌ Model load failed: {e}")
            return

        if self._computers is not None:
            computers = self._computers
            self._log(f"Using {len(computers)} pre-selected computer(s)")
        else:
            self._log("Discovering computers…")
            try:
                computers = veyon.discover_computers(cfg["veyon_cli"])
            except Exception as e:
                self._log(f"❌ Discovery failed: {e}")
                return

        if not computers:
            self._log("No computers found — add them in Veyon Configurator")
            return

        self._log(f"Found {len(computers)} computer(s):")
        for c in computers:
            self._log(f"  • {c['name']}  ({c['host']})")
            cid = upsert_computer(c["name"], c["host"])
            state.computer_ids[c["name"]] = cid

        for c in computers:
            threading.Thread(
                target=self._io_worker,
                args=(c["name"], c["host"], base_url),
                daemon=True, name=f"io-{c['host']}",
            ).start()

        threading.Thread(
            target=self._detect_worker, daemon=True, name="yolo"
        ).start()
        self._log(f"🚀 Monitoring started  (interval={cfg['interval']} s)")

    # ── Per-computer I/O worker ───────────────────────────────────────────────

    def _io_worker(self, name: str, host: str, base_url: str) -> None:
        """
        Pure network thread: authenticate, grab framebuffer, fetch logged-in
        OS username, push raw JPEG bytes onto _raw_q.
        No decoding or inference happens here.
        """
        cfg      = self.cfg
        session  = requests.Session()
        session.headers["Connection"] = "keep-alive"
        conn_uid: Optional[str] = None

        while not self._stop.is_set():

            # (Re-)authenticate
            if conn_uid is None:
                conn_uid = veyon.authenticate(session, base_url, host, cfg)
                if conn_uid is None:
                    self._log(f"[{name}] Auth error — retry in 10 s")
                    self._stop.wait(10)
                    continue
                time.sleep(2)

            raw = veyon.grab_framebuffer(
                session, base_url, conn_uid,
                cfg["img_fmt"], cfg["img_quality"], cfg["img_width"],
            )
            if raw is None:
                self._log(f"[{name}] Framebuffer failed — re-authenticating")
                conn_uid = None
                continue

            os_login_raw = veyon.get_logged_user(session, base_url, conn_uid)
            if os_login_raw:
                os_login = _parse_os_username(os_login_raw)
                db_user  = get_user_by_username(os_login)
                state.computer_users[name]        = db_user["id"] if db_user else None
                state.computer_os_usernames[name] = os_login
                if not db_user:
                    self._log(
                        f"[{name}] OS user '{os_login}' — "
                        "no system account yet (events logged with username)"
                    )
            else:
                state.computer_users[name]        = None
                state.computer_os_usernames[name] = None

            try:
                self._raw_q.put((name, raw), timeout=1.0)
            except queue.Full:
                pass  # back-pressure: drop frame

            self._stop.wait(float(cfg["interval"]))

    # ── Single batched YOLO detection thread ──────────────────────────────────

    def _detect_worker(self) -> None:
        cfg    = self.cfg
        device = "cuda" if torch.cuda.is_available() else "cpu"
        max_b  = 32 if device == "cuda" else 16
        self._log(f"Detection engine: {device}  |  max_batch={max_b}")
        model = yolo.get_model(cfg["model_path"])
        active = get_active_model()
        active_model_id: Optional[int] = active["id"] if active else None

        while not self._stop.is_set():
            raw_batch:  list[bytes] = []
            name_batch: list[str]   = []

            try:
                while len(raw_batch) < max_b:
                    n, raw = self._raw_q.get(timeout=0.05)
                    raw_batch.append(raw)
                    name_batch.append(n)
            except queue.Empty:
                if not raw_batch:
                    continue

            # Decode all images in batch
            imgs         = []
            valid_names  = []
            valid_raws   = []
            for n, raw in zip(name_batch, raw_batch):
                img = veyon.decode_image(raw)
                if img is not None:
                    imgs.append(img)
                    valid_names.append(n)
                    valid_raws.append(raw)

            if not imgs:
                continue

            try:
                results = model(
                    imgs,
                    imgsz=cfg["detect_imgsz"],
                    conf=float(cfg["detect_conf"]),
                    iou=float(cfg["detect_iou"]),
                    verbose=False, device=device, rect=True,
                )

                for comp_name, res, img_bgr in zip(valid_names, results, imgs):
                    annotated, dets = postprocess(
                        res, img_bgr, bool(cfg["keep_top1"])
                    )

                    # Store the RAW frame so the DB always holds clean pixels.
                    ok, buf = cv2.imencode(
                        ".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 75]     # ← raw stored
                    )
                    frame_bytes = buf.tobytes() if ok else None

                    computer_id = state.computer_ids.get(comp_name)
                    user_id     = state.computer_users.get(comp_name)
                    os_uname    = state.computer_os_usernames.get(comp_name)

                    if computer_id is not None:
                        ev_id = insert_event(
                            computer_id, dets,
                            user_id=user_id,
                            os_username=os_uname,
                            frame_bytes=frame_bytes,
                            model_id=active_model_id,
                        )
                        n_fired = alert_service.check_and_fire(
                            ev_id, dets, comp_name,
                        )
                        student_disp = os_uname or "—"
                        if user_id:
                            u = get_user_by_id(user_id)
                            if u:
                                student_disp = u["username"]
                        if n_fired:
                            self._log(
                                f"[{comp_name}] 🔔 {n_fired} alert(s) fired "
                                f"({student_disp})"
                            )

                    # Push to live preview queue — drop silently if full
                    try:
                        state.img_q.put_nowait((comp_name, img_bgr, annotated, dets))
                    except queue.Full:
                        pass

                    if dets:
                        self._log(
                            f"[{comp_name}] " +
                            ", ".join(
                                f"{d['class_name']}({d['conf']:.0%})"
                                for d in dets
                            )
                        )
                    else:
                        self._log(f"[{comp_name}] no detections")

            except Exception as e:
                self._log(f"❌ Detection error: {e}")


# ── Background drain worker ───────────────────────────────────────────────────

def drain_worker() -> None:
    """
    Daemon thread — drains log_q and img_q into global state.
    DB writes are done in the detect worker, not here.
    """
    while True:
        try:
            while True:
                msg = state.log_q.get_nowait()
                state.log_buffer.append(msg)
                if len(state.log_buffer) > state.LOG_CAP:
                    state.log_buffer.pop(0)
        except queue.Empty:
            pass

        try:
            while True:
                name, raw_bgr, ann_bgr, dets = state.img_q.get_nowait()
                state.latest_frames[name] = (img_to_b64(ann_bgr), img_to_b64(raw_bgr), dets)
        except queue.Empty:
            pass

        time.sleep(0.05)