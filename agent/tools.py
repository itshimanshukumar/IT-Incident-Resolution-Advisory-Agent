
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path


# Locate the project's data directory.
DATA_DIR = Path(__file__).parent.parent / "data"

OUTAGES_FILE = DATA_DIR / "outages.csv"
INCIDENTS_FILE = DATA_DIR / "incidents.csv"
TICKETS_FILE = DATA_DIR / "tickets.csv"
RULES_FILE = DATA_DIR / "escalation_rules.json"

TICKET_FIELDS = [
    "ticket_id",
    "employee_id",
    "location",
    "issue",
    "priority",
    "created_at",
    "status",
    "idempotency_key",
]


def check_network_status(
    location: str,
    service: str = "Wi-Fi",
) -> dict:
    """Return the latest matching recorded outage."""
    if not location or not location.strip():
        return {
            "status": "missing_location",
            "message": "Please provide your location.",
        }

    if not service or not service.strip():
        return {
            "status": "missing_service",
            "message": "Please provide the affected service.",
        }

    if not OUTAGES_FILE.exists():
        return {
            "status": "unknown",
            "message": "Outage data file is unavailable.",
            "location": location.strip(),
            "service": service.strip(),
        }

    try:
        with OUTAGES_FILE.open(
            "r", encoding="utf-8-sig", newline=""
        ) as file:
            records = list(csv.DictReader(file))
    except (OSError, csv.Error):
        return {
            "status": "unknown",
            "message": "Unable to read outage records.",
            "location": location.strip(),
            "service": service.strip(),
        }

    matches = [
        row for row in records
        if row.get("location", "").strip().casefold()
        == location.strip().casefold()
        and row.get("service", "").strip().casefold()
        == service.strip().casefold()
    ]

    if not matches:
        return {
            "status": "unknown",
            "message": "No matching outage record was found.",
            "location": location.strip(),
            "service": service.strip(),
        }

    try:
        matches.sort(
            key=lambda row: datetime.strptime(
                row["last_updated"], "%Y-%m-%d %H:%M"
            ),
            reverse=True,
        )
    except (ValueError, KeyError, TypeError):
        return {
            "status": "unknown",
            "message": "Outage timestamps are invalid.",
            "location": location.strip(),
            "service": service.strip(),
        }

    record = matches[0]

    try:
        updated_at = datetime.strptime(
            record["last_updated"], "%Y-%m-%d %H:%M"
        )
        age_hours = (
            datetime.now() - updated_at
        ).total_seconds() / 3600

        freshness = (
            "fresh" if 0 <= age_hours <= 24 else "stale"
        )
    except (ValueError, KeyError):
        freshness = "unknown"

    status = record.get("status", "").strip().casefold()
    if status not in {"active", "resolved"}:
        status = "unknown"

    return {
        "status": status,
        "outage_ref": record.get("outage_ref", ""),
        "location": record.get("location", ""),
        "service": record.get("service", ""),
        "update_text": record.get("update_text", ""),
        "last_updated": record.get("last_updated", ""),
        "freshness": freshness,
        "source": "outages.csv",
    }


def load_escalation_rules() -> dict:
    """Read escalation rules from the JSON file."""
    with RULES_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_escalation_rule(
    service: str,
    symptom: str = "",
) -> dict:
    """Find the rule matching a service and symptom."""
    try:
        config = load_escalation_rules()
    except (OSError, json.JSONDecodeError):
        return {
            "service": service,
            "default_priority": "Low",
            "escalate_after_attempts": 2,
            "matched": False,
            "message": "Rules unavailable; using safe defaults.",
        }

    for rule in config.get("rules", []):
        if rule.get("service", "").casefold() != service.casefold():
            continue

        keywords = rule.get("symptom_keywords", [])
        symptom_text = symptom.casefold()

        if not keywords or any(
            keyword.casefold() in symptom_text
            for keyword in keywords
        ):
            return {**rule, "matched": True}

    return {
        "service": service,
        "default_priority": config.get("default_priority", "Low"),
        "escalate_after_attempts": 2,
        "matched": False,
    }


def search_incident_history(
    symptom: str,
    location: str = "",
    service: str = "",
    os: str = "",
    limit: int = 5,
) -> dict:
    """Search historical incidents by symptom and optional filters."""
    if not symptom or not symptom.strip():
        return {
            "status": "missing_symptom",
            "results": [],
        }

    if not INCIDENTS_FILE.exists():
        return {
            "status": "unavailable",
            "results": [],
            "message": "Incident history file is unavailable.",
        }

    try:
        with INCIDENTS_FILE.open(
            "r", encoding="utf-8-sig", newline=""
        ) as file:
            records = list(csv.DictReader(file))
    except (OSError, csv.Error):
        return {"status": "unavailable", "results": []}

    words = set(symptom.casefold().split())
    results = []

    for record in records:
        if location and record.get("location", "").casefold() != location.casefold():
            continue
        if service and record.get("service", "").casefold() != service.casefold():
            continue
        if os and record.get("os", "").casefold() != os.casefold():
            continue

        historical_symptom = record.get("symptom", "").casefold()
        historical_words = set(historical_symptom.split())
        overlap = words & historical_words

        if not overlap:
            continue

        results.append({
            **record,
            "match_score": round(
                len(overlap) / max(len(words), 1), 2
            ),
        })

    results.sort(
        key=lambda item: item["match_score"],
        reverse=True,
    )

    return {
        "status": "found" if results else "no_match",
        "results": results[:max(1, min(limit, 20))],
        "source": "incidents.csv",
    }


def _make_idempotency_key(
    employee_id: str,
    location: str,
    issue: str,
) -> str:
    """Create a stable key for the same employee and issue."""
    normalized = "|".join([
        employee_id.strip().casefold(),
        location.strip().casefold(),
        " ".join(issue.casefold().split()),
    ])
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def create_ticket(
    employee_id: str,
    location: str,
    issue: str,
    priority: str = "",
    service: str = "Wi-Fi",
) -> dict:
    """Create one ticket per employee/location/issue combination."""
    employee_id = employee_id.strip() if employee_id else ""
    location = location.strip() if location else ""
    issue = issue.strip() if issue else ""
    service = service.strip() if service else ""

    if not all([employee_id, location, issue, service]):
        return {
            "status": "validation_error",
            "message": "Employee ID, location, issue and service are required.",
        }

    allowed_priorities = {"Low", "Medium", "High", "Critical"}
    if priority and priority.title() not in allowed_priorities:
        return {
            "status": "validation_error",
            "message": "Priority must be Low, Medium, High or Critical.",
        }

    rule = get_escalation_rule(service, issue)
    selected_priority = (
        priority.title()
        if priority
        else rule["default_priority"].title()
    )

    if selected_priority not in allowed_priorities:
        selected_priority = "Low"

    key = _make_idempotency_key(employee_id, location, issue)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Create the header if the ticket file is new or empty.
    if not TICKETS_FILE.exists() or TICKETS_FILE.stat().st_size == 0:
        with TICKETS_FILE.open(
            "w", encoding="utf-8", newline=""
        ) as file:
            writer = csv.DictWriter(file, fieldnames=TICKET_FIELDS)
            writer.writeheader()

    try:
        with TICKETS_FILE.open(
            "r", encoding="utf-8-sig", newline=""
        ) as file:
            reader = csv.DictReader(file)
            existing = list(reader)
            existing_fields = reader.fieldnames or []
    except (OSError, csv.Error):
        return {
            "status": "storage_error",
            "message": "Unable to read the ticket file.",
        }

    if not set(TICKET_FIELDS).issubset(existing_fields):
        return {
            "status": "storage_error",
            "message": "Ticket CSV is missing required columns.",
        }

    # Return the existing ticket for a repeated request.
    for ticket in existing:
        if ticket.get("idempotency_key") == key:
            return {
                "status": "existing",
                "message": "An equivalent ticket already exists.",
                "ticket": ticket,
            }

    # Choose the next ticket ID based on saved records.
    largest_number = 0
    for ticket in existing:
        ticket_id = ticket.get("ticket_id", "")
        if ticket_id.startswith("T-"):
            try:
                largest_number = max(
                    largest_number,
                    int(ticket_id[2:]),
                )
            except ValueError:
                pass

    ticket = {
        "ticket_id": f"T-{largest_number + 1:04d}",
        "employee_id": employee_id,
        "location": location,
        "issue": issue,
        "priority": selected_priority,
        "created_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "status": "Open",
        "idempotency_key": key,
    }

    try:
        with TICKETS_FILE.open(
            "a", encoding="utf-8", newline=""
        ) as file:
            writer = csv.DictWriter(file, fieldnames=TICKET_FIELDS)
            writer.writerow(ticket)
    except OSError:
        return {
            "status": "storage_error",
            "message": "Unable to save the new ticket.",
        }

    return {
        "status": "created",
        "message": "Support ticket created successfully.",
        "ticket": ticket,
    }
