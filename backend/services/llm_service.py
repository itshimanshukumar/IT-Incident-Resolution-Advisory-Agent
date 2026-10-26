import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from backend.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are ResolveIT, an IT Incident Resolution Advisory Agent.
Rules you must strictly follow:
1. You are an advisory system only. Never falsely claim you have fixed a physical device, restored a server, or verified an outage without data.
2. Never request employee passwords, PINs, OTPs, or authentication tokens.
3. Never advise employees to disable security controls, turn off firewalls, or bypass authentication.
4. If an outage is recorded as active, present the outage details and do not suggest ordinary troubleshooting.
5. If no outage is found, report that the status is unknown, not confirmed healthy.
6. Present clear, ordered, numbered steps with what the employee should observe.
7. Only use provided guide instructions; do not invent fictional IT procedures.
8. Ground every recommendation in verified tool results and source citations.
9. Directly address the user's specific question or query in a helpful, conversational manner using the context and prior conversation history. Do not repeat previous answers or steps verbatim when the user asks a follow-up or clarifying question.
"""

# Registered Tool Definitions
AGENT_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_network_status",
            "description": "Check whether an active or recorded network/service outage exists for a given location and IT service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Employee office location (e.g. Bhopal, Pune, Chennai)"},
                    "service": {"type": "string", "description": "Affected IT service (Wi-Fi, Printer, Email, VPN, or Other)"}
                },
                "required": ["location", "service"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_incident_history",
            "description": "Search past resolved incidents and historical resolution patterns matching service, location, or symptom.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Office location"},
                    "service": {"type": "string", "description": "IT service"},
                    "symptom": {"type": "string", "description": "Symptom description or error keywords"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_troubleshooting_steps",
            "description": "Retrieve approved troubleshooting instructions from the active IT guide matching service, symptom, and OS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "IT service name"},
                    "symptom": {"type": "string", "description": "Observed symptom or error"},
                    "os_name": {"type": "string", "description": "Target OS: 'Windows', 'macOS', or 'All'"}
                },
                "required": ["service", "symptom"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Create an IT support ticket in SQLite and sync to CSV when an issue is unresolved or escalated.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Verified employee ID or 'Guest'"},
                    "location": {"type": "string", "description": "Office location"},
                    "issue": {"type": "string", "description": "Detailed description of the issue"},
                    "service": {"type": "string", "description": "Affected IT service"},
                    "priority": {"type": "string", "description": "Ticket priority (Low, Medium, High, Critical)"},
                    "requester_name": {"type": "string", "description": "Full name if guest ticket"}
                },
                "required": ["employee_id", "location", "issue"]
            }
        }
    }
]

def execute_tool_call(tool_name: str, arguments: Dict[str, Any], db_path=None) -> Dict[str, Any]:
    """
    Safely executes an explicitly registered tool.
    Never executes arbitrary code, shell commands, or unregistered functions.
    """
    from backend.services.outage_service import check_network_status
    from backend.services.history_service import search_incident_history
    from backend.services.retrieval_service import retrieve_troubleshooting_steps
    from backend.services.ticket_service import create_ticket

    clean_name = (tool_name or "").strip()
    if clean_name == "check_network_status":
        loc = arguments.get("location")
        srv = arguments.get("service")
        res = check_network_status(loc, srv)
        return res.model_dump()

    elif clean_name == "search_incident_history":
        loc = arguments.get("location")
        srv = arguments.get("service")
        sym = arguments.get("symptom")
        res_list = search_incident_history(loc, srv, sym)
        return {"incidents": [item.model_dump() for item in res_list]}

    elif clean_name == "retrieve_troubleshooting_steps":
        srv = arguments.get("service")
        sym = arguments.get("symptom")
        os_n = arguments.get("os_name", "All")
        res = retrieve_troubleshooting_steps(srv, sym, os_n)
        return res.model_dump()

    elif clean_name == "create_ticket":
        emp_id = arguments.get("employee_id")
        loc = arguments.get("location")
        iss = arguments.get("issue")
        srv = arguments.get("service")
        pri = arguments.get("priority")
        req_name = arguments.get("requester_name")
        ticket_resp, is_new = create_ticket(
            employee_id=emp_id,
            location=loc,
            issue=iss,
            service=srv,
            priority=pri,
            requester_name=req_name,
            db_path=db_path
        )
        return {"ticket": ticket_resp.model_dump(), "is_new": is_new}

    else:
        raise ValueError(f"Unregistered tool call: '{clean_name}'. Tool execution rejected.")

def run_provider_tool_loop(
    user_message: str,
    context_data: Dict[str, Any],
    db_path=None,
    max_turns: int = 3,
    chat_history: Optional[List[Dict[str, str]]] = None
) -> Optional[str]:
    """
    Executes a bounded LLM tool-calling loop using Groq or Gemini.
    Dispatches only registered tools and falls back to None on error/offline mode.
    """
    # 1. Groq Provider Integration
    if settings.GROQ_API_KEY and settings.LLM_PROVIDER in ["groq", "all"]:
        try:
            from groq import Groq
            client = Groq(api_key=settings.GROQ_API_KEY)
            # Build messages: system + recent history + current user query
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            # Inject prior conversation turns (most recent 8 turns max to stay within token budget)
            if chat_history:
                for turn in chat_history[-8:]:
                    role = turn.get("role", "user")
                    content = turn.get("content", "")
                    if role in ("user", "assistant") and content:
                        messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": f"Context:\n{json.dumps(context_data, default=str)}\n\nUser Message: {user_message}"})

            for _ in range(max_turns):
                completion = client.chat.completions.create(
                    model=settings.GROQ_MODEL,
                    messages=messages,
                    tools=AGENT_TOOL_DEFINITIONS,
                    tool_choice="auto",
                    max_tokens=500,
                    temperature=0.2
                )
                msg = completion.choices[0].message
                if not msg.tool_calls:
                    return msg.content.strip() if msg.content else None

                # Append assistant tool calls
                messages.append(msg)

                # Process tool calls
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                        tool_result = execute_tool_call(fn_name, fn_args, db_path=db_path)
                    except Exception as ex:
                        tool_result = {"error": f"Tool execution failed: {str(ex)}"}

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "content": json.dumps(tool_result, default=str)
                    })

        except Exception as e:
            # Mask API keys and sensitive tokens from log
            logger.warning(f"Groq tool loop encountered an error, falling back: {type(e).__name__}")

    # 2. Gemini Provider Integration
    if settings.GEMINI_API_KEY and settings.LLM_PROVIDER in ["gemini", "all"]:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=settings.GEMINI_API_KEY)

            # Build Gemini tool declarations
            def make_gemini_tools():
                declarations = []
                for tool_def in AGENT_TOOL_DEFINITIONS:
                    fn = tool_def["function"]
                    declarations.append(
                        types.FunctionDeclaration(
                            name=fn["name"],
                            description=fn["description"],
                            parameters=fn["parameters"]
                        )
                    )
                return [types.Tool(function_declarations=declarations)]

            tools = make_gemini_tools()
            prompt = f"{SYSTEM_PROMPT}\n\nContext:\n{json.dumps(context_data, default=str)}\n\nUser Message: {user_message}"

            response = client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=tools,
                    temperature=0.2,
                    max_output_tokens=500
                )
            )

            # Handle candidate output
            if response.candidates:
                candidate = response.candidates[0]
                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if hasattr(part, "function_call") and part.function_call:
                            fn_call = part.function_call
                            call_name = fn_call.name
                            call_args = dict(fn_call.args) if fn_call.args else {}
                            # Execute tool
                            tool_res = execute_tool_call(call_name, call_args, db_path=db_path)
                            # Return explanation with tool grounding
                            return f"Tool {call_name} executed with result: {json.dumps(tool_res, default=str)}"
                        elif hasattr(part, "text") and part.text:
                            return part.text.strip()
        except Exception as e:
            logger.warning(f"Gemini tool loop encountered an error, falling back: {type(e).__name__}")

    return None

def generate_advisory_response(
    user_message: str,
    service: str,
    location: str,
    outage_info: Optional[Dict[str, Any]],
    steps: List[str],
    attempted_steps: List[str],
    history_info: Optional[List[Dict[str, Any]]] = None,
    document_citations: Optional[List[Dict[str, Any]]] = None,
    db_path=None,
    chat_history: Optional[List[Dict[str, str]]] = None
) -> Optional[str]:
    """
    Attempts to generate a grounded, polite advisory response via Groq or Gemini.
    Supplies context, outage info, approved guide citations, and history records.
    Falls back gracefully to None if LLMs are offline or unavailable.
    """
    context_data = {
        "employee_location": location or "Not provided",
        "affected_service": service,
        "outage_status": outage_info.get("status") if outage_info else "Not checked",
        "outage_detail": outage_info.get("message") if outage_info else "N/A",
        "outage_ref": outage_info.get("outage_ref") if outage_info else None,
        "outage_last_updated": outage_info.get("last_updated") if outage_info else None,
        "is_outage_stale": outage_info.get("is_stale", False) if outage_info else False,
        "approved_guide_steps": steps,
        "document_citations": document_citations or [],
        "previously_attempted_steps": attempted_steps,
        "historical_incidents": history_info or []
    }

    # First attempt tool loop if enabled (pass chat_history for multi-turn context)
    loop_result = run_provider_tool_loop(user_message, context_data, db_path=db_path, chat_history=chat_history)
    if loop_result and loop_result.strip():
        return loop_result.strip()

    # Context formatted prompt
    context_lines = [
        f"Employee Location: {context_data['employee_location']}",
        f"Affected Service: {service}",
        f"Outage Status: {context_data['outage_status']}",
        f"Outage Detail: {context_data['outage_detail']}",
        f"Available Approved Steps: {json.dumps(steps)}",
        f"Document Citations: {json.dumps(document_citations or [])}",
        f"Previously Attempted Steps: {json.dumps(attempted_steps)}",
        f"Historical Incidents: {json.dumps(history_info or [])}",
        f"Employee Query: {user_message}"
    ]
    prompt = "\n".join(context_lines)

    # Groq text completion fallback (with chat_history for multi-turn context)
    if settings.GROQ_API_KEY and settings.LLM_PROVIDER in ["groq", "all"]:
        try:
            from groq import Groq
            client = Groq(api_key=settings.GROQ_API_KEY)
            fallback_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            # Inject prior conversation turns for context
            if chat_history:
                for turn in chat_history[-8:]:
                    role = turn.get("role", "user")
                    content = turn.get("content", "")
                    if role in ("user", "assistant") and content:
                        fallback_messages.append({"role": role, "content": content})
            fallback_messages.append({
                "role": "user",
                "content": f"Based strictly on this data, provide a helpful advisory response for the employee:\n\n{prompt}"
            })
            completion = client.chat.completions.create(
                model=settings.GROQ_MODEL,
                messages=fallback_messages,
                max_tokens=400,
                temperature=0.2
            )
            content = completion.choices[0].message.content
            if content and content.strip():
                return content.strip()
        except Exception as e:
            logger.warning(f"Groq text fallback failed: {type(e).__name__}")

    # Gemini text completion fallback
    if settings.GEMINI_API_KEY and settings.LLM_PROVIDER in ["gemini", "all"]:
        try:
            import requests
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
            payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": f"{SYSTEM_PROMPT}\n\nContext:\n{prompt}"}]}
                ],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400}
            }
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts and "text" in parts[0]:
                        return parts[0]["text"].strip()
        except Exception as e:
            logger.warning(f"Gemini text fallback failed: {type(e).__name__}")

    return None
