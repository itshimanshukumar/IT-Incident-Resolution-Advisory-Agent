import re

from agent import tools
from agent.retriever import retrieve_troubleshooting_steps


def normalize_text(text):
    """Normalize text for comparing troubleshooting steps."""
    return re.sub(r"\s+", " ", str(text).lower()).strip()


def extract_steps(results):
    """Extract numbered instructions from retrieved guide sections."""
    steps = []

    for result in results:
        text = result.get("text", "")

        for line in text.splitlines():
            line = line.strip()

            match = re.match(r"^\s*(?:\d+[\.\)]|[-*])\s+(.+)$", line)
            if match:
                step = match.group(1).strip()
                if step and step not in steps:
                    steps.append(step)

    return steps


def is_attempted(step, attempted_steps):
    """Avoid recommending an instruction the employee already tried."""
    normalized_step = normalize_text(step)

    for attempted in attempted_steps:
        normalized_attempt = normalize_text(attempted)

        if not normalized_attempt:
            continue

        if (
            normalized_step == normalized_attempt
            or normalized_step in normalized_attempt
            or normalized_attempt in normalized_step
        ):
            return True

    return False


def resolve_incident(
    employee_id,
    location,
    service,
    operating_system,
    symptom,
    attempted_steps=None,
):
    """Coordinate outage checks, retrieval, guidance, and escalation."""

    attempted_steps = attempted_steps or []

    # Validate essential input before calling the tools.
    required = {
        "employee_id": employee_id,
        "location": location,
        "service": service,
        "operating_system": operating_system,
        "symptom": symptom,
    }

    missing = [
        name for name, value in required.items()
        if not isinstance(value, str) or not value.strip()
    ]

    if missing:
        return {
            "status": "validation_error",
            "message": "Required fields are missing or blank.",
            "missing_fields": missing,
        }

    issue = f"{service}: {symptom}"

    # 1. Always check the recorded outage status first.
    outage = tools.check_network_status(location, service)
    outage_status = outage.get("status", "unknown")
    freshness = outage.get("freshness", "unknown")

    if outage_status == "active" and freshness == "fresh":
        return {
            "status": "active_outage",
            "message": (
                "A currently recorded active outage matches this "
                "location and service. Follow the outage update and "
                "avoid repeating local troubleshooting unnecessarily."
            ),
            "outage": outage,
            "recommended_steps": [],
            "ticket": None,
        }

    # 2. Look for similar historical incidents.
    history = tools.search_incident_history(
        symptom=symptom,
        location=location,
        service=service,
        os=operating_system,
        limit=5,
    )

    # 3. Retrieve guidance from the IT guide.
    retrieval = retrieve_troubleshooting_steps(
        service=service,
        operating_system=operating_system,
        symptom=symptom,
        top_k=5,
    )

    # 4. Extract steps and filter previously attempted instructions.
    guide_results = retrieval.get("results", [])
    all_steps = extract_steps(guide_results)

    recommended_steps = [
        step for step in all_steps
        if not is_attempted(step, attempted_steps)
    ]

    # 5. Apply service escalation rules.
    rule = tools.get_escalation_rule(service=service, symptom=symptom)
    escalation_limit = rule.get("escalate_after_attempts", 2)
    priority = rule.get("default_priority", "Low")

    ticket_result = None

    should_escalate = (
        len(attempted_steps) >= escalation_limit
        or retrieval.get("status") != "found"
        or (bool(all_steps) and not recommended_steps)
    )

    if should_escalate:
        ticket_result = tools.create_ticket(
            employee_id=employee_id,
            location=location,
            issue=issue,
            priority=priority,
            service=service,
        )

        return {
            "status": "escalated",
            "message": (
                "The issue needs IT support. A ticket was requested "
                "because the attempt limit was reached or useful "
                "guide instructions were unavailable."
            ),
            "outage": outage,
            "outage_warning": (
                "The outage record is stale or its status is uncertain."
                if freshness == "stale" or outage_status == "unknown"
                else None
            ),
            "history": history,
            "retrieval": retrieval,
            "recommended_steps": recommended_steps,
            "ticket": ticket_result,
        }

    # 6. Return next steps without repeating attempted instructions.
    return {
        "status": "guidance",
        "message": (
            "Follow the suggested steps and report which ones were "
            "completed and whether the issue remains."
        ),
        "outage": outage,
        "outage_warning": (
            "The outage record is stale or its status is uncertain. "
            "Verify service status with IT before relying on it."
            if freshness == "stale" or outage_status == "unknown"
            else None
        ),
        "history": history,
        "retrieval": retrieval,
        "recommended_steps": recommended_steps,
        "ticket": None,
    }