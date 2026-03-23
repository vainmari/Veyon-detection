"""
app/state.py
────────────
All mutable global state lives here so every module imports from one place.
"""
from __future__ import annotations
import queue
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from app.services.monitor_service import MonitorController
    from app.services.training_service import TrainingWorker

monitor: Optional["MonitorController"] = None

log_q: queue.Queue[str]                               = queue.Queue()
img_q: queue.Queue[tuple[str, np.ndarray, list]]      = queue.Queue()

latest_frames:          dict[str, tuple[str, list]] = {}
log_buffer:             list[str]                   = []
LOG_CAP = 500

computer_ids:           dict[str, int]              = {}
computer_users:         dict[str, Optional[int]]    = {}
computer_win_usernames: dict[str, Optional[str]]    = {}

# Active training job — persists across page navigations
training_worker: Optional["TrainingWorker"] = None