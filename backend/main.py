import os
import json
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header, Depends, Query, Body, status as http_status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from backend.config import settings
from backend.database import init_db, get_connection, log_audit, check_db_health
from backend.models import (
    LoginRequest, UserResponse,
    ChatMessageRequest, ChatMessageResponse,
    TicketCreateRequest, TicketUpdateRequest, TicketResponse,
    OutageRecord, OutageUpdateRequest, HistoryItem, DashboardMetricsResponse,
    GuideApproveRequest
)
from backend.services.outage_service import (
    check_network_status, list_all_outages, upsert_outage, toggle_outage_status
)
from backend.services.history_service import search_incident_history
from backend.services.guide_indexer import guide_indexer
from backend.services.retrieval_service import retrieve_troubleshooting_steps
from backend.services.ticket_service import (
    create_ticket, list_tickets, get_ticket, update_ticket, get_dashboard_metrics
)
from backend.services.agent_service import agent_service
from backend.auth_service import (
    authenticate_user,
    get_current_user_optional,
    get_authenticated_user,
    require_it_role,
    require_employee_or_it
)

# Initialize Database and Guide Index on module load
init_db()
guide_indexer.load_index()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Database and Guide Index
    init_db()
    guide_indexer.build_and_save_index()
    yield

app = FastAPI(
    title="ResolveIT — IT Incident Resolution Advisory Agent",
    description="Backend API and Advisory Engine for IT Incident Resolution",
    version=settings.VERSION,
    lifespan=lifespan
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_origin_regex=r"https?://.*\.vercel\.app",  # Allow all Vercel preview deployments
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Health & Status Endpoints -----------------

@app.get("/api/health", tags=["System"])
def health_check():
    """Returns application health with live active database connectivity verification."""
    is_db_connected = check_db_health()
    status_str = "healthy" if is_db_connected else "degraded"
    db_str = "connected" if is_db_connected else "disconnected"

    return {
        "status": status_str,
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "database": db_str,
        "indexed_chunks": len(guide_indexer.index),
        "llm_provider": settings.LLM_PROVIDER
    }

# ----------------- Authentication Endpoints -----------------

@app.post("/api/auth/login", response_model=UserResponse, tags=["Authentication"])
def login(req: LoginRequest):
    """
    Validates user credentials using Argon2id, enforces portal separation,
    applies rate limiting, and returns verified user principal with signed session JWT.
    """
    user = authenticate_user(req)
    if not user:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Login failed. Invalid employee credentials or unauthorized portal selection."
        )
    return user

# ----------------- Conversational Agent Endpoints -----------------

@app.post("/api/chat/message", response_model=ChatMessageResponse, tags=["Agent"])
def send_chat_message(
    req: ChatMessageRequest,
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """
    Main advisory agent chat endpoint.
    Enforces conversation state isolation, input bounds, and authenticated caller verification.
    """
    # 1. Input validation & size limits
    clean_msg = req.message.strip()
    if not clean_msg:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Message cannot be empty.")
    if len(req.message) > 4000:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Message exceeds maximum allowed length.")

    # 2. Conversation isolation (IDOR protection)
    if req.conversation_id:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT employee_id, guest_session_id FROM conversations WHERE conversation_id = ?", (req.conversation_id,))
        conv = cur.fetchone()
        conn.close()

        if conv:
            conv_emp = conv["employee_id"]
            conv_gsid = conv["guest_session_id"]

            if conv_emp and conv_emp.upper() != "GUEST":
                # Stored conversation belongs to an employee
                if not current_user:
                    raise HTTPException(
                        status_code=http_status.HTTP_401_UNAUTHORIZED,
                        detail="Authentication required to access this employee conversation."
                    )
                if current_user.role.lower() != "it" and conv_emp != current_user.id:
                    raise HTTPException(
                        status_code=http_status.HTTP_403_FORBIDDEN,
                        detail="Forbidden: Cannot access or modify another employee's conversation."
                    )
            elif conv_gsid:
                # Stored conversation belongs to a guest session
                if req.guest_session_id and conv_gsid != req.guest_session_id.strip():
                    raise HTTPException(
                        status_code=http_status.HTTP_403_FORBIDDEN,
                        detail="Forbidden: Guest session mismatch for this conversation."
                    )

    # 3. Derive verified caller identity
    emp_id = current_user.id if current_user else req.employee_id
    emp_loc = req.location
    emp_name = (current_user.name if current_user else None) or req.requester_name

    try:
        response = agent_service.process_message(
            message=clean_msg,
            conversation_id=req.conversation_id,
            employee_id=emp_id,
            requester_name=emp_name,
            guest_session_id=req.guest_session_id,
            location=emp_loc,
            service=req.service,
            os_name=req.os_name,
            attempted_from_client=req.attempted_steps
        )
        return response
    except Exception as e:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while processing your message: {str(e)}"
        )

@app.post("/api/chat/reset", tags=["Agent"])
def reset_chat(
    conversation_id: Optional[str] = Query(None),
    body: Optional[dict] = Body(None),
    current_user: Optional[UserResponse] = Depends(get_current_user_optional),
    guest_session_id: Optional[str] = Query(None)
):
    """
    Resets conversation state.
    Enforces that only the verified conversation owner or IT staff can reset a conversation.
    """
    cid = conversation_id
    if not cid and body:
        cid = body.get("conversation_id") or body.get("session_id")
    if not cid or not cid.strip():
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="conversation_id or session_id is required to reset conversation."
        )
    cid = cid.strip()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT employee_id, guest_session_id FROM conversations WHERE conversation_id = ?", (cid,))
    conv = cur.fetchone()

    if not conv:
        conn.close()
        return {"status": "success", "message": f"Conversation {cid} reset."}

    conv_emp = conv["employee_id"]
    conv_gsid = conv["guest_session_id"]

    # Verify ownership
    if conv_emp and conv_emp.upper() != "GUEST":
        if not current_user:
            conn.close()
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required to reset employee conversation."
            )
        if current_user.role.lower() != "it" and conv_emp != current_user.id:
            conn.close()
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You cannot reset another user's conversation."
            )
    elif conv_gsid:
        if guest_session_id and conv_gsid != guest_session_id.strip():
            conn.close()
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Guest session does not match this conversation."
            )

    cur.execute("DELETE FROM conversations WHERE conversation_id = ?", (cid,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Conversation {cid} reset."}

# ----------------- Outage Checking Endpoints -----------------

@app.get("/api/outages", response_model=OutageRecord, tags=["Outages"])
def get_outage_status(
    location: Optional[str] = Query(None, description="Employee office location"),
    service: Optional[str] = Query("Other", description="Affected IT service")
):
    return check_network_status(location, service)

@app.get("/api/outages/all", tags=["Outages"])
def get_all_outages():
    """Returns all recorded outages from data/outages.csv."""
    return list_all_outages()

@app.post("/api/it/outages", tags=["Outages"])
def create_or_update_outage(
    req: OutageUpdateRequest,
    it_user: UserResponse = Depends(require_it_role)
):
    """Create or update an outage record. Restricted strictly to IT personnel."""
    try:
        rec = upsert_outage(
            location=req.location,
            service=req.service,
            status=req.status,
            update_text=req.update_text,
            outage_ref=req.outage_ref
        )
        log_audit(it_user.id, "outage_update", rec.get("outage_ref", "unknown"), "SUCCESS", f"{rec.get('service')} in {rec.get('location')} -> {rec.get('status')}")
        return rec
    except Exception as e:
        log_audit(it_user.id, "outage_update", req.outage_ref or "new", "FAILED", str(e))
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=f"Failed to update outage: {str(e)}")

@app.post("/api/it/outages/{outage_ref}/toggle", tags=["Outages"])
def toggle_outage(
    outage_ref: str,
    it_user: UserResponse = Depends(require_it_role)
):
    """Toggle an outage between Active and Resolved. Restricted strictly to IT personnel."""
    rec = toggle_outage_status(outage_ref)
    if not rec:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Outage {outage_ref} not found.")
    log_audit(it_user.id, "outage_toggle", outage_ref, "SUCCESS", f"Status changed to {rec.get('status')}")
    return rec

# ----------------- Incident History Endpoints -----------------

@app.get("/api/history", response_model=List[HistoryItem], tags=["History"])
def get_incident_history(
    location: Optional[str] = Query(None),
    service: Optional[str] = Query(None),
    symptom: Optional[str] = Query(None)
):
    return search_incident_history(location, service, symptom)

# ----------------- Guide Retrieval Endpoints -----------------

@app.get("/api/guides", tags=["Guides"])
def get_guides_status():
    approved_info = guide_indexer.get_approved_guide_info()
    meta = guide_indexer.get_index_metadata()
    active_guide = guide_indexer.get_active_guide()

    # Discover document files in guides directory
    guides_dir = guide_indexer.guides_dir
    avail_files = []
    if guides_dir.exists():
        avail_files = sorted([
            f.name for f in guides_dir.glob("*")
            if f.suffix.lower() in [".md", ".pdf", ".docx", ".txt"] and f.is_file()
        ])

    # Backward compatibility for frontend
    source_files = [meta.get("source_filename")] if meta.get("source_filename") else []
    approved_info["source_files"] = source_files
    approved_info["topics"] = list(set(c.get("topic") for c in guide_indexer.index if c.get("topic")))

    return {
        "status": "success",
        "approved_info": approved_info,
        "registered_guides": approved_info.get("guides", []),
        "available_files": avail_files,
        "active_guide_id": active_guide.get("id") if active_guide else None,
        "active_guide_version": active_guide.get("version") if active_guide else None,
        "source_filename": active_guide.get("filename") if active_guide else None,
        "total_chunks": len(guide_indexer.index),
        "indexing_warnings": meta.get("warnings", []),
        "pending_review_guides": guide_indexer.get_pending_review_guides()
    }

@app.post("/api/guides/{guide_id}/activate", tags=["Guides"])
def activate_existing_guide(
    guide_id: str,
    it_user: UserResponse = Depends(require_it_role)
):
    """Activates an already registered guide by ID."""
    prev_info = guide_indexer.get_approved_guide_info()
    guides = list(prev_info.get("guides", []))
    target = next((g for g in guides if g.get("id") == guide_id), None)
    if not target:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Guide {guide_id} not found in registered guides.")

    for g in guides:
        g["status"] = "Active" if g.get("id") == guide_id else "Inactive"

    new_registry = {
        "active_guide_id": guide_id,
        "guides": guides
    }

    guide_indexer.approved_file.write_text(json.dumps(new_registry, indent=2), encoding="utf-8")
    result = guide_indexer.build_and_save_index()
    log_audit(it_user.id, "guide_activate", guide_id, "SUCCESS", f"Activated guide {guide_id}")
    return {"status": "success", "active_guide_id": guide_id, "result": result}

@app.post("/api/guides/reindex", tags=["Guides"])
def reindex_guides(
    it_user: UserResponse = Depends(require_it_role)
):
    """Administrative guide re-indexing restricted strictly to server-verified IT staff."""
    result = guide_indexer.build_and_save_index()
    if result.get("status") == "error":
        log_audit(it_user.id, "guide_reindex", "guide_index.json", "FAILED", result.get("error", "Re-indexing failed"))
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=result.get("error", "Failed to build guide index.")
        )
    log_audit(it_user.id, "guide_reindex", "guide_index.json", "SUCCESS", f"Indexed {result['indexed_chunks']} chunks from {result.get('source_filename')}")
    return result

@app.post("/api/guides/approve", tags=["Guides"])
def approve_guide(
    req: GuideApproveRequest,
    it_user: UserResponse = Depends(require_it_role)
):
    """Administrative guide approval restricted strictly to server-verified IT staff."""
    # Path traversal validation
    clean_filename = req.filename.strip()
    if "/" in clean_filename or "\\" in clean_filename or ".." in clean_filename:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename: path traversal sequences are strictly forbidden."
        )

    guide_file = guide_indexer.guides_dir / clean_filename
    if not guide_file.exists():
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Guide file '{clean_filename}' not found in guides directory."
        )

    prev_info = guide_indexer.get_approved_guide_info()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    guides = list(prev_info.get("guides", []))
    found = False
    for g in guides:
        if g.get("id") == req.guide_id or g.get("filename") == clean_filename:
            g["id"] = req.guide_id
            g["name"] = req.name or g.get("name", clean_filename)
            g["filename"] = clean_filename
            g["version"] = req.version
            g["status"] = "Active"
            g["approved_by"] = it_user.id
            g["approved_at"] = now_str
            g["services"] = req.services or g.get("services", ["Wi-Fi", "Printer", "Email"])
            g["supported_os"] = req.supported_os or g.get("supported_os", ["Windows", "macOS", "All"])
            found = True
            break

    if not found:
        guides.append({
            "id": req.guide_id,
            "name": req.name or clean_filename,
            "filename": clean_filename,
            "version": req.version,
            "status": "Active",
            "approved_by": it_user.id,
            "approved_at": now_str,
            "services": req.services or ["Wi-Fi", "Printer", "Email"],
            "supported_os": req.supported_os or ["Windows", "macOS", "All"]
        })

    # Deactivate other guides
    for g in guides:
        if g.get("id") != req.guide_id and g.get("status") == "Active":
            g["status"] = "Inactive"

    new_registry = {
        "active_guide_id": req.guide_id,
        "guides": guides
    }

    guide_indexer.approved_file.write_text(json.dumps(new_registry, indent=2), encoding="utf-8")
    result = guide_indexer.build_and_save_index()

    if result.get("status") == "error":
        guide_indexer.approved_file.write_text(json.dumps(prev_info, indent=2), encoding="utf-8")
        guide_indexer.load_index()
        log_audit(it_user.id, "guide_approve", clean_filename, "FAILED", f"Approval rolled back: {result.get('error')}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Approved guide could not be indexed: {result.get('error')}. Registry changes were rolled back."
        )

    log_audit(it_user.id, "guide_approve", clean_filename, "SUCCESS", f"Approved {req.guide_id} v{req.version} ({result['indexed_chunks']} chunks)")
    return {
        "status": "success",
        "message": f"Guide '{req.guide_id}' v{req.version} approved and indexed successfully.",
        "active_guide_id": req.guide_id,
        "active_guide_version": req.version,
        "source_filename": clean_filename,
        "indexed_chunks": result["indexed_chunks"],
        "source_files": result["source_files"]
    }

# ----------------- Ticket Management Endpoints -----------------

@app.get("/api/tickets", response_model=List[TicketResponse], tags=["Tickets"])
def get_tickets(
    employee_id: Optional[str] = Query(None),
    guest_session_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    service: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """
    List tickets with server-enforced role and identity authorization.
    IT staff may view all tickets; employees may only view their own tickets;
    guests may only view tickets matching their verified session ID.
    """
    if current_user:
        if current_user.role.lower() == "it":
            return list_tickets(employee_id, guest_session_id, status, priority, service, location, search)
        elif current_user.role.lower() == "employee":
            if employee_id and employee_id.strip() != current_user.id:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Forbidden: Employees are not authorized to view other employees' tickets."
                )
            return list_tickets(current_user.id, None, status, priority, service, location, search)

    # Unauthenticated / Guest caller
    if employee_id:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to view employee tickets."
        )
    if not guest_session_id or not guest_session_id.strip():
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Authentication or a valid guest session ID is required to list tickets."
        )
    return list_tickets(None, guest_session_id.strip(), status, priority, service, location, search)

@app.post("/api/tickets", response_model=TicketResponse, status_code=http_status.HTTP_201_CREATED, tags=["Tickets"])
def create_support_ticket(
    req: TicketCreateRequest,
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """
    Create a support ticket.
    Validates authenticated ownership so employees cannot create tickets for others.
    """
    if current_user:
        if current_user.role.lower() == "employee":
            if req.employee_id.strip() != current_user.id:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Forbidden: Employees cannot create tickets on behalf of other employees."
                )
    else:
        # Unauthenticated / guest caller
        if req.employee_id.strip().upper() != "GUEST":
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required to create tickets under an employee ID."
            )
        if not req.requester_name or not req.requester_name.strip():
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="Requester name is required for guest tickets."
            )
        if not req.guest_session_id or not req.guest_session_id.strip():
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="Guest session ID is required for guest tickets."
            )

    try:
        ticket, is_new = create_ticket(
            employee_id=req.employee_id,
            location=req.location,
            issue=req.issue,
            priority=req.priority,
            service=req.service,
            conversation_id=req.conversation_id,
            attempted_steps=req.attempted_steps,
            outage_ref=req.outage_ref,
            guide_ref=req.guide_ref,
            idempotency_key=req.idempotency_key,
            requester_name=req.requester_name,
            guest_session_id=req.guest_session_id
        )
        return ticket
    except ValueError as ve:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Ticket creation failed: {str(e)}")

@app.get("/api/tickets/{ticket_id}", response_model=TicketResponse, tags=["Tickets"])
def get_ticket_by_id(
    ticket_id: str,
    guest_session_id: Optional[str] = Query(None),
    current_user: Optional[UserResponse] = Depends(get_current_user_optional)
):
    """
    Retrieve single ticket with IDOR prevention.
    Verifies caller owns the ticket or possesses IT administrative authorization.
    """
    clean_tid = ticket_id.strip()
    t = get_ticket(clean_tid)
    if not t:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Ticket {clean_tid} not found.")

    if current_user:
        if current_user.role.lower() == "it":
            return t
        elif current_user.role.lower() == "employee":
            if t.employee_id != current_user.id:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Forbidden: You are not authorized to access this ticket."
                )
            return t

    # Unauthenticated / guest access check
    if t.guest_session_id and guest_session_id and t.guest_session_id == guest_session_id.strip():
        return t

    raise HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required to view this ticket."
    )

@app.patch("/api/tickets/{ticket_id}", response_model=TicketResponse, tags=["Tickets"])
def update_support_ticket(
    ticket_id: str,
    req: TicketUpdateRequest,
    it_user: UserResponse = Depends(require_it_role)
):
    """
    Update ticket status, priority, assignment, or resolution notes.
    Restricted strictly to server-verified IT staff.
    """
    try:
        updated = update_ticket(
            ticket_id=ticket_id,
            updates=req.model_dump(exclude_unset=True),
            performed_by=it_user.id,
            user_role=it_user.role
        )
        return updated
    except PermissionError as pe:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail=str(pe))
    except ValueError as ve:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(ve))

# ----------------- Dashboard & Audit Endpoints -----------------

@app.get("/api/dashboard/metrics", response_model=DashboardMetricsResponse, tags=["Dashboard"])
def get_metrics(it_user: UserResponse = Depends(require_it_role)):
    """Metrics restricted strictly to server-verified IT staff."""
    return get_dashboard_metrics()

@app.get("/api/audit-logs", tags=["Dashboard"])
def get_audit_trail(it_user: UserResponse = Depends(require_it_role)):
    """System audit logs restricted strictly to server-verified IT staff."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ----------------- Static Frontend Hosting -----------------

# Employee portal
UI_DIR = settings.BASE_DIR / "it_agent_ui"
if UI_DIR.exists():
    app.mount("/it_agent_ui", StaticFiles(directory=str(UI_DIR), html=True), name="it_agent_ui")

# IT Department Command Center (separate portal with its own UI)
IT_DEPT_DIR = settings.BASE_DIR / "it_department"
if IT_DEPT_DIR.exists():
    app.mount("/it_department", StaticFiles(directory=str(IT_DEPT_DIR), html=True), name="it_department")

@app.get("/", include_in_schema=False)
def serve_root():
    return RedirectResponse(url="/it_agent_ui/")

@app.get("/it", include_in_schema=False)
def serve_it_portal():
    return RedirectResponse(url="/it_department/")
