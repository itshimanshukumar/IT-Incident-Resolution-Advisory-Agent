import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from backend.config import settings
from backend.database import get_connection
from backend.models import (
    ChatMessageResponse, OutageRecord, SuggestionOption, TicketResponse
)
from backend.services.outage_service import check_network_status, normalize_service, normalize_location
from backend.services.history_service import search_incident_history
from backend.services.retrieval_service import retrieve_troubleshooting_steps
from backend.services.ticket_service import create_ticket, get_ticket, list_tickets, evaluate_escalation_rules
from backend.services.llm_service import generate_advisory_response



# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def detect_service(text: str) -> str:
    t = text.lower()
    if re.search(r"\bvpn\b", t):
        return "VPN"
    if re.search(r"wi-?fi|network|internet|lan|ethernet|wifi|hotspot|router", t):
        return "Wi-Fi"
    if re.search(r"print|printer|printing|paper jam|spooler|toner|cartridge", t):
        return "Printer"
    if re.search(r"email|outlook|mailbox|exchange|webmail|inbox|send email", t):
        return "Email"
    return "Other"


def detect_os(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(mac|macos|macbook|apple|osx)\b", t):
        return "macOS"
    if re.search(r"\b(windows|win10|win11|pc)\b", t):
        return "Windows"
    return "All"


def detect_location(text: str):
    cities = ["bhopal", "pune", "chennai", "delhi", "mumbai", "bengaluru",
              "bangalore", "hyderabad", "noida", "gurugram", "kolkata"]
    for c in cities:
        if re.search(r"\b" + c + r"\b", text, re.IGNORECASE):
            return c.capitalize()
    return None


def _load_json_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return []


def _is_negative_followup(text: str) -> bool:
    return bool(re.search(
        r"not\s+(work|fix|help|solve|resolv)|did(n't|n.t)?\s+work|doesn'?t\s+work"
        r"|still\s+(not|broken|down|failing|same)"
        r"|it\s+does\s+not\s+(solve|work|fix|help)"
        r"|same\s+(problem|issue|error)"
        r"|no\s+(luck|improvement|change)"
        r"|\b(still|again)\b.*(issue|problem|error|fail)",
        text, re.IGNORECASE
    ))


# ---------------------------------------------------------------------------
# AgentService
# ---------------------------------------------------------------------------

class AgentService:
    def __init__(self, db_path=None, rules_file=None):
        self.db_path = db_path
        self.rules_file = rules_file

    def _get_rules_data(self):
        if self.rules_file and Path(self.rules_file).exists():
            try:
                return json.loads(Path(self.rules_file).read_text(encoding="utf-8"))
            except Exception:
                pass
        from backend.services.ticket_service import load_escalation_rules
        return load_escalation_rules()


    # ------------------------------------------------------------------
    # Conversation state helpers
    # ------------------------------------------------------------------

    def get_or_create_conversation(self, conversation_id, employee_id=None,
                                   location=None, guest_session_id=None):
        cid = conversation_id or str(uuid.uuid4())
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT * FROM conversations WHERE conversation_id = ?", (cid,))
        row = cur.fetchone()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not row:
            inc_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
            cur.execute("""
            INSERT INTO conversations (
                conversation_id, incident_id, employee_id, requester_name, guest_session_id,
                location, service, symptom,
                os_name, outage_status, outage_ref, suggested_steps, attempted_steps,
                completed_steps, failed_steps, skipped_steps, resolution_status,
                ticket_id, ticket_creation_status, ticket_status, escalation_status,
                last_prompt_type, retrieved_steps, is_resolved,
                prev_incident_id, prev_incident_service, prev_incident_symptom,
                prev_incident_location, prev_incident_resolved, ticket_intake_state,
                chat_history, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cid, inc_id, employee_id or None, None, guest_session_id or None,
                location or None, "Other", "",
                "All", "None", None, json.dumps([]), json.dumps([]),
                json.dumps([]), json.dumps([]), json.dumps([]), "in_progress",
                None, "none", "none", "none",
                "initial", json.dumps([]), 0,
                None, None, None, None, 0, "none",
                json.dumps([]), now_str, now_str
            ))
            conn.commit()
            cur.execute("SELECT * FROM conversations WHERE conversation_id = ?", (cid,))
            row = cur.fetchone()
        else:
            row_dict = dict(row)
            if not row_dict.get("incident_id"):
                inc_id = f"INC-{cid[-8:].upper()}"
                cur.execute("UPDATE conversations SET incident_id = ? WHERE conversation_id = ?",
                            (inc_id, cid))
                conn.commit()
                cur.execute("SELECT * FROM conversations WHERE conversation_id = ?", (cid,))
                row = cur.fetchone()
        conn.close()
        return dict(row)

    def update_conversation(self, cid: str, updates: dict):
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        fields, params = [], []
        for k, v in updates.items():
            fields.append(f"{k} = ?")
            params.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
        fields.append("updated_at = ?")
        params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        params.append(cid)
        cur.execute(f"UPDATE conversations SET {', '.join(fields)} WHERE conversation_id = ?", params)
        conn.commit()
        conn.close()

    def _safe(self, state: dict, key: str, default=None):
        """Safe access to state keys that may not exist in older DB rows."""
        return state.get(key, default)

    def _save_chat_history(self, cid: str, existing_history: list, user_msg: str, agent_msg: str):
        """Appends the latest user+assistant turn to chat_history and persists (max 20 turns)."""
        updated = list(existing_history)
        if user_msg:
            updated.append({"role": "user", "content": user_msg})
        if agent_msg:
            updated.append({"role": "assistant", "content": agent_msg})
        # Keep last 20 entries (10 full turns) to avoid unbounded growth
        updated = updated[-20:]
        self.update_conversation(cid, {"chat_history": updated})
        return updated

    # ------------------------------------------------------------------
    # Main message processor
    # ------------------------------------------------------------------

    def process_message(self, message: str, conversation_id=None, employee_id=None,
                        location=None, service=None, os_name=None,
                        attempted_from_client=None, requester_name=None,
                        guest_session_id=None):
        raw_msg = (message or "").strip()
        state = self.get_or_create_conversation(
            conversation_id, employee_id, location, guest_session_id
        )
        cid = state["conversation_id"]

        stored_suggested = _load_json_list(state.get("suggested_steps"))
        stored_attempted = _load_json_list(state.get("attempted_steps"))
        stored_failed    = _load_json_list(state.get("failed_steps"))
        stored_completed = _load_json_list(state.get("completed_steps"))
        stored_skipped   = _load_json_list(state.get("skipped_steps"))
        # Load stored chat history for multi-turn LLM context (up to 20 turns persisted)
        stored_chat_history = _load_json_list(state.get("chat_history"))

        if attempted_from_client:
            for s in attempted_from_client:
                if s not in stored_attempted:
                    stored_attempted.append(s)

        # Resolve identity fields — never silently invent values
        msg_loc     = detect_location(raw_msg)
        # For authenticated employees: use their verified employee_id.
        # For guests: keep "Guest" as the sentinel; never promote to a real ID.
        stored_emp_id = state.get("employee_id")
        cur_emp_id  = (employee_id.strip() if employee_id and employee_id.strip() else None) \
                      or stored_emp_id or "Guest"

        # Guest session: use server-supplied or stored session id
        stored_gsid = self._safe(state, "guest_session_id")
        cur_gsid    = (guest_session_id.strip() if guest_session_id and guest_session_id.strip()
                       else None) or stored_gsid

        # Requester name: collected during intake, persisted across turns
        stored_req_name = self._safe(state, "requester_name")
        cur_req_name    = (requester_name.strip() if requester_name and requester_name.strip()
                           else None) or stored_req_name

        cur_loc     = (location.strip() if location and location.strip() else None) \
                      or msg_loc or state.get("location")
        msg_os      = detect_os(raw_msg)
        cur_os      = (os_name.strip() if os_name and os_name.strip() else None) \
                      or (msg_os if msg_os != "All" else None) \
                      or state.get("os_name") or "All"
        msg_srv     = (service.strip() if service and service.strip() else None) \
                      or detect_service(raw_msg)
        stored_srv  = state.get("service") or "Other"
        cur_srv     = msg_srv if msg_srv != "Other" else stored_srv
        cur_inc_id  = state.get("incident_id") or f"INC-{cid[-8:].upper()}"
        cur_ticket_id = state.get("ticket_id")
        low_msg     = raw_msg.lower()

        # For authenticated employees: load name from database if missing (never guess location)
        if cur_emp_id.upper() != "GUEST" and not cur_req_name:
            try:
                conn_u = get_connection(self.db_path)
                cur_u = conn_u.cursor()
                cur_u.execute("SELECT name FROM users WHERE id = ?", (cur_emp_id,))
                user_row = cur_u.fetchone()
                if user_row and user_row["name"]:
                    cur_req_name = user_row["name"]
                conn_u.close()
            except Exception:
                pass

        # Ticket intake state machine state
        intake_state = self._safe(state, "ticket_intake_state", "none")

        # Previous (archived) incident context
        prev_inc_id       = self._safe(state, "prev_incident_id")
        prev_inc_service  = self._safe(state, "prev_incident_service")
        prev_inc_symptom  = self._safe(state, "prev_incident_symptom")
        prev_inc_location = self._safe(state, "prev_incident_location")
        prev_inc_resolved = bool(self._safe(state, "prev_incident_resolved", 0))

        # ----------------------------------------------------------------
        # GUARDRAIL: passwords / secrets

        # ----------------------------------------------------------------
        if re.search(r"password is|my password|otp is|one-time code|auth code|secret key", low_msg):
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=cur_inc_id,
                employee_id=cur_emp_id,
                agent_message=(
                    "For your safety, please do not share passwords, OTPs, or verification codes. "
                    "Remove those details and describe only the technical error or symptom."
                ),
                service=cur_srv, location=cur_loc, os_name=cur_os,
                outage_status=state.get("outage_status") or "None",
                suggested_steps=[], attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                resolution_status=state.get("resolution_status") or "in_progress",
                ticket_id=cur_ticket_id,
                ticket_status=state.get("ticket_status") or "none",
                escalation_status=state.get("escalation_status") or "none",
                suggestions=[
                    SuggestionOption(label="Wi-Fi issue",   message="My Wi-Fi is not working"),
                    SuggestionOption(label="Printer issue", message="My printer is offline"),
                    SuggestionOption(label="Email issue",   message="My email is not working"),
                ], next_action="clarify"
            )

        # ----------------------------------------------------------------
        # INTAKE STATE MACHINE: ticket_intake sub-flow
        # These states are entered only via explicit transitions, never from
        # a raw regex match on "raise a ticket".
        # ----------------------------------------------------------------

        # State: awaiting_intent_confirm
        # User said "raise a ticket" after a resolved incident. We asked:
        # "Document the resolved incident or start a new one?"
        if intake_state == "awaiting_intent_confirm":
            # "Document previous" / "yes" / "that incident"
            want_document_prev = bool(re.search(
                r"\b(previous|that|old|same|resolved|document|yes|yep|yeah|correct|confirm)\b",
                low_msg
            ))
            # "New issue"
            want_new = bool(re.search(
                r"\b(new|different|another|no|nope)\b", low_msg
            ))

            if want_document_prev and prev_inc_id:
                # Promote previous incident context into current ticket intake
                use_srv      = prev_inc_service  or cur_srv
                use_symptom  = prev_inc_symptom  or state.get("symptom") or raw_msg
                use_location = prev_inc_location or cur_loc

                self.update_conversation(cid, {
                    "service": use_srv,
                    "symptom": use_symptom,
                    "location": use_location,
                    "ticket_intake_state": "awaiting_requester_name"
                    if cur_emp_id.upper() == "GUEST" and not cur_req_name
                    else "ready_to_create",
                    "attempted_steps": stored_attempted,
                })

                if cur_emp_id.upper() == "GUEST" and not cur_req_name:
                    return self._ask_requester_name(
                        cid, cur_inc_id, cur_emp_id, cur_gsid, use_srv, use_location, cur_os, state
                    )
                else:
                    return self._create_ticket_from_intake(
                        cid, cur_inc_id, cur_emp_id, cur_req_name, cur_gsid,
                        use_srv, use_symptom, use_location, cur_os, state,
                        stored_attempted, stored_completed, stored_failed, stored_skipped,
                        is_prev_resolved=True
                    )

            elif want_new:
                # Start fresh — new incident, new intake
                new_inc_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
                self.update_conversation(cid, {
                    "incident_id": new_inc_id,
                    "service": "Other", "symptom": "",
                    "outage_status": "None", "outage_ref": None,
                    "outage_checked_at": None, "outage_text": "",
                    "suggested_steps": [], "attempted_steps": [],
                    "completed_steps": [], "failed_steps": [], "skipped_steps": [],
                    "resolution_status": "in_progress",
                    "is_resolved": 0, "ticket_id": None,
                    "ticket_creation_status": "none", "ticket_status": "none",
                    "escalation_status": "none", "last_prompt_type": "initial",
                    "ticket_intake_state": "none",
                })
                return ChatMessageResponse(
                    conversation_id=cid,
                    incident_id=new_inc_id,
                    employee_id=cur_emp_id,
                    agent_message=(
                        "Understood — we will start a fresh incident. "
                        "What technical problem are you experiencing? "
                        "I will check for a known outage first."
                    ),
                    service="Other", location=cur_loc, os_name=cur_os,
                    outage_status="None", suggested_steps=[], attempted_steps=[],
                    completed_steps=[], failed_steps=[], skipped_steps=[],
                    is_resolved=False, resolution_status="in_progress",
                    ticket_id=None, ticket_status="none", escalation_status="none",
                    suggestions=[
                        SuggestionOption(label="Wi-Fi issue",   message="My Wi-Fi is not working"),
                        SuggestionOption(label="Printer issue", message="My printer is offline"),
                        SuggestionOption(label="Email issue",   message="My email is not working"),
                    ], next_action="new_issue"
                )
            else:
                return ChatMessageResponse(
                    conversation_id=cid,
                    incident_id=cur_inc_id,
                    employee_id=cur_emp_id,
                    agent_message=(
                        "Sorry, I didn't catch that. "
                        f"Your previous {prev_inc_service or cur_srv} issue "
                        f"({'resolved' if prev_inc_resolved else 'unresolved'}) — "
                        "should I raise a ticket to document that incident, "
                        "or are you reporting a completely new problem?"
                    ),
                    service=cur_srv, location=cur_loc, os_name=cur_os,
                    outage_status=state.get("outage_status") or "None",
                    suggested_steps=[], attempted_steps=stored_attempted,
                    completed_steps=stored_completed, failed_steps=stored_failed,
                    skipped_steps=stored_skipped,
                    is_resolved=bool(state.get("is_resolved")),
                    resolution_status=state.get("resolution_status") or "in_progress",
                    ticket_id=cur_ticket_id,
                    ticket_status=state.get("ticket_status") or "none",
                    escalation_status=state.get("escalation_status") or "none",
                    suggestions=[
                        SuggestionOption(label="Document that incident",
                                         message="Document the resolved incident"),
                        SuggestionOption(label="Report a new issue",
                                         message="Report a new issue"),
                    ], next_action="clarify"
                )

        # State: awaiting_requester_name
        # We asked the guest for their name
        if intake_state == "awaiting_requester_name":
            candidate_name = raw_msg.strip()
            # Validate: must look like a real name (at least 2 chars, not a command)
            if len(candidate_name) >= 2 and not re.match(
                r"^(yes|no|ok|sure|help|ticket|raise|cancel|quit)$",
                candidate_name, re.IGNORECASE
            ):
                cur_req_name = candidate_name
                self.update_conversation(cid, {
                    "requester_name": cur_req_name,
                    "ticket_intake_state": "ready_to_create"
                })
                use_srv     = cur_srv
                use_symptom = state.get("symptom") or raw_msg
                use_loc     = cur_loc
                return self._create_ticket_from_intake(
                    cid, cur_inc_id, cur_emp_id, cur_req_name, cur_gsid,
                    use_srv, use_symptom, use_loc, cur_os, state,
                    stored_attempted, stored_completed, stored_failed, stored_skipped,
                    is_prev_resolved=bool(state.get("is_resolved"))
                )
            else:
                return ChatMessageResponse(
                    conversation_id=cid,
                    incident_id=cur_inc_id,
                    employee_id=cur_emp_id,
                    agent_message=(
                        "Please enter your full name (e.g. Rahul Singh) so the IT team "
                        "can identify your ticket and follow up with you."
                    ),
                    service=cur_srv, location=cur_loc, os_name=cur_os,
                    outage_status=state.get("outage_status") or "None",
                    suggested_steps=[], attempted_steps=stored_attempted,
                    completed_steps=stored_completed, failed_steps=stored_failed,
                    skipped_steps=stored_skipped,
                    is_resolved=bool(state.get("is_resolved")),
                    resolution_status=state.get("resolution_status") or "in_progress",
                    ticket_id=cur_ticket_id,
                    ticket_status=state.get("ticket_status") or "none",
                    escalation_status=state.get("escalation_status") or "none",
                    suggestions=[], next_action="clarify"
                )

        # State: awaiting_guest_os
        # We asked the guest user for their operating system (Windows / macOS)
        if intake_state == "awaiting_guest_os":
            is_esc = bool(re.search(
                r"\b(raise|create|generate|file|submit|make)\s+(a\s+)?ticket\b|\bescalate\b|not\s+resolved.*ticket|didn'?t\s+work.*ticket",
                low_msg
            ))
            if is_esc:
                intake_state = "none"
                self.update_conversation(cid, {"ticket_intake_state": "none"})
            else:
                detected_os = detect_os(raw_msg)
                # Also accept plain text answers
                if detected_os == "All":
                    low = raw_msg.strip().lower()
                    if re.search(r"\b(windows|win)\b", low):
                        detected_os = "Windows"
                    elif re.search(r"\b(mac|macos|macbook|apple)\b", low):
                        detected_os = "macOS"
                if detected_os != "All":
                    cur_os = detected_os
                    self.update_conversation(cid, {
                        "os_name": cur_os,
                        "ticket_intake_state": "none",
                    })
                    # Fall through to PATH 4 naturally with OS now set —
                    # we override intake_state so the rest of the path runs
                    intake_state = "none"
                else:
                    agent_msg = (
                        "I didn't catch that. Are you using **Windows** or **macOS**? "
                        "This helps me provide the right troubleshooting steps for your system."
                    )
                    self._save_chat_history(cid, stored_chat_history, raw_msg, agent_msg)
                    return ChatMessageResponse(
                        conversation_id=cid,
                        incident_id=cur_inc_id,
                        employee_id=cur_emp_id,
                        agent_message=agent_msg,
                        service=cur_srv, location=cur_loc, os_name=cur_os,
                        outage_status=state.get("outage_status") or "None",
                        suggested_steps=[], attempted_steps=stored_attempted,
                        completed_steps=stored_completed, failed_steps=stored_failed,
                        skipped_steps=stored_skipped,
                        is_resolved=False, resolution_status="in_progress",
                        ticket_id=cur_ticket_id,
                        ticket_status=state.get("ticket_status") or "none",
                        escalation_status=state.get("escalation_status") or "none",
                        suggestions=[
                            SuggestionOption(label="Windows", message="I am using Windows"),
                            SuggestionOption(label="macOS",   message="I am using macOS"),
                        ], next_action="ask_os"
                    )


        # ----------------------------------------------------------------
        # NAVIGATION intents
        # ----------------------------------------------------------------
        is_escalation_intent = bool(re.search(
            r"\b(raise|create|generate|file|submit|make)\s+(a\s+)?ticket\b|\bescalate\b",
            low_msg
        ))


        is_navigation_new_issue = bool(re.search(
            r"\b(start|report|have)\s+(a\s+)?(new|another|different)\s+(issue|problem|incident)\b|"
            r"\bnew\s+issue\b|"
            r"\bstart\s+new\s+session\b",
            low_msg
        ))

        specific_ticket_match = re.search(r"\b(it-\d+)\b", raw_msg, re.IGNORECASE)

        is_navigation_tickets = (not is_escalation_intent) and (not is_navigation_new_issue) and (
            bool(re.search(
                r"\b(open|show|view|check|display|see|list)\b.*?\b(ticket|tickets|modal)\b|"
                r"\bmy\s+tickets\b|"
                r"\bticket\s+modal\b|"
                r"\btickets\s+modal\b|"
                r"\bcheck\s+ticket\s+status\b|"
                r"\bticket\s+status\b",
                low_msg
            )) or bool(specific_ticket_match)
        )

        # PATH 0A: Navigation — My Tickets / Specific Ticket
        if is_navigation_tickets:
            target_ticket_id = (
                specific_ticket_match.group(1).upper() if specific_ticket_match else cur_ticket_id
            )
            ticket_obj = None
            if target_ticket_id:
                ticket_obj = get_ticket(target_ticket_id, self.db_path)

            ticket_status      = ticket_obj.status if ticket_obj else (state.get("ticket_status") or "none")
            resolution_status  = state.get("resolution_status") or (
                "unresolved" if target_ticket_id else "in_progress"
            )
            escalation_status  = state.get("escalation_status") or (
                "escalated" if target_ticket_id else "none"
            )

            if ticket_obj:
                agent_msg = (
                    f"Opening the My Tickets view. You can review your open and past support tickets, "
                    f"including ticket {ticket_obj.ticket_id} for your {ticket_obj.service} issue "
                    f"in {ticket_obj.location} "
                    f"(Status: {ticket_obj.status}, Priority: {ticket_obj.priority})."
                )
            else:
                agent_msg = (
                    "Opening the My Tickets view. You can review all your recorded support tickets "
                    "and track their resolution status."
                )

            self.update_conversation(cid, {
                "employee_id": cur_emp_id,
                "location": cur_loc,
                "service": cur_srv,
                "attempted_steps": stored_attempted
            })

            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=cur_inc_id,
                employee_id=cur_emp_id,
                agent_message=agent_msg,
                service=cur_srv, location=cur_loc, os_name=cur_os,
                outage_status=state.get("outage_status") or "None",
                suggested_steps=[], attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                is_resolved=bool(state.get("is_resolved")),
                resolution_status=resolution_status,
                ticket=ticket_obj,
                ticket_id=ticket_obj.ticket_id if ticket_obj else target_ticket_id,
                ticket_status=ticket_status,
                escalation_status=escalation_status,
                suggestions=[
                    SuggestionOption(label="Start new issue",   message="Start a new issue"),
                    SuggestionOption(label="Check ticket status", message="Check ticket status"),
                ],
                next_action="open_tickets"
            )

        # PATH 0B: Navigation — Start a new issue
        if is_navigation_new_issue:
            new_inc_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
            self.update_conversation(cid, {
                "incident_id": new_inc_id,
                "service": "Other", "symptom": "",
                "outage_status": "None", "outage_ref": None,
                "outage_checked_at": None, "outage_text": "",
                "suggested_steps": [], "attempted_steps": [],
                "completed_steps": [], "failed_steps": [], "skipped_steps": [],
                "resolution_status": "in_progress",
                "is_resolved": 0, "ticket_id": None,
                "ticket_creation_status": "none", "ticket_status": "none",
                "escalation_status": "none", "last_prompt_type": "initial",
                "ticket_intake_state": "none",
            })
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=new_inc_id,
                employee_id=cur_emp_id,
                agent_message=(
                    "I am ready to help you with a new issue. "
                    "What technical problem or symptom are you experiencing? "
                    "I will check outage records and guide you through approved steps."
                ),
                service="Other", location=cur_loc, os_name=cur_os,
                outage_status="None", suggested_steps=[], attempted_steps=[],
                completed_steps=[], failed_steps=[], skipped_steps=[],
                is_resolved=False, resolution_status="in_progress",
                ticket=None, ticket_id=None, ticket_status="none", escalation_status="none",
                suggestions=[
                    SuggestionOption(label="Wi-Fi issue",   message="My Wi-Fi is not working"),
                    SuggestionOption(label="Printer issue", message="My printer is offline"),
                    SuggestionOption(label="Email issue",   message="My email is not working"),
                ],
                next_action="new_issue"
            )

        # ----------------------------------------------------------------
        # Intent classification (order matters)
        # ----------------------------------------------------------------
        is_confirmed_resolved = bool(re.search(
            r"\b(yes|fixed|it worked|problem solved|working now)\b|(?<!not\s)(?<!un)resolved",
            low_msg
        ))

        is_negative_followup = (
            not is_escalation_intent and not is_confirmed_resolved
            and _is_negative_followup(raw_msg)
            and bool(stored_suggested) and bool(state.get("symptom"))
        )

        # Also treat "Not resolved — raise ticket" as a combined negative+escalation
        is_explicit_unresolved_escalation = bool(re.search(
            r"not\s+resolved.*ticket|unresolved.*ticket|didn'?t\s+work.*ticket"
            r"|still\s+not.*raise",
            low_msg
        ))

        # ----------------------------------------------------------------
        # PATH 1: Issue resolved ("yes fixed", "working now", etc.)
        # ----------------------------------------------------------------
        if state.get("symptom") and is_confirmed_resolved and not is_escalation_intent:
            for s in stored_attempted:
                if s not in stored_completed:
                    stored_completed.append(s)

            # Archive current incident context before marking resolved
            self.update_conversation(cid, {
                "is_resolved": 1,
                "resolution_status": "resolved",
                "attempted_steps": stored_attempted,
                "completed_steps": stored_completed,
                # Save previous incident snapshot so ticket intake can reuse it
                "prev_incident_id": cur_inc_id,
                "prev_incident_service": cur_srv,
                "prev_incident_symptom": state.get("symptom") or "",
                "prev_incident_location": cur_loc or "",
                "prev_incident_resolved": 1,
                "ticket_intake_state": "none",
            })
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=cur_inc_id,
                employee_id=cur_emp_id,
                agent_message=(
                    "Great — glad to hear your issue is resolved! "
                    "This troubleshooting session is complete. "
                    "If you need to raise a support ticket to document this incident "
                    "or report a new problem, just let me know."
                ),
                service=cur_srv, location=cur_loc, os_name=cur_os,
                outage_status="Resolved", is_resolved=True,
                resolution_status="resolved",
                suggested_steps=[], attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                suggestions=[
                    SuggestionOption(label="Raise a ticket for this",
                                     message="I want to raise a ticket"),
                    SuggestionOption(label="Start new issue",
                                     message="I have a new issue to report"),
                ],
                next_action="resolved"
            )

        # ----------------------------------------------------------------
        # PATH 2: Escalation / ticket creation intent
        # ----------------------------------------------------------------
        if is_escalation_intent or is_explicit_unresolved_escalation:

            # Sub-case A: incident is already marked resolved in state.
            # Do NOT create a ticket immediately — ask whether they want to
            # document the resolved incident or report something new.
            if state.get("is_resolved") and state.get("symptom") and not cur_ticket_id:
                # Enter awaiting_intent_confirm state
                self.update_conversation(cid, {
                    "ticket_intake_state": "awaiting_intent_confirm",
                    "prev_incident_id": state.get("incident_id") or cur_inc_id,
                    "prev_incident_service": cur_srv,
                    "prev_incident_symptom": state.get("symptom") or "",
                    "prev_incident_location": cur_loc or "",
                    "prev_incident_resolved": 1,
                    "attempted_steps": stored_attempted,
                })
                return ChatMessageResponse(
                    conversation_id=cid,
                    incident_id=cur_inc_id,
                    employee_id=cur_emp_id,
                    agent_message=(
                        f"Your {cur_srv} issue was marked as resolved earlier. "
                        "Would you like me to raise a ticket to document that resolved incident, "
                        "or are you reporting a new problem?"
                    ),
                    service=cur_srv, location=cur_loc, os_name=cur_os,
                    outage_status=state.get("outage_status") or "Resolved",
                    suggested_steps=[], attempted_steps=stored_attempted,
                    completed_steps=stored_completed, failed_steps=stored_failed,
                    skipped_steps=stored_skipped,
                    is_resolved=True, resolution_status="resolved",
                    ticket_id=cur_ticket_id,
                    ticket_status=state.get("ticket_status") or "none",
                    escalation_status=state.get("escalation_status") or "none",
                    suggestions=[
                        SuggestionOption(label="Document the resolved incident",
                                         message="Document the resolved incident"),
                        SuggestionOption(label="Report a new issue",
                                         message="Report a new issue"),
                    ], next_action="clarify"
                )

            # Sub-case B: existing open ticket — return it, no duplicate
            existing_ticket = None
            if cur_ticket_id:
                existing_ticket = get_ticket(cur_ticket_id, self.db_path)
            if existing_ticket and existing_ticket.status not in ("Resolved", "Closed"):
                agent_msg = (
                    f"You already have an open ticket for this issue: {existing_ticket.ticket_id}. "
                    f"Assigned department: {existing_ticket.department}. "
                    f"Status: {existing_ticket.status}. "
                    "I will keep using that ticket to avoid duplicate submissions."
                )
                self.update_conversation(cid, {"attempted_steps": stored_attempted})
                return ChatMessageResponse(
                    conversation_id=cid,
                    incident_id=cur_inc_id,
                    employee_id=cur_emp_id,
                    agent_message=agent_msg,
                    service=cur_srv, location=cur_loc, os_name=cur_os,
                    outage_status=state.get("outage_status") or "None",
                    suggested_steps=[], attempted_steps=stored_attempted,
                    completed_steps=stored_completed, failed_steps=stored_failed,
                    skipped_steps=stored_skipped,
                    is_resolved=False, resolution_status="unresolved",
                    ticket=existing_ticket,
                    ticket_id=existing_ticket.ticket_id,
                    ticket_status=existing_ticket.status,
                    escalation_status="escalated",
                    suggestions=[
                        SuggestionOption(label="View My Tickets", message="Open tickets modal"),
                        SuggestionOption(label="Start new issue",  message="I have another issue"),
                    ], next_action="ticket_created"
                )

            # Sub-case C: location is still missing — must collect it before ticket creation
            if not cur_loc:
                self.update_conversation(cid, {
                    "service": cur_srv,
                    "symptom": state.get("symptom") or raw_msg,
                    "os_name": cur_os,
                    "ticket_intake_state": "awaiting_location_for_ticket",
                    "attempted_steps": stored_attempted,
                })
                return ChatMessageResponse(
                    conversation_id=cid,
                    incident_id=cur_inc_id,
                    employee_id=cur_emp_id,
                    agent_message=(
                        "To route your support ticket correctly, which office location "
                        "are you working from? (e.g. Bhopal, Pune, Chennai)"
                    ),
                    service=cur_srv, location=None, os_name=cur_os,
                    outage_status="Unknown", suggested_steps=[], attempted_steps=stored_attempted,
                    completed_steps=stored_completed, failed_steps=stored_failed,
                    skipped_steps=stored_skipped,
                    is_resolved=False, resolution_status="in_progress",
                    ticket_id=cur_ticket_id,
                    ticket_status=state.get("ticket_status") or "none",
                    escalation_status=state.get("escalation_status") or "none",
                    suggestions=[
                        SuggestionOption(label="Bhopal",  message="I am working in Bhopal"),
                        SuggestionOption(label="Pune",    message="I am working in Pune"),
                        SuggestionOption(label="Chennai", message="I am working in Chennai"),
                    ], next_action="ask_location"
                )

            # Sub-case D: guest and no requester name collected yet
            if cur_emp_id.upper() == "GUEST" and not cur_req_name:
                use_symptom = state.get("symptom") or raw_msg
                self.update_conversation(cid, {
                    "service": cur_srv, "symptom": use_symptom, "location": cur_loc,
                    "ticket_intake_state": "awaiting_requester_name",
                    "attempted_steps": stored_attempted,
                })
                return self._ask_requester_name(
                    cid, cur_inc_id, cur_emp_id, cur_gsid, cur_srv, cur_loc, cur_os, state
                )

            # Sub-case E: all required fields present — create ticket
            issue_desc = state.get("symptom") or raw_msg
            return self._create_ticket_from_intake(
                cid, cur_inc_id, cur_emp_id, cur_req_name, cur_gsid,
                cur_srv, issue_desc, cur_loc, cur_os, state,
                stored_attempted, stored_completed, stored_failed, stored_skipped,
                is_prev_resolved=bool(state.get("is_resolved"))
            )

        # ----------------------------------------------------------------
        # PATH 3: Negative follow-up — steps did not help, serve next batch
        # ----------------------------------------------------------------
        if is_negative_followup:
            for step in stored_suggested:
                if step not in stored_attempted: stored_attempted.append(step)
                if step not in stored_failed:    stored_failed.append(step)

            current_symptom      = state.get("symptom") or raw_msg
            cached_outage_status = state.get("outage_status") or "Unknown"
            cached_outage_text   = state.get("outage_text") or ""

            # Evaluate escalation rules and attempt threshold
            rule_pri, rule_dept, attempts_thresh = evaluate_escalation_rules(cur_srv, current_symptom, self._get_rules_data())
            valid_attempts = len(set(stored_attempted) | set(stored_failed))

            retrieval  = retrieve_troubleshooting_steps(cur_srv, current_symptom, cur_os)
            all_steps  = [st.instruction for st in retrieval.steps]
            exhausted  = set(stored_attempted) | set(stored_failed) | set(stored_completed) | set(stored_skipped)
            next_steps = [s for s in all_steps if s not in exhausted][:3]

            # If attempt threshold reached or all guide steps exhausted -> escalate
            if valid_attempts >= attempts_thresh or not next_steps:
                self.update_conversation(cid, {
                    "attempted_steps": stored_attempted,
                    "failed_steps": stored_failed,
                    "suggested_steps": [],
                    "last_prompt_type": "exhausted"
                })
                if valid_attempts >= attempts_thresh:
                    agent_msg = (
                        f"You have completed {valid_attempts} troubleshooting attempt{'s' if valid_attempts != 1 else ''}, "
                        f"reaching the configured escalation threshold ({attempts_thresh} attempt{'s' if attempts_thresh != 1 else ''}) for {cur_srv}. "
                        f"I recommend escalating this to {rule_dept} ({rule_pri} priority). "
                        "Would you like me to raise a support ticket on your behalf?"
                    )
                else:
                    agent_msg = (
                        "I have gone through all the recommended troubleshooting steps from the "
                        "approved IT guide for this issue and none of them resolved it. "
                        f"I recommend escalating this to {rule_dept} ({rule_pri} priority). "
                        "Would you like me to raise a support ticket on your behalf?"
                    )
                self._save_chat_history(cid, stored_chat_history, raw_msg, agent_msg)
                return ChatMessageResponse(
                    conversation_id=cid,
                    incident_id=cur_inc_id,
                    employee_id=cur_emp_id,
                    agent_message=agent_msg,
                    service=cur_srv, location=cur_loc, os_name=cur_os,
                    outage_status=cached_outage_status,
                    suggested_steps=[], attempted_steps=stored_attempted,
                    completed_steps=stored_completed, failed_steps=stored_failed,
                    skipped_steps=stored_skipped,
                    is_resolved=False,
                    resolution_status=state.get("resolution_status") or "in_progress",
                    ticket_id=cur_ticket_id,
                    ticket_status=state.get("ticket_status") or "none",
                    escalation_status=state.get("escalation_status") or "none",
                    suggestions=[
                        SuggestionOption(label="Yes, raise a ticket",
                                         message="Please raise a ticket for this issue"),
                        SuggestionOption(label="I'll try something else",
                                         message="Let me try something else"),
                    ], next_action="confirm_resolution"
                )

            # Historical evidence lookup
            hist_records = search_incident_history(location=cur_loc, service=cur_srv, symptom=current_symptom)
            hist_note = ""
            if hist_records:
                top_h = hist_records[0]
                hist_note = f"\n\nHistorical Incident Context: Past incident {top_h.incident_id} ({top_h.status}) in {top_h.location} for {top_h.service} resolved via: {top_h.resolution}."

            matched_steps = [st for st in retrieval.steps if st.instruction in next_steps]
            citations = [{"guide_id": st.guide_id, "version": st.guide_version, "document": st.source_document, "section": st.source_section, "step": st.step_number} for st in matched_steps]

            llm_enhanced = generate_advisory_response(
                user_message=raw_msg, service=cur_srv, location=cur_loc,
                outage_info={"status": cached_outage_status, "message": cached_outage_text},
                steps=next_steps, attempted_steps=stored_attempted,
                history_info=[h.model_dump() for h in hist_records[:2]],
                document_citations=citations,
                db_path=self.db_path,
                chat_history=stored_chat_history
            )
            steps_formatted = "\n".join(f"{i+1}. {s}" for i, s in enumerate(next_steps))
            guide_source_doc = matched_steps[0].source_document if matched_steps else "approved IT guide"
            guide_ref_id = matched_steps[0].guide_id if matched_steps else "GUIDE-IT"
            guide_ref_ver = matched_steps[0].guide_version if matched_steps else "1.0"
            intro = (
                f"The previous steps did not resolve the issue. "
                f"Here are the next recommended troubleshooting actions from approved IT guide [{guide_ref_id} v{guide_ref_ver} ({guide_source_doc})]:\n\n"
            )
            if llm_enhanced:
                final_message = (
                    f"{intro}{llm_enhanced}{hist_note}\n\n"
                    "Is your problem resolved? Reply 'Yes' if fixed, or 'Not resolved' to create a ticket."
                )
            else:
                final_message = (
                    f"{intro}{steps_formatted}{hist_note}\n\n"
                    "Is your problem resolved? Reply 'Yes' if fixed, or 'Not resolved' to create a ticket."
                )

            self.update_conversation(cid, {
                "suggested_steps": next_steps,
                "attempted_steps": stored_attempted,
                "failed_steps": stored_failed,
                "last_prompt_type": "troubleshoot_followup"
            })
            self._save_chat_history(cid, stored_chat_history, raw_msg, final_message)
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=cur_inc_id,
                employee_id=cur_emp_id,
                agent_message=final_message,
                service=cur_srv, symptom=current_symptom, location=cur_loc, os_name=cur_os,
                outage_status=cached_outage_status, suggested_steps=next_steps,
                attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                is_resolved=False,
                resolution_status=state.get("resolution_status") or "in_progress",
                ticket_id=cur_ticket_id,
                ticket_status=state.get("ticket_status") or "none",
                escalation_status=state.get("escalation_status") or "none",
                suggestions=[
                    SuggestionOption(label="Yes, resolved",
                                     message="Yes, it is resolved"),
                    SuggestionOption(label="Not resolved - raise ticket",
                                     message="Not resolved, please raise a ticket"),
                ], next_action="troubleshoot"
            )

        # ----------------------------------------------------------------
        # PATH 4: New or continuing issue — troubleshoot
        # ----------------------------------------------------------------
        current_symptom = state.get("symptom") or ""
        if not current_symptom:
            current_symptom = raw_msg
        else:
            if raw_msg.lower() not in current_symptom.lower():
                current_symptom = f"{current_symptom}. {raw_msg}"

        if not cur_loc:
            self.update_conversation(cid, {
                "service": cur_srv, "symptom": current_symptom, "os_name": cur_os
            })
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=cur_inc_id,
                employee_id=cur_emp_id,
                agent_message=(
                    "Before I check outage records and guide you through troubleshooting, "
                    "which office location are you working from? (e.g. Bhopal, Pune, Chennai)"
                ),
                service=cur_srv, location=None, os_name=cur_os,
                outage_status="Unknown", suggested_steps=[], attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                is_resolved=False, resolution_status="in_progress",
                ticket_id=cur_ticket_id,
                ticket_status=state.get("ticket_status") or "none",
                escalation_status=state.get("escalation_status") or "none",
                suggestions=[
                    SuggestionOption(label="Bhopal",  message="I am working in Bhopal"),
                    SuggestionOption(label="Pune",    message="I am working in Pune"),
                    SuggestionOption(label="Chennai", message="I am working in Chennai"),
                ], next_action="ask_location"
            )

        stored_outage_status = state.get("outage_status") or "None"
        stored_outage_text   = state.get("outage_text") or ""
        already_checked      = bool(state.get("outage_checked_at"))

        if already_checked and stored_outage_status not in ("None", ""):
            outage = OutageRecord(
                outage_ref=state.get("outage_ref"), location=cur_loc, service=cur_srv,
                status=stored_outage_status, message=stored_outage_text,
                lookup_timestamp=state.get("outage_checked_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                is_stale=True, warning="Using cached outage status from earlier in this session."
            )
        else:
            outage = check_network_status(cur_loc, cur_srv)

        self.update_conversation(cid, {
            "employee_id": cur_emp_id, "location": cur_loc, "service": cur_srv,
            "symptom": current_symptom, "os_name": cur_os,
            "outage_status": outage.status, "outage_ref": outage.outage_ref,
            "outage_checked_at": outage.lookup_timestamp, "outage_text": outage.message
        })

        if outage.status == "Active":
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=cur_inc_id,
                employee_id=cur_emp_id,
                agent_message=outage.message,
                service=cur_srv, location=cur_loc, os_name=cur_os,
                outage_status="Active", outage_info=outage,
                suggested_steps=[], attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                is_resolved=False, resolution_status="in_progress",
                ticket_id=cur_ticket_id,
                ticket_status=state.get("ticket_status") or "none",
                escalation_status=state.get("escalation_status") or "none",
                suggestions=[
                    SuggestionOption(label="Raise a ticket anyway",
                                     message="Please raise a ticket for this issue"),
                    SuggestionOption(label="I'll wait for resolution", message="Understood, I will wait"),
                ], next_action="outage_reported"
            )

        # Check escalation rules threshold
        rule_pri, rule_dept, attempts_thresh = evaluate_escalation_rules(cur_srv, current_symptom, self._get_rules_data())
        if attempts_thresh == 0 or cur_srv in ["VPN", "Security"]:
            status_intro = (outage.message + " ") if (outage and outage.message) else ""
            if cur_srv == "VPN":
                msg = (
                    f"{status_intro}VPN connectivity is not supported by the currently approved "
                    f"IT incident resolution guide. I recommend escalating this issue to Network "
                    f"Engineering so a specialist can resolve it (Priority: {rule_pri}). "
                    "Would you like me to raise a support ticket on your behalf?"
                )
            else:
                msg = (
                    f"{status_intro}{cur_srv} incidents require immediate escalation to {rule_dept} "
                    f"with {rule_pri} priority (escalation threshold: 0 attempts). "
                    "Would you like me to raise a support ticket on your behalf?"
                )
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=cur_inc_id,
                employee_id=cur_emp_id,
                agent_message=msg,
                service=cur_srv, location=cur_loc, os_name=cur_os,
                outage_status=outage.status, outage_info=outage,
                suggested_steps=[], attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                is_resolved=False, resolution_status="in_progress",
                ticket_id=cur_ticket_id,
                ticket_status=state.get("ticket_status") or "none",
                escalation_status="escalated",
                suggestions=[
                    SuggestionOption(label="Yes, raise a ticket",
                                     message="Please raise a ticket for this issue"),
                    SuggestionOption(label="I'll wait", message="I will wait"),
                ], next_action="confirm_resolution"
            )

        # Incident history evidence
        hist_records = search_incident_history(location=cur_loc, service=cur_srv, symptom=current_symptom)
        hist_note = ""
        if hist_records:
            top_h = hist_records[0]
            hist_note = f"\n\nHistorical Incident Context: Past incident {top_h.incident_id} ({top_h.status}) in {top_h.location} for {top_h.service} resolved via: {top_h.resolution}."

        retrieval  = retrieve_troubleshooting_steps(cur_srv, current_symptom, cur_os)
        exhausted  = set(stored_suggested) | set(stored_attempted) | set(stored_failed) | set(stored_completed) | set(stored_skipped)
        all_steps  = [st.instruction for st in retrieval.steps]
        next_steps = [s for s in all_steps if s not in exhausted][:3]

        if not next_steps:
            # Check if LLM can provide a conversational answer to the employee's specific question/follow-up
            matched_steps = [st for st in retrieval.steps if st.instruction in stored_suggested] or retrieval.steps
            citations = [{"guide_id": st.guide_id, "version": st.guide_version, "document": st.source_document, "section": st.source_section, "step": st.step_number} for st in matched_steps]
            llm_followup = generate_advisory_response(
                user_message=raw_msg, service=cur_srv, location=cur_loc,
                outage_info=outage.model_dump() if outage else None,
                steps=stored_suggested or all_steps, attempted_steps=stored_attempted,
                history_info=[h.model_dump() for h in hist_records[:2]],
                document_citations=citations,
                db_path=self.db_path,
                chat_history=stored_chat_history
            )
            if llm_followup:
                prompt_suffix = ""
                if not re.search(r"reply ['\"]?yes['\"]?|is your problem resolved|raise a ticket", llm_followup, re.IGNORECASE):
                    prompt_suffix = "\n\nDid this answer your question or resolve your issue? Reply 'Yes' if fixed, or 'Not resolved' to create a ticket."
                final_message = f"{llm_followup}{prompt_suffix}"
                self._save_chat_history(cid, stored_chat_history, raw_msg, final_message)
                return ChatMessageResponse(
                    conversation_id=cid,
                    incident_id=cur_inc_id,
                    employee_id=cur_emp_id,
                    agent_message=final_message,
                    service=cur_srv, symptom=current_symptom, location=cur_loc, os_name=cur_os,
                    outage_status=outage.status if outage else "None", outage_info=outage,
                    suggested_steps=stored_suggested, attempted_steps=stored_attempted,
                    completed_steps=stored_completed, failed_steps=stored_failed,
                    skipped_steps=stored_skipped,
                    is_resolved=False, resolution_status="in_progress",
                    ticket_id=cur_ticket_id,
                    ticket_status=state.get("ticket_status") or "none",
                    escalation_status=state.get("escalation_status") or "none",
                    suggestions=[
                        SuggestionOption(label="Yes, resolved", message="Yes, it is resolved"),
                        SuggestionOption(label="Not resolved - raise ticket", message="Not resolved, please raise a ticket"),
                    ], next_action="troubleshoot"
                )

            status_intro = outage.message + " " if outage.message else ""
            msg = (
                f"{status_intro}There are no further troubleshooting steps available in the "
                "approved IT guide for this issue. "
                f"I recommend escalating this to {rule_dept} ({rule_pri} priority). "
                "Did earlier steps resolve your problem? If not, reply 'Not resolved' and "
                "I will raise a support ticket."
            )
            self._save_chat_history(cid, stored_chat_history, raw_msg, msg)
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=cur_inc_id,
                employee_id=cur_emp_id,
                agent_message=msg,
                service=cur_srv, location=cur_loc, os_name=cur_os,
                outage_status=outage.status, outage_info=outage,
                suggested_steps=[], attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                is_resolved=False, resolution_status="in_progress",
                ticket_id=cur_ticket_id,
                ticket_status=state.get("ticket_status") or "none",
                escalation_status=state.get("escalation_status") or "none",
                suggestions=[
                    SuggestionOption(label="Yes, resolved",
                                     message="Yes, it is resolved"),
                    SuggestionOption(label="Not resolved - raise ticket",
                                     message="Not resolved, please raise a ticket"),
                ], next_action="confirm_resolution"
            )

        matched_steps = [st for st in retrieval.steps if st.instruction in next_steps]
        citations = [{"guide_id": st.guide_id, "version": st.guide_version, "document": st.source_document, "section": st.source_section, "step": st.step_number} for st in matched_steps]

        llm_enhanced = generate_advisory_response(
            user_message=raw_msg, service=cur_srv, location=cur_loc,
            outage_info=outage.model_dump(), steps=next_steps, attempted_steps=stored_attempted,
            history_info=[h.model_dump() for h in hist_records[:2]],
            document_citations=citations,
            db_path=self.db_path,
            chat_history=stored_chat_history
        )
        steps_formatted = "\n".join(f"{i+1}. {s}" for i, s in enumerate(next_steps))
        status_prefix   = outage.message + " " if outage.message else ""
        guide_source_doc = matched_steps[0].source_document if matched_steps else "approved IT guide"
        guide_ref_id = matched_steps[0].guide_id if matched_steps else "GUIDE-IT"
        guide_ref_ver = matched_steps[0].guide_version if matched_steps else "1.0"

        if llm_enhanced:
            final_message = (
                f"{status_prefix}\n\n{llm_enhanced}{hist_note}\n\n"
                "Is your problem resolved? Reply 'Yes' if fixed, or 'Not resolved' to create a ticket."
            )
        else:
            final_message = (
                f"{status_prefix}Based on the approved Starter IT Guide ({cur_srv}) [{guide_ref_id} v{guide_ref_ver} ({guide_source_doc})], "
                f"try these steps one at a time:\n\n{steps_formatted}{hist_note}\n\n"
                "Is your problem resolved? Reply 'Yes' if fixed, or 'Not resolved' to create a ticket."
            )

        self.update_conversation(cid, {
            "suggested_steps": next_steps,
            "attempted_steps": stored_attempted,
            "last_prompt_type": "troubleshoot"
        })
        self._save_chat_history(cid, stored_chat_history, raw_msg, final_message)

        return ChatMessageResponse(
            conversation_id=cid,
            incident_id=cur_inc_id,
            employee_id=cur_emp_id,
            agent_message=final_message,
            service=cur_srv, symptom=current_symptom, location=cur_loc, os_name=cur_os,
            outage_status=outage.status, outage_info=outage,
            suggested_steps=next_steps, attempted_steps=stored_attempted,
            completed_steps=stored_completed, failed_steps=stored_failed,
            skipped_steps=stored_skipped,
            is_resolved=False, resolution_status="in_progress",
            ticket_id=cur_ticket_id,
            ticket_status=state.get("ticket_status") or "none",
            escalation_status=state.get("escalation_status") or "none",
            suggestions=[
                SuggestionOption(label="Yes, resolved",
                                 message="Yes, it is resolved"),
                SuggestionOption(label="Not resolved - raise ticket",
                                 message="Not resolved, please raise a ticket"),
            ], next_action="troubleshoot"
        )


    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ask_requester_name(self, cid, inc_id, emp_id, gsid, service, location, os_name, state):
        """Prompt guest for their name before ticket creation."""
        stored_attempted = _load_json_list(state.get("attempted_steps"))
        return ChatMessageResponse(
            conversation_id=cid,
            incident_id=inc_id,
            employee_id=emp_id,
            agent_message=(
                "To create a support ticket, the IT team needs to know who to follow up with. "
                "Please enter your full name (e.g. Rahul Singh). "
                "We do not need your password or any authentication codes."
            ),
            service=service, location=location, os_name=os_name,
            outage_status=state.get("outage_status") or "None",
            suggested_steps=[], attempted_steps=stored_attempted,
            completed_steps=_load_json_list(state.get("completed_steps")),
            failed_steps=_load_json_list(state.get("failed_steps")),
            skipped_steps=_load_json_list(state.get("skipped_steps")),
            is_resolved=bool(state.get("is_resolved")),
            resolution_status=state.get("resolution_status") or "in_progress",
            ticket_id=state.get("ticket_id"),
            ticket_status=state.get("ticket_status") or "none",
            escalation_status=state.get("escalation_status") or "none",
            suggestions=[], next_action="clarify"
        )

    def _create_ticket_from_intake(
        self, cid, inc_id, emp_id, req_name, gsid,
        service, issue_desc, location, os_name, state,
        stored_attempted, stored_completed, stored_failed, stored_skipped,
        is_prev_resolved=False
    ):
        """
        Central ticket creation call — the ONLY place tickets are persisted
        from conversation flow. Calls the validated create_ticket gate.
        Preserves resolution_status if the incident was resolved.
        """
        try:
            ticket_resp, is_new = create_ticket(
                employee_id=emp_id,
                location=location,
                issue=issue_desc,
                service=service,
                conversation_id=cid,
                attempted_steps=stored_attempted,
                outage_ref=state.get("outage_ref"),
                requester_name=req_name,
                guest_session_id=gsid,
                db_path=self.db_path
            )
        except ValueError as ve:
            # Validation gate blocked creation — surface the reason
            return ChatMessageResponse(
                conversation_id=cid,
                incident_id=inc_id,
                employee_id=emp_id,
                agent_message=f"I could not create the ticket: {ve}",
                service=service, location=location, os_name=os_name,
                outage_status=state.get("outage_status") or "None",
                suggested_steps=[], attempted_steps=stored_attempted,
                completed_steps=stored_completed, failed_steps=stored_failed,
                skipped_steps=stored_skipped,
                is_resolved=is_prev_resolved,
                resolution_status="resolved" if is_prev_resolved else "in_progress",
                ticket_id=state.get("ticket_id"),
                ticket_status=state.get("ticket_status") or "none",
                escalation_status=state.get("escalation_status") or "none",
                suggestions=[], next_action="clarify"
            )

        creation_status  = "created" if is_new else "existing"
        # Do NOT force resolution_status to "unresolved" when the incident was resolved
        new_res_status   = "resolved" if is_prev_resolved else "unresolved"

        self.update_conversation(cid, {
            "ticket_id": ticket_resp.ticket_id,
            "ticket_creation_status": creation_status,
            "ticket_status": ticket_resp.status,
            "escalation_status": "escalated",
            "resolution_status": new_res_status,
            "attempted_steps": stored_attempted,
            "ticket_intake_state": "ticket_created",
            "requester_name": req_name,
            "guest_session_id": gsid,
        })

        if is_new:
            if is_prev_resolved:
                agent_msg = (
                    f"Ticket created: {ticket_resp.ticket_id}. "
                    f"This documents your resolved {service} incident. "
                    f"Department: {ticket_resp.department}. Priority: {ticket_resp.priority}. "
                    f"Status: {ticket_resp.status}. Track it anytime in My Tickets."
                )
            else:
                agent_msg = (
                    f"Ticket created successfully: {ticket_resp.ticket_id}. "
                    f"Assigned department: {ticket_resp.department}. "
                    f"Priority: {ticket_resp.priority}. "
                    f"Status: {ticket_resp.status}. "
                    "You can follow its progress anytime in My Tickets."
                )
        else:
            agent_msg = (
                f"You already have an open ticket for this issue: {ticket_resp.ticket_id}. "
                f"Assigned department: {ticket_resp.department}. "
                f"Status: {ticket_resp.status}. "
                "I will keep using that ticket to avoid duplicate submissions."
            )

        return ChatMessageResponse(
            conversation_id=cid,
            incident_id=inc_id,
            employee_id=emp_id,
            agent_message=agent_msg,
            service=service, location=location, os_name=os_name,
            outage_status=state.get("outage_status") or "None",
            suggested_steps=[], attempted_steps=stored_attempted,
            completed_steps=stored_completed, failed_steps=stored_failed,
            skipped_steps=stored_skipped,
            is_resolved=is_prev_resolved,
            resolution_status=new_res_status,
            ticket=ticket_resp,
            ticket_id=ticket_resp.ticket_id,
            ticket_status=ticket_resp.status,
            escalation_status="escalated",
            suggestions=[
                SuggestionOption(label="View My Tickets", message="Open tickets modal"),
                SuggestionOption(label="Start new issue",  message="I have another issue"),
            ], next_action="ticket_created"
        )


agent_service = AgentService()
