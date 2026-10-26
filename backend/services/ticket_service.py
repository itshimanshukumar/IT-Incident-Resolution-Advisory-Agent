import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from backend.config import settings
from backend.database import get_connection, sync_tickets_to_csv, log_audit
from backend.models import TicketResponse, TicketHistoryEntry, DashboardMetricsResponse
from backend.services.outage_service import normalize_service, normalize_location

def load_escalation_rules(rules_file: Optional[Path] = None) -> Dict[str, Any]:
    file_path = rules_file or settings.ESCALATION_RULES_FILE
    if file_path.exists():
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "rules": [],
        "default_fallback": {"default_priority": "Medium", "escalate_after_attempts": 3, "target_department": "IT Support"}
    }

def evaluate_escalation_rules(
    service: str,
    issue_text: str,
    rules_data: Optional[Dict[str, Any]] = None
) -> Tuple[str, str, int]:
    """
    Evaluates escalation rules from escalation_rules.json based on service and symptom keywords.
    Returns (priority, department, escalate_after_attempts).
    Deterministic fallback when a rule is missing or malformed.
    """
    rules = rules_data or load_escalation_rules()
    norm_srv = normalize_service(service)
    issue_low = (issue_text or "").lower()

    if isinstance(rules, dict):
        for rule in rules.get("rules", []):
            if not isinstance(rule, dict):
                continue
            r_srv = normalize_service(rule.get("service", ""))
            if r_srv == norm_srv:
                keywords = rule.get("symptom_keywords", [])
                if any(kw.lower() in issue_low for kw in keywords if isinstance(kw, str)):
                    return (
                        str(rule.get("default_priority", "Medium")),
                        str(rule.get("target_department", "IT Support")),
                        int(rule.get("escalate_after_attempts", 3))
                    )

    # Default fallback
    fallback = rules.get("default_fallback", {}) if isinstance(rules, dict) else {}
    dept = "IT Support Desk"
    if norm_srv == "Wi-Fi":
        dept = "Network Support"
    elif norm_srv == "Printer":
        dept = "Workplace IT"
    elif norm_srv == "Email":
        dept = "Messaging Support"

    return (
        str(fallback.get("default_priority", "Medium")),
        str(fallback.get("target_department", dept)),
        int(fallback.get("escalate_after_attempts", 3))
    )


def create_ticket(
    employee_id: str,
    location: str,
    issue: str,
    priority: Optional[str] = None,
    service: Optional[str] = None,
    conversation_id: Optional[str] = None,
    attempted_steps: Optional[List[str]] = None,
    outage_ref: Optional[str] = None,
    guide_ref: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    requester_name: Optional[str] = None,
    guest_session_id: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Tuple[TicketResponse, bool]:
    """
    Central ticket-creation gate. All ticket creation paths must call this function.
    Validates required fields before any DB write. Returns (TicketResponse, is_newly_created).
    """
    # 1. Validation gate
    if not employee_id or not employee_id.strip():
        raise ValueError("employee_id is required to create a support ticket.")
    if not location or not location.strip():
        raise ValueError("location is required to route a support ticket.")
    if not issue or not issue.strip():
        raise ValueError("issue description is required to create a support ticket.")

    # Guest sentinel: if employee_id is literally "Guest" and no name was collected, block creation
    is_guest = employee_id.strip().upper() == "GUEST"
    if is_guest and not (requester_name and requester_name.strip()):
        raise ValueError(
            "requester_name is required for guest ticket creation. "
            "Please provide your name so the IT team can follow up with you."
        )

    norm_loc = normalize_location(location)
    norm_srv = normalize_service(service or issue)
    clean_issue = issue.strip()
    norm_attempted = attempted_steps or []
    clean_req_name = (requester_name or "").strip() or None
    clean_guest_sid = (guest_session_id or "").strip() or None

    # Derive idempotency key if not supplied
    clean_key_text = re.sub(r"\W+", " ", clean_issue.lower()).strip()
    # Include guest_session_id in key to scope guest tickets to their session
    id_part = clean_guest_sid or employee_id.strip()
    derived_idempotency_key = idempotency_key or f"{id_part}|{norm_srv}|{clean_key_text}"

    conn = get_connection(db_path)
    cur = conn.cursor()

    # If authenticated employee and requester_name was not explicitly passed, load from users DB table
    if not is_guest and not clean_req_name:
        try:
            cur.execute("SELECT name FROM users WHERE id = ?", (employee_id.strip(),))
            user_r = cur.fetchone()
            if user_r and user_r["name"]:
                clean_req_name = user_r["name"]
        except Exception:
            pass

    # 2. Check for existing ticket via exact idempotency key
    cur.execute("SELECT * FROM tickets WHERE idempotency_key = ?", (derived_idempotency_key,))
    existing_row = cur.fetchone()
    if existing_row:
        conn.close()
        return _row_to_ticket_response(existing_row, db_path), False

    # 3. Check for existing open ticket for same employee/session, service, matching issue
    if clean_guest_sid:
        cur.execute(
            "SELECT * FROM tickets WHERE guest_session_id = ? AND service = ? "
            "AND status != 'Resolved' AND status != 'Closed' ORDER BY created_at DESC",
            (clean_guest_sid, norm_srv)
        )
    else:
        cur.execute(
            "SELECT * FROM tickets WHERE employee_id = ? AND service = ? "
            "AND status != 'Resolved' AND status != 'Closed' ORDER BY created_at DESC",
            (employee_id.strip(), norm_srv)
        )
    open_rows = cur.fetchall()
    for row in open_rows:
        row_issue_clean = re.sub(r"\W+", " ", row["issue"].lower()).strip()
        if row_issue_clean in clean_key_text or clean_key_text in row_issue_clean:
            conn.close()
            return _row_to_ticket_response(row, db_path), False

    # 4. Determine priority and department via server-side escalation rules
    rule_priority, department, _ = evaluate_escalation_rules(norm_srv, clean_issue)
    final_priority = priority if priority else rule_priority

    # 5. Generate unique ticket ID on the server
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    ticket_id = f"IT-{int(time.time() * 1000) % 10000000:07d}"

    display_requester = clean_req_name or employee_id.strip()

    # Atomic insert with concurrency race condition handling
    try:
        cur.execute("""
        INSERT INTO tickets (
            ticket_id, idempotency_key, employee_id, requester_name, guest_session_id,
            location, service, issue,
            priority, status, department, assigned_technician, attempted_steps,
            outage_ref, guide_ref, resolution_notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticket_id, derived_idempotency_key, employee_id.strip(), clean_req_name, clean_guest_sid,
            norm_loc, norm_srv, clean_issue,
            final_priority, "Open", department, None, json.dumps(norm_attempted),
            outage_ref, guide_ref, None, now_str, now_str
        ))

        # Record history
        cur.execute("""
        INSERT INTO ticket_history (ticket_id, action, performed_by, details, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """, (
            ticket_id, "created", display_requester,
            f"Ticket created for {norm_srv} with {final_priority} priority routed to {department}.",
            now_str
        ))

        conn.commit()

        # Retrieve inserted row
        cur.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))
        new_row = cur.fetchone()
        conn.close()

        # Sync to tickets.csv
        try:
            sync_tickets_to_csv(db_path)
        except Exception:
            pass

        log_audit(display_requester, "ticket_created", ticket_id, "SUCCESS", f"Created {ticket_id}", db_path)

        return _row_to_ticket_response(new_row, db_path), True

    except sqlite3.IntegrityError:
        # Race condition: concurrent insert with same idempotency key succeeded first
        cur.execute("SELECT * FROM tickets WHERE idempotency_key = ?", (derived_idempotency_key,))
        existing_concurrent = cur.fetchone()
        conn.close()
        if existing_concurrent:
            return _row_to_ticket_response(existing_concurrent, db_path), False
        raise


def list_tickets(
    employee_id: Optional[str] = None,
    guest_session_id: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    service: Optional[str] = None,
    location: Optional[str] = None,
    search: Optional[str] = None,
    db_path: Optional[Path] = None
) -> List[TicketResponse]:
    """Lists tickets. For guests, scope by guest_session_id to prevent cross-guest leakage."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    query = "SELECT * FROM tickets WHERE 1=1"
    params: List[Any] = []

    # Guest-session scoped query: use session id when available (prevents cross-guest leakage)
    if guest_session_id and guest_session_id.strip():
        query += " AND guest_session_id = ?"
        params.append(guest_session_id.strip())
    elif employee_id:
        query += " AND employee_id = ?"
        params.append(employee_id.strip())

    if status and status.lower() != "all":
        query += " AND LOWER(status) = ?"
        params.append(status.strip().lower())

    if priority and priority.lower() != "all":
        query += " AND LOWER(priority) = ?"
        params.append(priority.strip().lower())

    if service and service.lower() != "all":
        query += " AND service = ?"
        params.append(normalize_service(service))

    if location and location.lower() != "all":
        query += " AND LOWER(location) = ?"
        params.append(location.strip().lower())

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query += " AND (LOWER(ticket_id) LIKE ? OR LOWER(issue) LIKE ? OR LOWER(employee_id) LIKE ?)"
        params.extend([term, term, term])

    query += " ORDER BY created_at DESC"

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    return [_row_to_ticket_response(r, db_path) for r in rows]

def get_ticket(ticket_id: str, db_path: Optional[Path] = None) -> Optional[TicketResponse]:
    """Retrieves full ticket details including history trail."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id.strip(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_ticket_response(row, db_path)

def update_ticket(
    ticket_id: str,
    updates: Dict[str, Any],
    performed_by: str,
    user_role: str,
    db_path: Optional[Path] = None
) -> TicketResponse:
    """
    Updates ticket status, priority, assignment, or notes.
    Enforces role permissions and records full audit trail.
    """
    if user_role.lower() != "it":
        raise PermissionError("Only IT department personnel can update support tickets.")

    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id.strip(),))
    ticket = cur.fetchone()
    if not ticket:
        conn.close()
        raise ValueError(f"Ticket {ticket_id} not found.")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields_to_update = []
    params = []
    changes = []

    # Validate state transitions
    status_map = {
        "open": "Open",
        "in progress": "In progress",
        "in-progress": "In progress",
        "inprogress": "In progress",
        "resolved": "Resolved",
        "closed": "Closed"
    }
    if "status" in updates and updates["status"]:
        s_clean = updates["status"].strip().lower()
        if s_clean not in status_map:
            conn.close()
            raise ValueError(f"Invalid status '{updates['status']}'. Must be one of ['Open', 'In progress', 'Resolved', 'Closed']")
        new_status = status_map[s_clean]
        fields_to_update.append("status = ?")
        params.append(new_status)
        changes.append(f"Status changed from {ticket['status']} to {new_status}")

    priority_map = {
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "critical": "Critical"
    }
    if "priority" in updates and updates["priority"]:
        p_clean = updates["priority"].strip().lower()
        if p_clean not in priority_map:
            conn.close()
            raise ValueError(f"Invalid priority '{updates['priority']}'. Must be one of ['Low', 'Medium', 'High', 'Critical']")
        new_pri = priority_map[p_clean]
        fields_to_update.append("priority = ?")
        params.append(new_pri)
        changes.append(f"Priority changed from {ticket['priority']} to {new_pri}")

    if "assigned_technician" in updates:
        tech = (updates["assigned_technician"] or "").strip()
        fields_to_update.append("assigned_technician = ?")
        params.append(tech)
        changes.append(f"Assigned technician set to '{tech}'")

    if "resolution_notes" in updates:
        res_notes = (updates["resolution_notes"] or "").strip()
        fields_to_update.append("resolution_notes = ?")
        params.append(res_notes)
        if res_notes:
            changes.append(f"Resolution notes added: {res_notes}")

    if not fields_to_update:
        conn.close()
        return _row_to_ticket_response(ticket, db_path)

    fields_to_update.append("updated_at = ?")
    params.append(now_str)
    params.append(ticket_id)

    cur.execute(f"UPDATE tickets SET {', '.join(fields_to_update)} WHERE ticket_id = ?", params)

    for chg in changes:
        cur.execute("""
        INSERT INTO ticket_history (ticket_id, action, performed_by, details, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """, (ticket_id, "updated", performed_by, chg, now_str))

    conn.commit()
    cur.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))
    updated_row = cur.fetchone()
    conn.close()

    try:
        sync_tickets_to_csv(db_path)
    except Exception:
        pass

    log_audit(performed_by, "ticket_updated", ticket_id, "SUCCESS", "; ".join(changes), db_path)

    return _row_to_ticket_response(updated_row, db_path)

def get_dashboard_metrics(db_path: Optional[Path] = None) -> DashboardMetricsResponse:
    """Calculates ticket statistics for the IT Department dashboard."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("SELECT status, service, priority FROM tickets")
    rows = cur.fetchall()
    conn.close()

    total = len(rows)
    open_cnt = 0
    in_prog_cnt = 0
    res_cnt = 0
    by_service: Dict[str, int] = {}
    by_priority: Dict[str, int] = {}

    for r in rows:
        st = (r["status"] or "").lower()
        if st == "open":
            open_cnt += 1
        elif st in ["in progress", "in-progress"]:
            in_prog_cnt += 1
        elif st in ["resolved", "closed"]:
            res_cnt += 1

        srv = r["service"] or "Other"
        by_service[srv] = by_service.get(srv, 0) + 1

        pri = r["priority"] or "Medium"
        by_priority[pri] = by_priority.get(pri, 0) + 1

    # Check active outages
    from backend.services.outage_service import check_network_status
    active_outages = 0
    outage_file = settings.OUTAGES_FILE
    if outage_file.exists():
        try:
            import csv
            with open(outage_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if (r.get("status") or "").strip().lower() == "active":
                        active_outages += 1
        except Exception:
            pass

    return DashboardMetricsResponse(
        total_tickets=total,
        open_tickets=open_cnt,
        in_progress_tickets=in_prog_cnt,
        resolved_tickets=res_cnt,
        active_outages=active_outages,
        tickets_by_service=by_service,
        tickets_by_priority=by_priority
    )

def _row_to_ticket_response(row: Any, db_path: Optional[Path] = None) -> TicketResponse:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, ticket_id, action, performed_by, details, timestamp FROM ticket_history WHERE ticket_id = ? ORDER BY id ASC", (row["ticket_id"],))
    hist_rows = cur.fetchall()
    conn.close()

    history = [
        TicketHistoryEntry(
            id=h["id"],
            ticket_id=h["ticket_id"],
            action=h["action"],
            performed_by=h["performed_by"],
            details=h["details"],
            timestamp=h["timestamp"]
        ) for h in hist_rows
    ]

    attempted = []
    if row["attempted_steps"]:
        try:
            attempted = json.loads(row["attempted_steps"])
        except Exception:
            attempted = []

    # Safe column access for new columns that may not exist in old rows
    def _safe(key, default=None):
        try:
            return row[key]
        except (IndexError, KeyError):
            return default

    return TicketResponse(
        ticket_id=row["ticket_id"],
        employee_id=row["employee_id"],
        requester_name=_safe("requester_name"),
        guest_session_id=_safe("guest_session_id"),
        location=row["location"],
        service=row["service"],
        issue=row["issue"],
        priority=row["priority"],
        status=row["status"],
        department=row["department"],
        assigned_technician=row["assigned_technician"],
        attempted_steps=attempted,
        outage_ref=row["outage_ref"],
        guide_ref=row["guide_ref"],
        resolution_notes=row["resolution_notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        history=history
    )
