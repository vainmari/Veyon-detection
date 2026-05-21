"""
app/state.py
────────────
All mutable global state lives here so every module imports from one place.

Queue sizing
────────────
  log_q   — unbounded; log messages are tiny and drain_worker is very fast.
  img_q   — bounded at 64 frames.  The drain_worker runs every 50 ms and the
             detect worker pushes at most one frame per computer per inference
             cycle, so 64 slots is ample headroom.  A bounded queue prevents
             memory growth if the drain thread ever falls behind, and the
             detect worker simply drops the oldest work (put_nowait + except).
  _raw_q  — bounded at 128 inside MonitorController (back-pressure on I/O threads).
"""
from __future__ import annotations
import queue
import threading
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from app.services.monitor_service import MonitorController
    from app.services.training_service import TrainingWorker

monitor: Optional["MonitorController"] = None

# Serialises check-then-assign on `monitor` across threads. Both the UI
# (dashboard.do_start / do_stop) and the schedule_service daemon flip this
# variable, so without a lock a teacher clicking Start at the exact moment
# the scheduler ticks can create two MonitorController instances and leak
# one of them forever. Always acquire before reading + writing `monitor`.
monitor_lock: threading.Lock = threading.Lock()

log_q: queue.Queue[str]                          = queue.Queue()
img_q: queue.Queue[tuple[str, np.ndarray, np.ndarray, list]] = queue.Queue(maxsize=64)
#                                         ↑ raw bgr    ↑ annotated bgr

latest_frames:          dict[str, tuple[str, str, list]] = {}
#                                        ↑ ann_b64  ↑ raw_b64
log_buffer:             list[str]                   = []
log_total:              int                         = 0   # monotonic count of all messages ever added
LOG_CAP = 500

computer_ids:           dict[str, int]           = {}
computer_users:         dict[str, Optional[int]] = {}
computer_os_usernames:  dict[str, Optional[str]] = {}

# Per-computer perf_counter() of the last frame that successfully reached the
# detect worker. Used to decide whether a computer is "online" (still delivering
# frames) or has gone silent — see is_computer_online() below.
computer_last_frame_ts: dict[str, float]         = {}

# Capture interval (seconds). Mirrored from cfg["interval"] by _detect_worker
# on startup so the offline-detection threshold can scale with the user's
# configured cadence.
capture_interval:       float                    = 1.0

# A computer is considered online if a frame was received within this many
# seconds. We take max(10s, 3 × capture_interval) so brief network blips and
# the 10-second auth-retry window don't falsely flag a working machine offline.
def offline_threshold_sec() -> float:
    return max(10.0, 3.0 * capture_interval)


def is_computer_online(name: str) -> bool:
    """True if a frame from `name` was successfully processed recently."""
    import time as _time
    last_ts = computer_last_frame_ts.get(name)
    if last_ts is None:
        return False
    return (_time.perf_counter() - last_ts) < offline_threshold_sec()

# Active training job — persists across page navigations
training_worker: Optional["TrainingWorker"] = None

# Consecutive-detection counters used by alert_service threshold logic.
# Key: (computer_name, class_index: int) → consecutive hit count.
# Reset to 0 when a frame has NO detection for that class.
consecutive_detections: dict[tuple[str, int], int] = {}

# Model ID that the current monitoring session is actually running with.
# Set by _detect_worker after resolving the pinned/active model; cleared on stop.
# Use this instead of get_active_model() when you need to know what's in use NOW.
running_model_id: Optional[int] = None

# Set to True when monitoring was started automatically by the schedule service.
# The scheduler will only auto-STOP a session it started itself — it never stops
# a session the teacher launched manually.
schedule_triggered: bool = False

# Human-readable label for what is currently being monitored.
# None  → not running
# ""    → all computers (no group filter)
# "Lab 1" → a specific group name
monitored_group_name: Optional[str] = None