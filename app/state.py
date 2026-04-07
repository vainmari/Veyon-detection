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
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from app.services.monitor_service import MonitorController
    from app.services.training_service import TrainingWorker

monitor: Optional["MonitorController"] = None

log_q: queue.Queue[str]                          = queue.Queue()
img_q: queue.Queue[tuple[str, np.ndarray, np.ndarray, list]] = queue.Queue(maxsize=64)
#                                         ↑ raw bgr    ↑ annotated bgr

latest_frames:          dict[str, tuple[str, str, list]] = {}
#                                        ↑ ann_b64  ↑ raw_b64
log_buffer:             list[str]                   = []
LOG_CAP = 500

computer_ids:           dict[str, int]           = {}
computer_users:         dict[str, Optional[int]] = {}
computer_os_usernames:  dict[str, Optional[str]] = {}

# Active training job — persists across page navigations
training_worker: Optional["TrainingWorker"] = None

# Consecutive-detection counters used by alert_service threshold logic.
# Key: (computer_name, class_index: int) → consecutive hit count.
# Reset to 0 when a frame has NO detection for that class.
consecutive_detections: dict[tuple[str, int], int] = {}

# Set to True when monitoring was started automatically by the schedule service.
# The scheduler will only auto-STOP a session it started itself — it never stops
# a session the teacher launched manually.
schedule_triggered: bool = False

# Human-readable label for what is currently being monitored.
# None  → not running
# ""    → all computers (no group filter)
# "Lab 1" → a specific group name
monitored_group_name: Optional[str] = None