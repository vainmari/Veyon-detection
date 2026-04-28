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
pip install -e ".[dev]"

# 3. Place your YOLO model
#    Put ONNX_FP32.onnx (or any .pt / .onnx) inside the weights/ folder

# 4. Start the server
python run.py
# → browser opens at http://localhost:8080 automatically
```

Default login: **admin / admin** — change via Users page immediately.

Settings (auth keys, Veyon CLI path, detection thresholds, etc.) are configurable at runtime via the Settings page and persisted between restarts.

---

## Project Structure

```
repo/
│
├── run.py                          Entry point — imports app.main and starts NiceGUI
├── build.py                        PyInstaller build script (produces .exe)
├── pyproject.toml                  Project metadata and dependencies
├── conftest.py                     Adds repo root to sys.path for pytest
│
├── weights/                        YOLO model files (.onnx / .pt)
│   └── ONNX_FP32.onnx
│
├── data/                           Created automatically on first run
│   ├── monitor.db                  SQLite database
│   ├── datasets/                   Uploaded dataset files
│   └── models/                     Uploaded or trained/fine-tuned models
│
├── app/
│   ├── main.py                     App factory: startup hooks, page imports, ui.run()
│   ├── config.py                   Default settings + get/save/collect_cfg helpers
│   ├── state.py                    Global mutable state shared across threads
│   │
│   ├── core/                       Pure utilities — no UI, no business logic
│   │   ├── auth.py                 Session helpers (get/set/clear, require_auth)
│   │   ├── imaging.py              postprocess(), img_to_b64()
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
│       ├── _file_browser.py        Reusable server-side file/folder picker dialog
│       ├── _snapshot.py            Snapshot viewer overlay
│       ├── login.py                /login
│       ├── dashboard.py            /          (teacher only — live grid)
│       ├── history.py              /history   (teacher: all; student: own)
│       ├── analytics.py            /analytics (teacher: all; student: own)
│       ├── users.py                /users     (teacher only)
│       ├── groups.py               /groups    (teacher only)
│       ├── schedules.py            /schedules (teacher only)
│       ├── alerts.py               /alerts    (teacher only)
│       ├── models.py               /models    (teacher only — import/train YOLO)
│       ├── audit.py                /audit     (admin only)
│       └── settings.py             /settings  (admin only)
│
└── tests/
    ├── conftest.py                 Adds repo root to sys.path
    ├── test_auth.py                Session helpers, require_auth role checks
    ├── test_config.py              Settings merging, collect_cfg() type casting
    ├── test_imaging.py             postprocess, img_to_b64
    ├── test_file_browser.py        _list_entries: ordering, filtering, error handling
    ├── test_database.py            DB schema, CRUD, query filters, auto-assign
    ├── test_monitor.py             MonitorController lifecycle, drain_worker
    └── test_veyon.py               Veyon client (all network calls mocked)
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
- **Windows username always logged.** Every `detection_event` stores `os_username` (the part after `COMPUTER\`). If no matching account exists yet, the column is populated anyway. When a teacher later creates an account with the same username, `create_user()` auto-assigns all matching historical events in one SQL UPDATE.

---

## Database Schema

```
role               ← admin / teacher / student
user               ← accounts (bcrypt passwords, role FK)
computer           ← monitored machines (name, host_address)
detection_class    ← YOLO class registry, seeded from DEFAULT_CLASSES
ml_model           ← imported / trained YOLO model records
detection_event    ← one row per captured frame, always logged
                      • computer_id → computer
                      • user_id     → user (nullable — assigned later)
                      • os_username — raw Windows login (e.g. "Lina")
                      • frame_blob  — annotated JPEG stored as BLOB
                      • had_detection — 0/1 convenience flag
detection          ← one row per bounding box inside an event
                      • event_id → detection_event
                      • class_id → detection_class
                      • confidence, box_x1/y1/x2/y2
group              ← named computer groups
computer_group     ← many-to-many: computer ↔ group
schedule           ← monitoring schedules (time windows per group)
notification       ← alert records with read/unread state
```

All foreign keys are enforced with `PRAGMA foreign_keys = ON`. The `_migrate()` function in `database.py` adds new columns safely to existing databases without data loss.

---

## User Roles

| Feature | Admin | Teacher | Student |
|---|---|---|---|
| Dashboard (live preview) | ✅ | ✅ | ❌ |
| History — all students | ✅ | ✅ | ❌ |
| History — own records | ✅ | ✅ | ✅ |
| Analytics | ✅ | ✅ | own only |
| Users page | ✅ | ✅ | ❌ |
| Groups & Schedules | ✅ | ✅ | ❌ |
| Models (import/train) | ✅ | ✅ | ❌ |
| Alerts | ✅ | ✅ | ❌ |
| Audit log | ✅ | ❌ | ❌ |
| Settings | ✅ | ❌ | ❌ |
| Start / Stop monitoring | ✅ | ✅ | ❌ |

Student usernames **must match their Windows login name** (the part after the backslash in `COMPUTER\username`). The monitor fetches the active Windows user from Veyon on every poll cycle and links events automatically.

---

## Configuration

All settings are editable at runtime via the Settings page and persisted between restarts.

| Key | Default | Description |
|---|---|---|
| `auth_method` | `key` | `key` (certificate) or `logon` (username/password) |
| `key_name` | `class` | Veyon authentication key name |
| `key_path` | `class.pem` | Path to the private key file |
| `logon_username` | _(empty)_ | Username for logon auth |
| `logon_password` | _(empty)_ | Password for logon auth |
| `veyon_cli` | `C:\...\veyon-cli.exe` | Full path to veyon-cli |
| `host` | `localhost` | Veyon WebAPI server host |
| `port` | `11080` | Veyon WebAPI server port |
| `auto_start` | `true` | Launch Veyon WebAPI automatically on startup |
| `start_wait` | `10` | Seconds to wait for WebAPI to become ready |
| `interval` | `1` | Poll interval in seconds per computer |
| `img_fmt` | `jpeg` | Capture format (`jpeg` or `png`) |
| `img_quality` | `85` | JPEG quality (1–100) |
| `img_width` | `480` | Capture width in pixels |
| `detect_conf` | `0.40` | YOLO confidence threshold |
| `detect_iou` | `0.20` | IoU threshold for NMS |
| `keep_top1` | `true` | Keep only the highest-confidence detection per class |
| `alert_threshold` | `1` | Minimum detections per event to trigger an alert |

---

## Running Tests

```bash
# Install dev dependencies (includes pytest)
pip install -e ".[dev]"

# All tests
pytest --tb=short -q

# Specific file or test
pytest tests/test_config.py -v
pytest tests/test_config.py::TestCollectCfgKeyData -v
```

| Test file | What it covers |
|---|---|
| `test_auth.py` | Session get/set/clear, `require_auth` redirects and role checks |
| `test_config.py` | Settings merging, `collect_cfg()` type casting, key file reading |
| `test_imaging.py` | Bounding-box postprocessing, base64 encoding, image immutability |
| `test_file_browser.py` | Directory listing, extension/mode filtering, permission errors |
| `test_database.py` | Schema, migrations, all CRUD, query filters, auto-assign logic |
| `test_monitor.py` | Username parsing, drain worker, `MonitorController` lifecycle |
| `test_veyon.py` | Port check, image decode, authenticate, framebuffer grab, user fetch, computer discovery |

All network, subprocess, and NiceGUI calls are mocked — tests run offline with no Veyon server needed.

---

## CI

Two test jobs run in parallel on every push; the build only runs after both pass.

```
test-unit ──┐
             ├─► build (exe + release)
test-db   ──┘
```

- **test-unit** — all mocked tests (`test_auth`, `test_config`, `test_imaging`, `test_file_browser`, `test_monitor`, `test_veyon`)
- **test-db** — database tests against a real SQLite file (`test_database`)
- **build** — PyInstaller `.exe`; on tagged commits also zips and publishes a GitHub Release

---

## System Requirements

- **OS:** Windows (required for Veyon). Python 3.10+.
- **CPU:** ≥ 6 cores, ≥ 3.5 GHz base clock.
- **RAM:** ≥ 16 GB.
- **Storage:** SSD recommended (SQLite BLOB writes).
- **Network:** LAN ≥ 1 Gbps between monitor machine and student computers.
- **GPU:** Optional — CUDA is used automatically if available, falls back to CPU (ONNX Runtime).
