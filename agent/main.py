
from fastapi import FastAPI
from pydantic import BaseModel, Field

from agent.tools import (
    check_network_status,
    search_incident_history,
    get_escalation_rule,
    create_ticket,
)

from agent.retriever import retrieve_troubleshooting_steps
from agent.agent import resolve_incident

from agent.sessions import (
    create_session,
    get_session,
    record_attempt,
    update_session_status,
)


app = FastAPI(
    title="IT Incident Resolution Advisory Agent",
    description=(
        "IT outage checks, incident history, hybrid troubleshooting "
        "retrieval, incident resolution, persistent sessions, "
        "and ticket management."
    ),
    version="1.5.0",
)



# REQUEST MODELS


class OutageRequest(BaseModel):
    location: str = Field(min_length=1)
    service: str = Field(default="Wi-Fi", min_length=1)


class IncidentSearchRequest(BaseModel):
    symptom: str = Field(min_length=1)
    location: str = ""
    service: str = ""
    operating_system: str = ""
    limit: int = Field(default=5, ge=1, le=20)


class EscalationRuleRequest(BaseModel):
    service: str = Field(min_length=1)
    symptom: str = ""


class TicketRequest(BaseModel):
    employee_id: str = Field(min_length=1)
    location: str = Field(min_length=1)
    issue: str = Field(min_length=1)
    service: str = Field(default="Wi-Fi", min_length=1)
    priority: str = ""


class TroubleshootingRequest(BaseModel):
    service: str = Field(min_length=1)
    operating_system: str = Field(min_length=1)
    symptom: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)


class ResolveIncidentRequest(BaseModel):
    employee_id: str = Field(min_length=1)
    location: str = Field(min_length=1)
    service: str = Field(min_length=1)
    operating_system: str = Field(min_length=1)
    symptom: str = Field(min_length=1)
    attempted_steps: list[str] = Field(default_factory=list)


class CreateSessionRequest(BaseModel):
    employee_id: str = Field(min_length=1)
    location: str = Field(min_length=1)
    service: str = Field(min_length=1)
    operating_system: str = Field(min_length=1)
    symptom: str = Field(min_length=1)


class RecordAttemptRequest(BaseModel):
    step: str = Field(min_length=1)


class SessionStatusRequest(BaseModel):
    status: str = Field(min_length=1)
    ticket_id: str | None = None



# HOME AND HEALTH



@app.get("/")
def home():
    return {
        "message": "Welcome to the IT Incident Resolution Advisory Agent",
        "version": "1.5.0",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "application": "IT Incident Resolution Advisory Agent",
        "version": "1.5.0",
    }



# PHASE 1: OUTAGE CHECK



@app.post("/check-outage")
def check_outage(request: OutageRequest):
    return check_network_status(
        location=request.location,
        service=request.service,
    )



# PHASE 2: INCIDENT HISTORY



@app.post("/search-incidents")
def search_incidents(request: IncidentSearchRequest):
    return search_incident_history(
        symptom=request.symptom,
        location=request.location,
        service=request.service,
        os=request.operating_system,
        limit=request.limit,
    )



#  ESCALATION RULES



@app.post("/escalation-rule")
def escalation_rule(request: EscalationRuleRequest):
    return get_escalation_rule(
        service=request.service,
        symptom=request.symptom,
    )



#  TICKET CREATION



@app.post("/tickets")
def tickets(request: TicketRequest):
    return create_ticket(
        employee_id=request.employee_id,
        location=request.location,
        issue=request.issue,
        service=request.service,
        priority=request.priority,
    )



# PHASE 3: HYBRID RETRIEVAL



@app.post("/troubleshooting")
def get_troubleshooting(request: TroubleshootingRequest):
    return retrieve_troubleshooting_steps(
        service=request.service,
        operating_system=request.operating_system,
        symptom=request.symptom,
        top_k=request.top_k,
    )



# PHASE 4: INCIDENT RESOLUTION AGENT



@app.post("/resolve-incident")
def resolve_incident_endpoint(request: ResolveIncidentRequest):
    return resolve_incident(
        employee_id=request.employee_id,
        location=request.location,
        service=request.service,
        operating_system=request.operating_system,
        symptom=request.symptom,
        attempted_steps=request.attempted_steps,
    )



# PHASE 5: CREATE PERSISTENT SESSION



@app.post("/sessions")
def start_session(request: CreateSessionRequest):
    return create_session(
        employee_id=request.employee_id,
        location=request.location,
        service=request.service,
        operating_system=request.operating_system,
        symptom=request.symptom,
    )



#  GET SAVED SESSION



@app.get("/sessions/{session_id}")
def read_session(session_id: str):
    session = get_session(session_id)

    if session is None:
        return {
            "status": "not_found",
            "message": "Session not found.",
        }

    return {
        "status": "found",
        "session": session,
    }



#  RECORD COMPLETED TROUBLESHOOTING STEP



@app.post("/sessions/{session_id}/attempts")
def add_session_attempt(
    session_id: str,
    request: RecordAttemptRequest,
):
    return record_attempt(
        session_id=session_id,
        step=request.step,
    )



#  UPDATE SESSION STATUS



@app.patch("/sessions/{session_id}/status")
def change_session_status(
    session_id: str,
    request: SessionStatusRequest,
):
    return update_session_status(
        session_id=session_id,
        status=request.status,
        ticket_id=request.ticket_id,
    )



# RESOLVE A SAVED SESSION



@app.post("/sessions/{session_id}/resolve")
def resolve_saved_session(session_id: str):
    session = get_session(session_id)

    if session is None:
        return {
            "status": "not_found",
            "message": "Session not found.",
        }

    if session["status"] == "Resolved":
        return {
            "status": "already_resolved",
            "session": session,
        }

    result = resolve_incident(
        employee_id=session["employee_id"],
        location=session["location"],
        service=session["service"],
        operating_system=session["operating_system"],
        symptom=session["symptom"],
        attempted_steps=session["attempted_steps"],
    )

    if result.get("status") == "escalated":
        ticket_result = result.get("ticket") or {}
        ticket = ticket_result.get("ticket") or {}
        ticket_id = ticket.get("ticket_id")

        update_session_status(
            session_id=session_id,
            status="Escalated",
            ticket_id=ticket_id,
        )
    else:
        update_session_status(
            session_id=session_id,
            status="In Progress",
        )

    return {
        "status": "processed",
        "session": get_session(session_id),
        "resolution": result,
    }


# ---------------------------------------------------------------------------
# ResolveIT UI compatibility layer
#
# The original /check-outage, /search-incidents, /tickets, /troubleshooting,
# /resolve-incident, and /sessions endpoints above remain unchanged. The UI
# bundle has a separate /api/* contract (authentication, chat, dashboards,
# ticket administration, outages, and guide management). Reuse its validated
# handlers and services as additional routes on this same FastAPI application.
# UI-specific persistence is isolated in data/ui_data to avoid changing the
# original agent's CSV files or ticket/session behavior.
# ---------------------------------------------------------------------------

from pathlib import Path as _Path
from fastapi.responses import RedirectResponse as _RedirectResponse
from fastapi.staticfiles import StaticFiles as _StaticFiles

# Importing this module initializes the UI-specific SQLite schema and guide
# index. Only its /api/* routes are attached; its root/docs/static routes are
# deliberately excluded so the existing application routes remain authoritative.
from backend.main import app as _resolveit_ui_api_app

for _ui_route in _resolveit_ui_api_app.router.routes:
    if getattr(_ui_route, "path", "").startswith("/api/"):
        app.router.routes.append(_ui_route)

_PROJECT_ROOT = _Path(__file__).resolve().parent.parent
_EMPLOYEE_UI_DIR = _PROJECT_ROOT / "employee ui"
_IT_DEPARTMENT_UI_DIR = _PROJECT_ROOT / "it_department"

if _EMPLOYEE_UI_DIR.is_dir():
    app.mount(
        "/employee-ui",
        _StaticFiles(directory=str(_EMPLOYEE_UI_DIR), html=True),
        name="employee_ui",
    )

if _IT_DEPARTMENT_UI_DIR.is_dir():
    app.mount(
        "/it_department",
        _StaticFiles(directory=str(_IT_DEPARTMENT_UI_DIR), html=True),
        name="it_department_ui",
    )


@app.get("/employee", include_in_schema=False)
def employee_portal_entry():
    return _RedirectResponse(url="/employee-ui/", status_code=307)


@app.get("/it", include_in_schema=False)
def it_portal_entry():
    return _RedirectResponse(url="/it_department/", status_code=307)
