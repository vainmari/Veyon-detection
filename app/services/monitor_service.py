"""
app/services/monitor_service.py
────────────────────────────────
MonitorController  — per-computer I/O threads + single YOLO batch thread.
drain_worker       — drains queues into global state (no DB writes here).

DB writes happen inside the detect worker. Every insert in a single
inference batch shares one transaction (one fsync) to keep the hot path
fast even at 30 computers × 1 fps. Frames are stored as JPEG BLOBs
directly in the database (no disk I/O).

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

import numpy as np
import requests
import torch

import app.state as state
from app.core import veyon, yolo
from app.core.imaging import postprocess, encode_jpeg
from app.core.veyon import WEBAPI_BASE_TPL
from app.db._core import _conn
from app.services import alert_service
from app.db.database import (
    auto_create_student,
    get_active_model,
    get_model_by_id,
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
        cfg:         dict,
        computers:   Optional[list[dict]] = None,
        model_id:    Optional[int]        = None,
        schedule_id: Optional[int]        = None,
    ) -> None:
        """
        cfg         — settings dict from collect_cfg()
        computers   — optional pre-filtered list of {"name": str, "host": str}.
                      When None the controller discovers all computers from Veyon.
        model_id    — DB id of the ml_model to use; None = use active model.
        schedule_id — DB id of the schedule that triggered this session; used to
                      look up per-schedule notification class overrides.
        """
        self.cfg         = cfg
        self._computers  = computers
        self.model_id    = model_id
        self.schedule_id = schedule_id
        self._stop = threading.Event()
        self._proc: Optional[object] = None
        # raw_q carries (computer_name, raw_jpeg_bytes, capture_ts) where
        # capture_ts is time.perf_counter() at the moment the frame was enqueued.
        self._raw_q: queue.Queue[tuple[str, bytes, float]] = queue.Queue(maxsize=128)

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="monitor-main").start()

    def stop(self) -> None:
        self._stop.set()
        state.running_model_id = None
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
                if db_user is None:
                    # Auto-create an inactive placeholder so events are linked
                    # and the name is shown in the dashboard immediately.
                    new_id  = auto_create_student(os_login)
                    db_user = get_user_by_id(new_id)
                    self._log(
                        f"[{name}] OS user '{os_login}' — "
                        "auto-created inactive student account"
                    )
                state.computer_users[name]        = db_user["id"] if db_user else None
                state.computer_os_usernames[name] = os_login
            else:
                state.computer_users[name]        = None
                state.computer_os_usernames[name] = None

            try:
                self._raw_q.put((name, raw, time.perf_counter()), timeout=1.0)
            except queue.Full:
                pass  # back-pressure: drop frame

            self._stop.wait(float(cfg["interval"]))

    # ── Single batched YOLO detection thread ──────────────────────────────────

    def _detect_worker(self) -> None:
        cfg    = self.cfg
        device = "cuda" if torch.cuda.is_available() else "cpu"

        # Resolve which model to load: schedule-pinned > active model > cfg fallback
        if self.model_id is not None:
            db_m = get_model_by_id(self.model_id)
            if db_m:
                model_path   = db_m.get("onnx_path") or db_m.get("pt_path") or cfg["model_path"]
                detect_imgsz = int(db_m.get("imgsz") or cfg["detect_imgsz"])
                active_model_id: Optional[int] = db_m["id"]
                self._log(f"Schedule model: {db_m['name']}")
            else:
                self._log("⚠ Scheduled model not found — falling back to active model")
                fallback     = get_active_model()
                model_path   = (
                    (fallback.get("onnx_path") or fallback.get("pt_path"))
                    if fallback else None
                ) or cfg["model_path"]
                detect_imgsz = int((fallback.get("imgsz") if fallback else None) or cfg["detect_imgsz"])
                active_model_id = fallback["id"] if fallback else None
        else:
            model_path   = cfg["model_path"]
            detect_imgsz = int(cfg["detect_imgsz"])
            active       = get_active_model()
            active_model_id = active["id"] if active else None

        model = yolo.get_model(model_path)

        # Determine max batch size. ONNX models may have a static batch dimension
        # (e.g. 1) that ONNX Runtime won't exceed — inspect the model to find out.
        # Dynamic-batch ONNX and .pt models use the user-configured hardware limits.
        hw_max = cfg["batch_max_cuda"] if device == "cuda" else cfg["batch_max_cpu"]
        if str(model_path).lower().endswith(".onnx"):
            static_b = yolo.onnx_static_batch_size(model_path)
            max_b    = static_b if static_b is not None else hw_max
        else:
            max_b = hw_max
        self._log(f"Detection engine: {device}  |  max_batch={max_b}")

        # FP16 on CUDA only — halves VRAM and ~30% faster on nvidia. On CPU
        # it's slower (no native fp16 path in most CPU kernels) so we skip it.
        use_half = (device == "cuda")

        # Warm up the model so the first real batch doesn't eat a 200-300 ms
        # JIT / kernel-compile stall that would stall the whole detect loop.
        try:
            _warm = np.zeros((detect_imgsz, detect_imgsz, 3), dtype=np.uint8)
            model([_warm],
                  imgsz=detect_imgsz,
                  conf=0.99, iou=0.99,
                  verbose=False, device=device, rect=True, half=use_half)
            self._log(f"Model warmed up ({'fp16' if use_half else 'fp32'})")
        except Exception as _e:
            self._log(f"⚠ Model warmup skipped: {_e}")

        img_q_put  = int(cfg["img_quality"])
        keep_top1  = bool(cfg["keep_top1"])
        det_conf   = float(cfg["detect_conf"])
        det_iou    = float(cfg["detect_iou"])

        # Publish which model is actually running so UI pages can reflect it.
        state.running_model_id = active_model_id

        cycle_timing = cfg.get("detect_cycle_timing", False)

        try:
            while not self._stop.is_set():
                raw_batch:  list[bytes] = []
                name_batch: list[str]   = []
                ts_batch:   list[float] = []

                try:
                    while len(raw_batch) < max_b:
                        n, raw, ts = self._raw_q.get(timeout=0.05)
                        raw_batch.append(raw)
                        name_batch.append(n)
                        ts_batch.append(ts)
                except queue.Empty:
                    if not raw_batch:
                        continue

                # Decode all images in batch; keep capture timestamps in sync
                imgs         = []
                valid_names  = []
                valid_ts:  list[float] = []
                for n, raw, ts in zip(name_batch, raw_batch, ts_batch):
                    img = veyon.decode_image(raw)
                    if img is not None:
                        imgs.append(img)
                        valid_names.append(n)
                        valid_ts.append(ts)

                if not imgs:
                    continue

                try:
                    results = model(
                        imgs,
                        imgsz=detect_imgsz,
                        conf=det_conf,
                        iou=det_iou,
                        verbose=False, device=device, rect=True, half=use_half,
                    )
                    # Snapshot immediately after inference returns — this is the
                    # "detection result formed" point for cycle timing purposes.
                    t_infer_done = time.perf_counter()

                    # Re-fetch active model each batch in case it changed
                    if active_model_id is None:
                        _active = get_active_model()
                        active_model_id = _active["id"] if _active else None

                    # Accumulate everything produced by this inference batch and
                    # commit once at the end: a single WAL fsync for 30 frames
                    # instead of 30 fsyncs (~20-30% throughput win at scale).
                    batch_log: list[str] = []
                    wrote_anything = False

                    for comp_name, res, img_bgr in zip(valid_names, results, imgs):
                        annotated, dets = postprocess(res, img_bgr, keep_top1)

                        # Encode raw JPEG once. When there are no detections
                        # `annotated is img_bgr`, so the same bytes serve the DB
                        # and the preview — no second encode.
                        raw_bytes = encode_jpeg(img_bgr, img_q_put)
                        ann_bytes = raw_bytes if not dets else encode_jpeg(annotated, img_q_put)

                        computer_id = state.computer_ids.get(comp_name)
                        user_id     = state.computer_users.get(comp_name)
                        os_uname    = state.computer_os_usernames.get(comp_name)

                        if computer_id is not None:
                            ev_id = insert_event(
                                computer_id, dets,
                                user_id=user_id,
                                os_username=os_uname,
                                frame_bytes=raw_bytes or None,
                                model_id=active_model_id,
                                _commit=False,
                            )
                            wrote_anything = True
                            n_fired = alert_service.check_and_fire(
                                ev_id, dets, comp_name,
                                model_id=active_model_id,
                                schedule_id=self.schedule_id,
                            )
                            student_disp = os_uname or "—"
                            if user_id:
                                u = get_user_by_id(user_id)
                                if u:
                                    student_disp = u["username"]
                            if n_fired:
                                batch_log.append(
                                    f"[{comp_name}] 🔔 {n_fired} alert(s) fired "
                                    f"({student_disp})"
                                )

                        # Push pre-encoded JPEG bytes to live preview queue
                        try:
                            state.img_q.put_nowait(
                                (comp_name, raw_bytes, ann_bytes, dets)
                            )
                        except queue.Full:
                            pass

                        if dets:
                            batch_log.append(
                                f"[{comp_name}] " +
                                ", ".join(
                                    f"{d['class_name']}({d['conf']:.0%})"
                                    for d in dets
                                )
                            )
                        else:
                            batch_log.append(f"[{comp_name}] no detections")

                    # Single commit flushes every insert_event + insert_notification
                    # from this batch as one transaction.
                    if wrote_anything:
                        try:
                            _conn().commit()
                        except Exception as _ce:
                            self._log(f"⚠ DB commit failed: {_ce}")

                    if cycle_timing and valid_ts:
                        lats = [(t_infer_done - ts) * 1000 for ts in valid_ts]
                        avg  = sum(lats) / len(lats)
                        self._log(
                            f"⏱ Cycle: avg={avg:.0f} ms  "
                            f"min={min(lats):.0f} ms  max={max(lats):.0f} ms"
                            + (f"  (n={len(lats)})" if len(lats) > 1 else "")
                        )

                    for line in batch_log:
                        self._log(line)

                except Exception as e:
                    self._log(f"❌ Detection error: {e}")

        finally:
            # Always clear the running model so the UI stops reflecting it.
            state.running_model_id = None


# ── Background drain worker ───────────────────────────────────────────────────

def drain_worker() -> None:
    """
    Daemon thread — drains log_q and img_q into global state.
    DB writes are done in the detect worker, not here.

    img_q now carries pre-encoded JPEG bytes from the detect worker; the
    drain only has to base64-wrap them (very cheap), instead of running a
    full cv2.imencode per frame per computer on every cycle.
    """
    from app.core.imaging import bytes_to_b64_dataurl

    while True:
        try:
            while True:
                msg = state.log_q.get_nowait()
                state.log_buffer.append(msg)
                state.log_total += 1
                if len(state.log_buffer) > state.LOG_CAP:
                    state.log_buffer.pop(0)
        except queue.Empty:
            pass

        try:
            while True:
                name, raw_bytes, ann_bytes, dets = state.img_q.get_nowait()
                # If ann_bytes is raw_bytes (no detections) the wrapper gets
                # called twice on the same bytes; base64 encoding is ~free
                # compared to the JPEG encode we already eliminated.
                state.latest_frames[name] = (
                    bytes_to_b64_dataurl(ann_bytes),
                    bytes_to_b64_dataurl(raw_bytes),
                    dets,
                )
        except queue.Empty:
            pass

        time.sleep(0.05)