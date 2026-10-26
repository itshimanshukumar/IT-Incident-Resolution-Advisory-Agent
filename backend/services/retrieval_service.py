import re
from typing import List, Dict, Any, Optional
from backend.models import GuideStep, GuideRetrievalResult
from backend.services.guide_indexer import guide_indexer
from backend.services.outage_service import normalize_service
from backend.config import settings


def calculate_relevance_score(
    chunk: Dict[str, Any],
    target_service: str,
    symptom_tokens: List[str],
    target_os: str
) -> float:
    """
    Evaluates relevance of a guide chunk.
    Enforces strict service matching and grades symptom + OS relevance.
    """
    chunk_service = chunk.get("service", "General")

    # Strict service isolation: Printer issue must NEVER return Wi-Fi instructions,
    # and VPN must NEVER return Wi-Fi or Printer instructions.
    if target_service != "Other" and chunk_service != "General":
        if target_service.lower() != chunk_service.lower():
            return 0.0

    score = 10.0  # Base score for service match

    # OS Compatibility
    chunk_os = (chunk.get("os") or "All").lower()
    t_os = (target_os or "All").lower()
    if t_os in ["windows", "macos"]:
        if chunk_os == t_os:
            score += 15.0  # High bonus for exact OS match
        elif chunk_os == "all":
            score += 5.0
        else:
            # Different OS: reject
            return 0.0
    else:
        if chunk_os == "all":
            score += 8.0

    # Symptom token overlap
    searchable = (chunk.get("searchable_text") or "").lower()
    topic = (chunk.get("topic") or "").lower()
    chunk_symptom = (chunk.get("symptom") or "").lower()

    matched_tokens = 0
    for tok in symptom_tokens:
        if tok in chunk_symptom or tok in topic:
            score += 10.0
            matched_tokens += 1
        elif tok in searchable:
            score += 4.0
            matched_tokens += 1

    if symptom_tokens and matched_tokens == 0 and target_service == "Other":
        return 0.0

    return score


def rewrite_query(symptom: str, attempt: int) -> str:
    """
    Bounded query rewriting: normalizes user phrasing into canonical technical keywords.
    """
    s = symptom.lower()
    if attempt == 1:
        # First rewrite: expand common colloquial expressions
        s = re.sub(r"can't connect|cannot join|won't connect|down|broken", "connection failure offline", s)
        s = re.sub(r"not printing|stuck|paper issue", "printer offline spooler paper jam", s)
        s = re.sub(r"not getting mails|can't send|outlook error", "email sync send receive disconnected", s)
        s = re.sub(r"vpn failing|vpn down|cannot tunnel", "vpn connection gateway authorization", s)
    elif attempt == 2:
        # Second rewrite: reduce to core root terms
        s = re.sub(r"\b(my|the|is|are|not|having|problem|issue|please|help)\b", " ", s)
        s = " ".join(s.split())
    return s


def retrieve_troubleshooting_steps(
    service: Optional[str] = None,
    symptom: Optional[str] = None,
    os_name: Optional[str] = "All",
    max_steps: int = 5
) -> GuideRetrievalResult:
    """
    Retrieves and grades IT troubleshooting instructions with bounded query rewriting (up to 2 retries).
    Enforces service isolation, OS filtering (at both chunk and step levels),
    and traceability to the approved guide ID and version.
    """
    norm_service = normalize_service(service or "Other")
    curr_symptom = (symptom or "").strip()
    norm_os = (os_name or "All").strip()
    if norm_os.lower() not in ["windows", "macos"]:
        norm_os = "All"

    # Ensure guide index is loaded
    if not guide_indexer.index:
        guide_indexer.load_index()

    # Special handling for unsupported services like VPN
    if norm_service == "VPN":
        has_vpn_chunks = any(c.get("service") == "VPN" for c in guide_indexer.index)
        if not has_vpn_chunks:
            return GuideRetrievalResult(
                service="VPN",
                symptom=curr_symptom,
                matched_topic=None,
                source_document=None,
                guide_id=None,
                guide_version=None,
                steps=[],
                is_relevant=False,
                explanation="VPN incidents are not supported by the currently approved IT guide. Direct escalation to Network Engineering is required."
            )

    best_chunk = None
    best_score = 0.0
    max_rewrites = settings.MAX_REWRITE_ATTEMPTS
    attempts_done = 0

    # Iterative bounded retrieval loop
    query_to_search = curr_symptom
    while attempts_done <= max_rewrites:
        symptom_tokens = [w.lower() for w in re.split(r"\W+", query_to_search) if len(w) > 2]

        candidates = []
        for chunk in guide_indexer.index:
            score = calculate_relevance_score(chunk, norm_service, symptom_tokens, norm_os)
            if score > 0.0:
                candidates.append((score, chunk))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            top_score, top_chunk = candidates[0]
            # Acceptance threshold: must have meaningful score
            if top_score >= 10.0:
                best_chunk = top_chunk
                best_score = top_score
                break

        attempts_done += 1
        if attempts_done <= max_rewrites:
            query_to_search = rewrite_query(curr_symptom, attempts_done)

    # If no chunk met relevance criteria, reject safely
    if not best_chunk:
        return GuideRetrievalResult(
            service=norm_service,
            symptom=curr_symptom,
            matched_topic=None,
            source_document=None,
            guide_id=None,
            guide_version=None,
            steps=[],
            is_relevant=False,
            explanation=f"No relevant instructions found in approved IT guides for {norm_service} matching symptom '{curr_symptom}'. Query rewritten {attempts_done} times before rejection."
        )

    # Format and filter steps from best matching chunk
    steps_list = []
    guide_id = best_chunk.get("guide_id") or "GUIDE-IT"
    guide_ver = best_chunk.get("guide_version") or "1.0"
    source_doc = best_chunk.get("source_document", "IT Guide")
    section_name = best_chunk.get("section", "")
    source_ref = f"{guide_id} v{guide_ver} ({source_doc}) - {section_name}"
    raw_steps = best_chunk.get("steps", [])

    for s in raw_steps:
        step_os = s.get("os_target", "All")
        # Step-level OS filtering: don't return Windows-only steps to macOS users or vice versa
        if norm_os != "All":
            if step_os.lower() not in [norm_os.lower(), "all"]:
                continue

        steps_list.append(
            GuideStep(
                step_number=s.get("step_number", len(steps_list) + 1),
                instruction=s.get("instruction", ""),
                expected_observation=s.get("expected_observation") or "Verify system reaction",
                os_target=step_os,
                source_document=source_doc,
                source_section=section_name,
                source_reference=source_ref,
                guide_id=guide_id,
                guide_version=guide_ver
            )
        )
        if len(steps_list) >= max_steps:
            break

    return GuideRetrievalResult(
        service=norm_service,
        symptom=curr_symptom,
        matched_topic=best_chunk.get("topic"),
        source_document=source_doc,
        guide_id=guide_id,
        guide_version=guide_ver,
        steps=steps_list,
        is_relevant=True,
        explanation=f"Matched relevant instructions from topic '{best_chunk.get('topic')}' in {source_doc} (Relevance score: {best_score:.1f}, rewrites: {attempts_done})."
    )
