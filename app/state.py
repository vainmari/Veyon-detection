"""
app/state.py
────────────
All mutable global state lives here so every module imports from one place.
Nothing in this file does any I/O — it is pure data containers.
"""
from __future__ import annotations
import queue
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from app.services.monitor_service import MonitorController

# The single running monitor (None when stopped)
monitor: Optional["MonitorController"] = None

# Queues written by background threads, drained by the drain worker
log_q: queue.Queue[str]                               = queue.Queue()
img_q: queue.Queue[tuple[str, np.ndarray, list]]      = queue.Queue()

# Written by the drain worker, read by UI timers (no queue contention)
latest_frames: dict[str, tuple[str, list]] = {}   # name → (b64_jpeg, detections)
log_buffer:    list[str]                   = []   # rolling history, capped below
LOG_CAP = 500