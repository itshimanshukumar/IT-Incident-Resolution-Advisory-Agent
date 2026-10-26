import csv
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
SESSIONS_FILE = DATA_DIR / "sessions.csv"

SESSION_FIELDS = [
    "session_id",
    "employee_id",
    "location",
    "service",
    "operating_system",
    "symptom",
    "attempted_steps",
    "attempt_count",
    "status",
    "ticket_id",
    "created_at",
    "updated_at",
]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_file():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not SESSIONS_FILE.exists():
        with SESSIONS_FILE.open(
            "w", encoding="utf-8", newline=""
        ) as file:
            writer = csv.DictWriter(file, fieldnames=SESSION_FIELDS)
            writer.writeheader()


def _read_sessions():
    _ensure_file()

    with SESSIONS_FILE.open(
        "r", encoding="utf-8", newline=""
    ) as file:
        return list(csv.DictReader(file))


def _write_sessions(rows):
    """Rewrite the CSV with the current session records."""
    _ensure_file()

    with SESSIONS_FILE.open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=SESSION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _public_session(row):
    """Convert stored JSON and numeric values to useful Python values."""
    if row is None:
        return None

    result = dict(row)

    try:
        result["attempted_steps"] = json.loads(
            result.get("attempted_steps", "[]")
        )
    except (TypeError, json.JSONDecodeError):
        result["attempted_steps"] = []

    try:
        result["attempt_count"] = int(result.get("attempt_count", 0))
    except (TypeError, ValueError):
        result["attempt_count"] = 0

    return result


def get_session(session_id):
    """Find one session by its unique ID."""
    if not session_id or not session_id.strip():
        return None

    for row in _read_sessions():
        if row["session_id"] == session_id.strip():
            return _public_session(row)

    return None


def create_session(
    employee_id,
    location,
    service,
    operating_system,
    symptom,
):
    """Create and persist a new incident session."""
    required = {
        "employee_id": employee_id,
        "location": location,
        "service": service,
        "operating_system": operating_system,
        "symptom": symptom,
    }

    missing = [
        key for key, value in required.items()
        if not isinstance(value, str) or not value.strip()
    ]

    if missing:
        return {
            "status": "validation_error",
            "missing_fields": missing,
        }

    rows = _read_sessions()
    now = _now()

    row = {
        "session_id": "S-" + uuid.uuid4().hex[:12].upper(),
        "employee_id": employee_id.strip(),
        "location": location.strip(),
        "service": service.strip(),
        "operating_system": operating_system.strip(),
        "symptom": symptom.strip(),
        "attempted_steps": "[]",
        "attempt_count": "0",
        "status": "Open",
        "ticket_id": "",
        "created_at": now,
        "updated_at": now,
    }

    rows.append(row)
    _write_sessions(rows)

    return {
        "status": "created",
        "session": _public_session(row),
    }


def record_attempt(session_id, step):
    """Record a completed troubleshooting step without duplicating it."""
    if not isinstance(step, str) or not step.strip():
        return {
            "status": "validation_error",
            "message": "A non-empty step is required.",
        }

    rows = _read_sessions()

    for row in rows:
        if row["session_id"] != session_id:
            continue

        session = _public_session(row)
        steps = session["attempted_steps"]

        normalized_step = " ".join(step.lower().split())

        already_recorded = any(
            " ".join(existing.lower().split()) == normalized_step
            for existing in steps
        )

        if already_recorded:
            return {
                "status": "existing",
                "session": session,
            }

        steps.append(step.strip())
        row["attempted_steps"] = json.dumps(steps, ensure_ascii=False)
        row["attempt_count"] = str(len(steps))
        row["updated_at"] = _now()

        _write_sessions(rows)

        return {
            "status": "recorded",
            "session": _public_session(row),
        }

    return {
        "status": "not_found",
        "message": "Session not found.",
    }


def update_session_status(session_id, status, ticket_id=None):
    """Update the session status and optionally attach a ticket ID."""
    allowed_statuses = {"Open", "In Progress", "Escalated", "Resolved"}

    if status not in allowed_statuses:
        return {
            "status": "validation_error",
            "message": "Unsupported session status.",
        }

    rows = _read_sessions()

    for row in rows:
        if row["session_id"] != session_id:
            continue

        row["status"] = status

        if ticket_id:
            row["ticket_id"] = ticket_id

        row["updated_at"] = _now()
        _write_sessions(rows)

        return {
            "status": "updated",
            "session": _public_session(row),
        }

    return {
        "status": "not_found",
        "message": "Session not found.",
    }