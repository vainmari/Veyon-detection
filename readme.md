# Veyon AI Monitor

A real-time classroom monitoring system that captures student screens via the Veyon WebAPI, runs YOLO object detection to identify AI tool usage, and presents results in a browser-based dashboard — with no JavaScript required.

---

## Quick Start

```bash
# 1. Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and fill in configuration
copy .env.example .env          # then edit paths and secrets

# 4. Place your YOLO model
#    Put ONNX_FP32.onnx (or any .pt / .onnx) inside the weights/ folder

# 5. Start the server
python run.py
# → browser opens at http://localhost:8080 automatically
```

Default login: **admin / admin** — change via Users page immediately.

---

## Project Structure

```
repo/
│
├── run.py                          Entry point — just imports app.main
├── requirements.txt
├── .env.example                    Template for environment variables
├── conftest.py                     Adds repo root to sys.path for pytest
│
├── weights/                        YOLO model files (.onnx / .pt)
│   └── ONNX_FP32.onnx
│
├── data/                           Created automatically on first run
│   ├── monitor.db                  SQLite database
│   └── .nicegui/                   NiceGUI persistent storage (settings)
│
├── app/
│   ├── main.py                     App factory: startup hooks, page imports, ui.run()
│   ├── config.py                   Default settings + get/save helpers
│   ├── state.py                    Global mutable state shared across threads
│   │
│   ├── core/                       Pure utilities — no UI, no business logic
│   │   ├── auth.py                 Session helpers (get/set/clear, require_auth)
│   │   ├── imaging.py              postprocess(), img_to_b64(), save_image()
│   │   ├── veyon.py                Veyon WebAPI client (auth, framebuffer, user fetch)
│   │   └── yolo.py                 YOLO model singleton (get_model, reset_model)
│   │
│   ├── db/
│   │   └── database.py             All SQLite access: schema, CRUD, queries, analytics
│   │
│   ├── services/
│   │   └── monitor_service.py      MonitorController + drain_worker background thread
│   │
│   └── pages/                      One file per browser page
│       ├── _nav.py                 Shared navigation bar (role-aware, Start/Stop)
│       ├── login.py                /login
│       ├── dashboard.py            /          (teacher only)
│       ├── history.py              /history   (teacher: all; student: own)
│       ├── analytics.py            /analytics (teacher: all; student: own)
│       ├── users.py                /users     (teacher only)
│       └── settings.py             /settings  (teacher only)
│
└── tests/
    ├── conftest.py                 (empty — root conftest handles path)
    ├── test_database.py            DB schema, CRUD, queries, auto-assign
    ├── test_auth.py                Session helpers, require_auth
    ├── test_imaging.py             postprocess, img_to_b64, save_image
    ├── test_veyon.py               Veyon client (all network calls mocked)
    └── test_monitor.py             MonitorController, drain_worker, DB integration
```

---

## Architecture

### Data flow

```
Veyon WebAPI
    │
    │  (one thread per computer — pure I/O)
    ▼
_raw_q  ──────────────────────────────────────────────────────────┐
                                                                   │
                                        YOLO batch detect thread  │
                                          ├─ postprocess()        │
                                          ├─ insert_event() → DB  │
                                          └─ state.img_q ─────────┤
                                                                   │
                              drain_worker thread (50 ms loop)    │
                                  ├─ state.log_buffer  ◄──────────┘
                                  └─ state.latest_frames

                              NiceGUI UI timers (100 ms, per tab)
                                  ├─ read state.log_buffer → ui.log
                                  ├─ read state.latest_frames → ui.image
                                  └─ read state.computer_users → student label
```

**Key design decisions:**

- **One YOLO thread, many I/O threads.** Frame capture is network-bound and nearly free. All CPU/GPU inference is batched into a single thread, which keeps GPU utilisation high and latency low.
- **Drain worker as buffer.** A single background thread drains both queues into plain Python dicts. UI timers read those dicts — no queue contention between multiple open browser tabs.
- **Frames stored as BLOBs.** Annotated JPEG frames are stored directly in SQLite. No files on disk, no path management, no cleanup needed.
- **Windows username always logged.** Every `detection_event` stores `windows_username` (the part after `COMPUTER\`). If no matching account exists yet, the column is populated anyway. When a teacher later creates an account with the same username, `create_user()` auto-assigns all matching historical events in one SQL UPDATE.

---

## Database Schema

```
computer           ← monitored machines (name, host_address)
user               ← teacher or student accounts
detection_class    ← YOLO class registry, seeded from DEFAULT_CLASSES
detection_event    ← one row per captured frame, always logged
                      • computer_id → computer
                      • user_id     → user (nullable — assigned later)
                      • windows_username  — raw OS login (e.g. "Lina")
                      • frame_blob  — annotated JPEG stored as BLOB
                      • had_detection — 0/1 convenience flag
detection          ← one row per bounding box inside an event
                      • event_id → detection_event
                      • class_id → detection_class
                      • confidence, box_x1/y1/x2/y2
```

All foreign keys are enforced with `PRAGMA foreign_keys = ON`. The `_migrate()` function in `database.py` adds new columns safely to existing databases.

---

## Adding a New Detection Class

1. Add the class to `DEFAULT_CLASSES` in `app/db/database.py`:
   ```python
   {"index": 7, "name": "Calculator", "color": "#fbbf24"},
   ```
2. Add the matching colour to `BOX_COLORS` in `app/core/imaging.py`:
   ```python
   (251, 191,  36),   # 7  Calculator — amber
   ```
3. Retrain or fine-tune your YOLO model to include the new class.
4. Delete `data/monitor.db` (or run a manual INSERT into `detection_class`) and restart.

---

## User Roles

| Feature | Teacher / Admin | Student |
|---|---|---|
| Dashboard (live preview) | ✅ | ❌ |
| History — all students | ✅ | ❌ |
| History — own records | ✅ | ✅ |
| Analytics — all students | ✅ | ❌ |
| Analytics — own records | ✅ | ✅ |
| Users page | ✅ | ❌ |
| Settings | ✅ | ❌ |
| Start / Stop monitoring | ✅ (nav bar) | ❌ |

Student usernames **must match their Windows login name** (the part after the backslash in `COMPUTER\username`). The monitor fetches the active Windows user from Veyon on every poll cycle and links events automatically.

---

## Configuration

All settings are editable at runtime via the Settings page and persisted between restarts. The same keys are available as defaults in `app/config.py` and as environment variable overrides in `.env`.

| Key | Default | Description |
|---|---|---|
| `key_name` | `class` | Veyon authentication key name |
| `key_path` | `class.pem` | Path to the private key file |
| `veyon_cli` | `C:\...\veyon-cli.exe` | Full path to veyon-cli |
| `host` | `localhost` | WebAPI server host |
| `port` | `11080` | WebAPI server port |
| `auto_start` | `true` | Launch Veyon WebAPI automatically |
| `interval` | `1` | Poll interval in seconds |
| `model_path` | `weights/ONNX_FP32.onnx` | YOLO model path |
| `detect_conf` | `0.40` | Detection confidence threshold |
| `detect_iou` | `0.20` | IoU threshold for NMS |
| `output_dir` | `./data/screenshots` | Where raw frames are saved (if enabled) |

---

## Running Tests

```bash
pytest tests/ -v
```

| Test file | Covers |
|---|---|
| `test_database.py` | Schema, migrations, all CRUD, query filters, auto-assign logic |
| `test_auth.py` | Session get/set/clear, require_auth redirects and role checks |
| `test_imaging.py` | Bounding box drawing, base64 encoding, file saving |
| `test_veyon.py` | Port check, image decode, authenticate, framebuffer grab, user fetch, computer discovery |
| `test_monitor.py` | Username parsing, drain worker, MonitorController lifecycle, DB integration |

All network, subprocess, and NiceGUI calls are mocked — tests run offline with no Veyon server needed.

---

## System Requirements

- **OS:** Windows (required for Veyon). Python 3.10+.
- **CPU:** ≥ 6 cores, ≥ 3.5 GHz base clock.
- **RAM:** ≥ 16 GB.
- **Storage:** SSD recommended (SQLite BLOB writes).
- **Network:** LAN ≥ 1 Gbps between monitor machine and student computers.
- **GPU:** Optional — CUDA is used automatically if available, falls back to CPU.