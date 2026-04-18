"""
app/db/database.py
──────────────────
Public façade — re-exports every symbol from the sub-modules so that all
existing call-sites (from app.db.database import …) continue to work unchanged.

Sub-module layout
─────────────────
  _core.py      — thread-local connection pool, DB_PATH, low-level helpers
  schema.py     — init_db(), migrations, seed data, DEFAULT_CLASSES
  users.py      — role lookups + full user CRUD
  computers.py  — computer registry (with group assignment)
  groups.py     — computer_group CRUD
  schedules.py  — monitoring schedule CRUD + active-now query
  audit.py      — append-only audit log
  detection.py  — detection_class, detection_event, detection rows
  alerts.py     — alert rules (on detection_class) + notifications (JOINed)
  ml_models.py  — ml_model (includes training config) + sync_classes_from_model
  analytics.py  — read-only aggregate queries
"""
from app.db._core import DB_PATH, _tls, _conn, _now                      # noqa: F401
from app.db.schema import (                                                # noqa: F401
    DEFAULT_CLASSES,
    init_db, seed_classes,
    ensure_default_admin, ensure_default_teacher,
)
from app.db.users import (                                                 # noqa: F401
    list_roles, get_role_id,
    create_user, update_password, delete_user,
    auto_assign_user_events, nullify_user_events,
    auto_create_student, activate_user,
    get_user_by_username, get_user_by_id,
    verify_password, list_users,
)
from app.db.computers import upsert_computer, list_computers               # noqa: F401
from app.db.groups import (                                                 # noqa: F401
    list_groups, get_group, get_or_create_group,
    create_group, update_group, delete_group,
    add_computer_to_group, remove_computer_from_group,
    assign_computer_to_group, list_computers_in_group,
)
from app.db.schedules import (                                              # noqa: F401
    list_schedules, list_schedules_for_group, get_schedule,
    create_schedule, update_schedule, delete_schedule,
    get_active_schedules_now, find_overlapping_schedules,
    get_notify_class_ids_for_schedule, set_schedule_notify_classes,
    list_schedules_using_model,
)
from app.db.audit import log_action, list_audit_log                        # noqa: F401
from app.db.detection import (                                             # noqa: F401
    list_classes, get_class_by_model_index,
    insert_event, get_event_frame_b64,
    get_event_frame_annotated_b64, count_anonymous_events,
    assign_anonymous_events,
)
from app.db.alerts import (                                                # noqa: F401
    list_alert_rules, set_alert_rule, get_prohibited_class_ids,
    insert_notification, list_notifications,
    count_unread_notifications, mark_read, mark_all_read,
)
from app.db.ml_models import (                                             # noqa: F401
    create_ml_model, update_ml_model, get_active_model, get_model_by_id,
    set_active_model, list_models, delete_model,
    sync_classes_from_model, get_class_ids_for_model,
)
from app.db.analytics import (                                             # noqa: F401
    get_summary_stats, get_class_distribution,
    get_daily_detections, get_student_activity,
    count_events_for_user, count_query_events, query_events,
)
