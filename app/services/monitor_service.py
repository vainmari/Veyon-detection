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

import csv
import os
import queue
import sys
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
    create_run,
    finish_run,
    get_active_model,
    get_model_by_id,
    get_user_by_id,
    get_user_by_username,
    insert_event,
    set_run_model,
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
        started_by:  Optional[int]        = None,
        group_name:  Optional[str]        = None,
    ) -> None:
        """
        cfg         — settings dict from collect_cfg()
        computers   — optional pre-filtered list of {"name": str, "host": str}.
                      When None the controller discovers all computers from Veyon.
        model_id    — DB id of the ml_model to use; None = use active model.
        schedule_id — DB id of the schedule that triggered this session; used to
                      look up per-schedule notification class overrides.
        started_by  — user id of the teacher who clicked Start; None for
                      scheduler-triggered sessions.
        group_name  — label of the monitored group ('' / None = all computers);
                      recorded on the session's monitoring_run row for reports.
        """
        self.cfg         = cfg
        self._computers  = computers
        self.model_id    = model_id
        self.schedule_id = schedule_id
        self.started_by  = started_by
        self.group_name  = group_name
        self.run_id: Optional[int] = None
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
        if self.run_id is not None:
            try:
                finish_run(self.run_id)
            except Exception as e:
                self._log(f"⚠ Failed to finalize run record: {e}")
            self.run_id = None
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

        # Record the session for the Reports page. Created only once monitoring
        # is actually about to start — early validation failures above leave no
        # run row behind. Closed by stop(); crash leftovers are repaired by
        # finish_stale_runs() at next startup.
        try:
            self.run_id = create_run(
                trigger_type="schedule" if self.schedule_id else "manual",
                schedule_id=self.schedule_id,
                group_name=self.group_name or "",
                started_by=self.started_by,
            )
        except Exception as e:
            self._log(f"⚠ Failed to create run record: {e}")

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
        Per-computer network + decode thread: authenticate, grab framebuffer,
        decode JPEG, push (name, raw_bytes, np_image, ts) onto _raw_q. Decoding
        in this worker (instead of the detect thread) parallelizes JPEG decode
        across all monitored computers and removes ~3-5 ms × N of dead time
        from the hot inference path.

        OS-username lookup is cached and refreshed every USER_REFRESH_SEC
        seconds — it costs an HTTP /user call + a DB query, and the logged-in
        user almost never changes between consecutive frames.
        """
        USER_REFRESH_SEC = 30.0

        cfg      = self.cfg
        session  = requests.Session()
        session.headers["Connection"] = "keep-alive"
        conn_uid: Optional[str] = None
        last_user_check = 0.0

        while not self._stop.is_set():

            # (Re-)authenticate
            if conn_uid is None:
                conn_uid = veyon.authenticate(session, base_url, host, cfg)
                if conn_uid is None:
                    self._log(f"[{name}] Auth error — retry in 10 s")
                    self._stop.wait(10)
                    continue
                time.sleep(2)
                # Force a user refresh on the next iteration after re-auth
                last_user_check = 0.0

            raw = veyon.grab_framebuffer(
                session, base_url, conn_uid,
                cfg["img_fmt"], cfg["img_quality"], cfg["img_width"],
            )
            if raw is None:
                self._log(f"[{name}] Framebuffer failed — re-authenticating")
                conn_uid = None
                continue

            # ── Latency-measurement start point ──────────────────────────────
            # ts marks "frame received from monitored host". From this point
            # on, every step (JPEG decode, queue wait, batch fill, inference)
            # is on our side and therefore measurable. The measurement ends at
            # t_infer_done in _detect_worker, immediately after model() returns.
            # Latency = t_infer_done - ts represents the full local processing
            # pipeline, excluding network round-trip and remote screen capture
            # (which we cannot instrument from the client side).
            ts_capture = time.perf_counter()

            # Decode in this worker (parallel across IO threads) so the detect
            # worker doesn't burn time decoding N frames serially.
            img = veyon.decode_image(raw)
            if img is None:
                self._stop.wait(float(cfg["interval"]))
                continue

            now = time.perf_counter()
            if now - last_user_check >= USER_REFRESH_SEC:
                last_user_check = now
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
                # Use the ts_capture taken right after grab_framebuffer (above),
                # NOT a fresh timestamp here — the goal is to measure the whole
                # local pipeline, including the JPEG decode and any user-lookup
                # work done in this worker before the frame is queued.
                self._raw_q.put((name, raw, img, ts_capture), timeout=1.0)
            except queue.Full:
                pass  # back-pressure: drop frame (excluded from latency stats)

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
        if self.run_id is not None:
            try:
                set_run_model(self.run_id, active_model_id)
            except Exception as e:
                self._log(f"⚠ Failed to record run model: {e}")

        cycle_timing  = cfg.get("detect_cycle_timing", False)
        _agg_lats:  list[float] = []
        _capture_interval = float(cfg["interval"])
        _agg_flush  = time.perf_counter() + _capture_interval

        # Publish the capture interval so state.is_computer_online() can scale
        # its threshold to whatever the user has configured.
        state.capture_interval = _capture_interval
        state.computer_last_frame_ts.clear()

        try:
            while not self._stop.is_set():
                # IO workers already decoded the JPEG, so the detect path skips
                # cv2.imdecode entirely — frames arrive as (name, raw_bytes, np_image, ts).
                raw_batch:  list[bytes]      = []
                imgs:       list[np.ndarray] = []
                valid_names: list[str]       = []
                valid_ts:   list[float]      = []

                # Block for the first frame; once we have one, drain whatever
                # else is *already* in the queue without waiting. The previous
                # 50 ms-per-iteration timeout dominated single-computer latency
                # (a fixed ~50 ms wait per cycle for stragglers that almost
                # never came). This keeps natural batching when frames arrive
                # close together but adds zero idle time when they don't.
                try:
                    n, raw, img, ts = self._raw_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                raw_batch.append(raw)
                imgs.append(img)
                valid_names.append(n)
                valid_ts.append(ts)

                try:
                    while len(imgs) < max_b:
                        n, raw, img, ts = self._raw_q.get_nowait()
                        raw_batch.append(raw)
                        imgs.append(img)
                        valid_names.append(n)
                        valid_ts.append(ts)
                except queue.Empty:
                    pass

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

                    for comp_name, raw_bytes, res, img_bgr in zip(
                        valid_names, raw_batch, results, imgs
                    ):
                        annotated, dets = postprocess(res, img_bgr, keep_top1)

                        # Veyon already returned JPEG bytes (`raw_bytes`); reuse
                        # them for the DB blob and the unannotated preview. We
                        # only run cv2.imencode when there are detections to
                        # encode — that's the one image we have to materialize.
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
                                run_id=self.run_id,
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

                    # Stamp "last frame seen" for every computer in this batch.
                    # state.is_computer_online() reads these to decide whether
                    # a computer has gone silent.
                    _now_stamp = time.perf_counter()
                    for _n in valid_names:
                        state.computer_last_frame_ts[_n] = _now_stamp

                    if cycle_timing and valid_ts:
                        _agg_lats.extend(
                            (t_infer_done - ts) * 1000 for ts in valid_ts
                        )
                        now = time.perf_counter()
                        if now >= _agg_flush and _agg_lats:
                            avg        = sum(_agg_lats) / len(_agg_lats)
                            # Count only computers that delivered a frame
                            # recently — offline machines stay in
                            # state.latest_frames forever and used to inflate
                            # this count.
                            n_tracked  = sum(
                                1 for cn in state.latest_frames
                                if state.is_computer_online(cn)
                            ) or len(_agg_lats)
                            est_30     = avg * 30
                            self._log(
                                f"⏱ Cycle: avg={avg:.0f} ms  "
                                f"min={min(_agg_lats):.0f} ms  max={max(_agg_lats):.0f} ms  "
                                f"tracked={n_tracked}  est30={est_30:.0f} ms"
                            )
                            _write_latency_csv(avg, min(_agg_lats), max(_agg_lats),
                                               n_tracked, max_b, est_30)
                            _agg_lats.clear()
                            _agg_flush = now + _capture_interval

                    for line in batch_log:
                        self._log(line)

                except Exception as e:
                    self._log(f"❌ Detection error: {e}")

        finally:
            # Always clear the running model so the UI stops reflecting it.
            state.running_model_id = None


# ── Latency CSV logger ───────────────────────────────────────────────────────

_LATENCY_CSV = os.path.join(
    os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "latency_log.csv",
)
_CSV_HEADER  = [
    "timestamp",        # local wall-clock at flush time (HH:MM:SS)
    "avg_ms",           # mean per-frame latency: t_infer_done − ts_capture
    "min_ms",           # min per-frame latency in this aggregation window
    "max_ms",           # max per-frame latency in this aggregation window
    "n_tracked",        # number of computers actively delivering frames
    "batch_size",       # max frames per inference call (hardware/model limit)
    "linear_proj_30_ms",# avg_ms × 30: linear scaling projection, NOT a prediction
]

def _write_latency_csv(avg: float, mn: float, mx: float,
                       n_tracked: int, batch_size: int, est_30: float) -> None:
    write_header = not os.path.exists(_LATENCY_CSV)
    with open(_LATENCY_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(_CSV_HEADER)
        w.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            f"{avg:.1f}", f"{mn:.1f}", f"{mx:.1f}",
            n_tracked, batch_size, f"{est_30:.1f}",
        ])


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