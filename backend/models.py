from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

# Authentication
class LoginRequest(BaseModel):
    employee_id: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    portal: str = Field("employee", max_length=32)

class UserResponse(BaseModel):
    id: str
    name: str
    department: str
    location: str
    role: str
    access_token: Optional[str] = None
    token_type: Optional[str] = "bearer"

# Outage Models
class OutageRecord(BaseModel):
    outage_ref: Optional[str] = None
    location: str
    service: str
    status: str  # 'Active', 'Resolved', 'Unknown', 'Error'
    update_text: Optional[str] = None
    last_updated: Optional[str] = None
    is_stale: bool = False
    warning: Optional[str] = None
    message: str
    lookup_timestamp: str

class OutageUpdateRequest(BaseModel):
    outage_ref: Optional[str] = Field(None, max_length=64)
    location: str = Field(..., min_length=1, max_length=64)
    service: str = Field(..., min_length=1, max_length=64)
    status: str = Field("Active", max_length=32)
    update_text: str = Field(..., min_length=1, max_length=2000)

# History Models
class HistoryItem(BaseModel):
    incident_id: str
    location: str
    service: str
    symptom: str
    resolution: str
    status: str
    created_at: str

class GuideStep(BaseModel):
    step_id: str = ""
    step_number: int
    title: Optional[str] = None
    instruction: str
    expected_observation: Optional[str] = None
    os_target: Optional[str] = "All"
    source_document: Optional[str] = None
    source_section: Optional[str] = None
    source_reference: Optional[str] = None
    guide_id: Optional[str] = None
    guide_version: Optional[str] = None
    status: str = "suggested"  # 'suggested', 'attempted', 'completed', 'failed', 'skipped'

class GuideRetrievalResult(BaseModel):
    service: str
    symptom: str
    matched_topic: Optional[str] = None
    source_document: Optional[str] = None
    guide_id: Optional[str] = None
    guide_version: Optional[str] = None
    steps: List[GuideStep] = []
    is_relevant: bool = True
    explanation: Optional[str] = None

class GuideApproveRequest(BaseModel):
    guide_id: str = Field(..., min_length=1, max_length=64)
    filename: str = Field(..., min_length=1, max_length=128)
    version: str = Field(..., min_length=1, max_length=32)
    name: Optional[str] = Field(None, max_length=128)
    services: Optional[List[str]] = ["Wi-Fi", "Printer", "Email"]
    supported_os: Optional[List[str]] = ["Windows", "macOS", "All"]

# Ticket Models
class TicketCreateRequest(BaseModel):
    employee_id: str = Field(..., min_length=1, max_length=64)
    requester_name: Optional[str] = Field(None, max_length=128)
    guest_session_id: Optional[str] = Field(None, max_length=128)
    location: str = Field(..., min_length=1, max_length=64)
    issue: str = Field(..., min_length=1, max_length=2000)
    service: Optional[str] = Field("Other", max_length=64)
    priority: Optional[str] = Field(None, max_length=32)
    conversation_id: Optional[str] = Field(None, max_length=128)
    attempted_steps: Optional[List[str]] = []
    outage_ref: Optional[str] = Field(None, max_length=64)
    guide_ref: Optional[str] = Field(None, max_length=128)
    idempotency_key: Optional[str] = Field(None, max_length=256)

class TicketUpdateRequest(BaseModel):
    status: Optional[str] = Field(None, max_length=32)
    priority: Optional[str] = Field(None, max_length=32)
    assigned_technician: Optional[str] = Field(None, max_length=128)
    internal_notes: Optional[str] = Field(None, max_length=2000)
    troubleshooting_performed: Optional[str] = Field(None, max_length=2000)
    resolution_notes: Optional[str] = Field(None, max_length=2000)

class TicketHistoryEntry(BaseModel):
    id: int
    ticket_id: str
    action: str
    performed_by: str
    details: str
    timestamp: str

class TicketResponse(BaseModel):
    ticket_id: str
    employee_id: str
    requester_name: Optional[str] = None
    guest_session_id: Optional[str] = None
    location: str
    service: str
    issue: str
    priority: str
    status: str
    department: str
    assigned_technician: Optional[str] = None
    attempted_steps: List[str] = []
    outage_ref: Optional[str] = None
    guide_ref: Optional[str] = None
    resolution_notes: Optional[str] = None
    created_at: str
    updated_at: str
    history: Optional[List[TicketHistoryEntry]] = []

# Chat Models
class SuggestionOption(BaseModel):
    label: str
    message: str

class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: Optional[str] = Field(None, max_length=128)
    employee_id: Optional[str] = Field(None, max_length=64)
    requester_name: Optional[str] = Field(None, max_length=128)
    guest_session_id: Optional[str] = Field(None, max_length=128)
    location: Optional[str] = Field(None, max_length=64)
    service: Optional[str] = Field(None, max_length=64)
    os_name: Optional[str] = Field(None, max_length=32)
    attempted_steps: Optional[List[str]] = []


class ChatMessageResponse(BaseModel):
    conversation_id: str
    incident_id: Optional[str] = None
    employee_id: Optional[str] = None
    agent_message: str
    service: str
    symptom: Optional[str] = None
    location: Optional[str] = None
    os_name: Optional[str] = None
    outage_status: str  # 'Active', 'Resolved', 'Unknown', 'None'
    outage_info: Optional[OutageRecord] = None
    suggested_steps: List[str] = []
    attempted_steps: List[str] = []
    completed_steps: List[str] = []
    failed_steps: List[str] = []
    skipped_steps: List[str] = []
    suggestions: List[SuggestionOption] = []
    is_resolved: bool = False
    resolution_status: Optional[str] = "in_progress"  # 'in_progress', 'resolved', 'unresolved'
    ticket: Optional[TicketResponse] = None
    ticket_id: Optional[str] = None
    ticket_status: Optional[str] = None
    escalation_status: Optional[str] = "none"  # 'none', 'escalated'
    next_action: str  # 'ask_location', 'outage_reported', 'troubleshoot', 'confirm_resolution', 'ticket_created', 'open_tickets', 'new_issue', 'resolved', 'clarify'

class DashboardMetricsResponse(BaseModel):
    total_tickets: int
    open_tickets: int
    in_progress_tickets: int
    resolved_tickets: int
    active_outages: int
    tickets_by_service: Dict[str, int]
    tickets_by_priority: Dict[str, int]
