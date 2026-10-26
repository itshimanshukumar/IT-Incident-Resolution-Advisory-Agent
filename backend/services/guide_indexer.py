import os
import json
import re
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from backend.config import settings

logger = logging.getLogger(__name__)


class GuideExtractionError(Exception):
    """Raised when text extraction from a guide file fails."""
    pass


class GuideValidationError(Exception):
    """Raised when extracted guide content fails structural or validation checks."""
    pass


def normalize_service_name(name: str) -> str:
    """Normalizes guide section or service text into canonical service names."""
    if not name:
        return "General"
    n = name.strip()
    if re.search(r"\bvpn\b", n, re.IGNORECASE):
        return "VPN"
    if re.search(r"wi-?fi|network|internet|wlan|lan", n, re.IGNORECASE):
        return "Wi-Fi"
    if re.search(r"print|printer|spooler", n, re.IGNORECASE):
        return "Printer"
    if re.search(r"email|mail|outlook|exchange", n, re.IGNORECASE):
        return "Email"
    return n.capitalize()


def extract_text_from_file(file_path: Path) -> str:
    """
    Extracts raw text from supported document types: MD, TXT, DOCX, PDF, JSON.
    Raises GuideExtractionError on missing files, corrupt data, or empty unreadable documents.
    """
    if not file_path.exists():
        raise GuideExtractionError(f"Guide file not found: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix in [".md", ".txt"]:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                raise GuideExtractionError(f"File '{file_path.name}' is empty.")
            return content
        except GuideExtractionError:
            raise
        except Exception as e:
            raise GuideExtractionError(f"Failed to read text file '{file_path.name}': {e}")

    elif suffix == ".docx":
        try:
            import docx
            doc = docx.Document(str(file_path))
            paras = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
            if not paras:
                raise GuideExtractionError(f"DOCX '{file_path.name}' contains no readable paragraphs.")
            return "\n".join(paras)
        except GuideExtractionError:
            raise
        except Exception as e:
            raise GuideExtractionError(f"Failed to read DOCX '{file_path.name}': {e}")

    elif suffix == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(str(file_path))
            if len(reader.pages) == 0:
                raise GuideExtractionError(f"PDF '{file_path.name}' has 0 pages.")
            pages_text = []
            for i, page in enumerate(reader.pages):
                try:
                    txt = page.extract_text() or ""
                    pages_text.append(txt)
                except Exception as pe:
                    logger.warning(f"Failed to extract page {i} of '{file_path.name}': {pe}")
            full_text = "\n".join(pages_text)
            if not full_text.strip():
                raise GuideExtractionError(f"PDF '{file_path.name}' contains no readable text across {len(reader.pages)} pages.")
            return full_text
        except GuideExtractionError:
            raise
        except Exception as e:
            raise GuideExtractionError(f"Corrupt or unreadable PDF '{file_path.name}': {e}")

    elif suffix == ".json":
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return json.dumps(data, indent=2)
        except Exception as e:
            raise GuideExtractionError(f"Failed to parse JSON '{file_path.name}': {e}")

    raise GuideExtractionError(f"Unsupported guide file format: '{suffix}'")


def parse_guide_chunks(
    text: str,
    source_doc: str,
    guide_id: str = "GUIDE-IT-2026-V1",
    guide_version: str = "1.0"
) -> List[Dict[str, Any]]:
    """
    Robust heading- and structure-aware parser for IT Incident Resolution Guides.
    Extracts structured chunks with:
    - guide_id, guide_version, source_document
    - service, section, topic, symptom, os
    - ordered troubleshooting steps with expected observations and step-level OS targets
    - searchable_text
    """
    if not text or not text.strip():
        return []

    # Clean text: normalize line endings and page breaks
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n\n")
    lines = [l.strip() for l in cleaned.split("\n")]
    # Filter pure pagination artifacts
    lines = [l for l in lines if l and not re.match(r"^Page\s+\d+(\s+of\s+\d+)?$", l, re.IGNORECASE)]

    chunks: List[Dict[str, Any]] = []

    # 1. Structure Check: Check if document has Section or Topic markers
    has_sections = bool(re.search(r"(?m)^(?:##\s+|SECTION\s+\d+[:\s]*|Section\s+\d+[:\s]*)", cleaned, re.IGNORECASE))

    if has_sections:
        section_pattern = r"(?m)^(?:##\s+|SECTION\s+\d+[:\s]*|Section\s+\d+[:\s]*)"
        raw_sections = re.split(section_pattern, cleaned, flags=re.IGNORECASE)

        for sec in raw_sections:
            if not sec.strip():
                continue
            sec_lines = [l.strip() for l in sec.strip().split("\n") if l.strip()]
            header = sec_lines[0]
            current_service = normalize_service_name(header)
            current_section = header

            # Split topics within section: '### Topic:', '### ', 'Topic:'
            topic_pattern = r"(?m)^(?:###\s+(?:Topic:\s*)?|Topic:\s*)"
            raw_topics = re.split(topic_pattern, "\n".join(sec_lines[1:]), flags=re.IGNORECASE)

            if len(raw_topics) == 1 and not re.search(topic_pattern, "\n".join(sec_lines[1:]), re.IGNORECASE):
                topic_blocks = [(header, raw_topics[0])]
            else:
                topic_blocks = []
                for tb in raw_topics:
                    if not tb.strip():
                        continue
                    tb_lines = [l.strip() for l in tb.strip().split("\n") if l.strip()]
                    t_title = tb_lines[0]
                    topic_blocks.append((t_title, tb))

            for t_title, block in topic_blocks:
                b_lines = [l.strip() for l in block.split("\n") if l.strip()]
                service = current_service
                symptom = ""
                os_target = "All"
                steps: List[Dict[str, Any]] = []

                for line in b_lines:
                    stripped = re.sub(r"^[-*•]\s*", "", line).strip()

                    # Metadata line parsing
                    if re.match(r"^\*\*Service:\*\*", stripped, re.IGNORECASE) or re.match(r"^Service:", stripped, re.IGNORECASE):
                        val = re.sub(r"^(?:\*\*Service:\*\*|Service:)\s*", "", stripped, flags=re.IGNORECASE).strip()
                        service = normalize_service_name(val)
                    elif re.match(r"^\*\*Symptom:\*\*", stripped, re.IGNORECASE) or re.match(r"^Symptom:", stripped, re.IGNORECASE):
                        symptom = re.sub(r"^(?:\*\*Symptom:\*\*|Symptom:)\s*", "", stripped, flags=re.IGNORECASE).strip()
                    elif re.match(r"^\*\*OS:\*\*", stripped, re.IGNORECASE) or re.match(r"^OS:", stripped, re.IGNORECASE):
                        os_target = re.sub(r"^(?:\*\*OS:\*\*|OS:)\s*", "", stripped, flags=re.IGNORECASE).strip()
                    elif re.match(r"^(?:\*\*Expected Observation:\*\*|Expected Observation:|Observation:|Expected:)", stripped, re.IGNORECASE):
                        obs = re.sub(r"^(?:\*\*Expected Observation:\*\*|Expected Observation:|Observation:|Expected:)\s*", "", stripped, flags=re.IGNORECASE).strip()
                        if steps:
                            steps[-1]["expected_observation"] = obs
                    else:
                        # Step detection
                        m_bold = re.match(r"^\*\*Step\s+(\d+)(?:\s+\[(.*?)\])?:\*\*\s*(.*)", stripped, re.IGNORECASE)
                        m_plain = re.match(r"^Step\s+(\d+)(?:\s+\[(.*?)\])?[:\-]\s*(.*)", stripped, re.IGNORECASE)
                        m_num = re.match(r"^(\d+)[\.\)]\s+(.*)", stripped)

                        if m_bold:
                            s_num = int(m_bold.group(1))
                            s_os = m_bold.group(2) or os_target
                            s_text = m_bold.group(3).strip()
                            steps.append({
                                "step_number": s_num,
                                "instruction": s_text,
                                "expected_observation": "",
                                "os_target": s_os
                            })
                        elif m_plain:
                            s_num = int(m_plain.group(1))
                            s_os = m_plain.group(2) or os_target
                            s_text = m_plain.group(3).strip()
                            steps.append({
                                "step_number": s_num,
                                "instruction": s_text,
                                "expected_observation": "",
                                "os_target": s_os
                            })
                        elif m_num and (len(steps) > 0 or re.search(r"\b(check|run|open|verify|restart|confirm|press|test)\b", stripped, re.IGNORECASE)):
                            s_num = int(m_num.group(1))
                            s_text = m_num.group(2).strip()
                            m_os = re.search(r"\[(Windows|macOS|All)\]", s_text, re.IGNORECASE)
                            s_os = m_os.group(1) if m_os else os_target
                            if m_os:
                                s_text = s_text.replace(m_os.group(0), "").strip()
                            steps.append({
                                "step_number": s_num,
                                "instruction": s_text,
                                "expected_observation": "",
                                "os_target": s_os
                            })

                if steps:
                    step_oses = set(s["os_target"] for s in steps)
                    if len(step_oses) == 1 and "All" not in step_oses:
                        os_target = list(step_oses)[0]

                    for s in steps:
                        if not s["expected_observation"]:
                            s["expected_observation"] = "Verify system response"

                    chunk_id = f"{source_doc}_{len(chunks)+1}"
                    searchable = f"{guide_id} v{guide_version} {service} {current_section} {t_title} {symptom} {os_target} " + " ".join(s["instruction"] for s in steps)
                    chunks.append({
                        "id": chunk_id,
                        "guide_id": guide_id,
                        "guide_version": guide_version,
                        "service": service,
                        "section": current_section,
                        "topic": t_title,
                        "symptom": symptom or t_title,
                        "os": os_target,
                        "steps": steps,
                        "source_document": source_doc,
                        "searchable_text": searchable
                    })

    # 2. Compact / Multi-service line format (such as starter_it_guide.pdf)
    if not chunks:
        service_groups: Dict[str, List[Dict[str, Any]]] = {}

        for line in lines:
            stripped = re.sub(r"^[-*•]\s*", "", line).strip()
            if not stripped:
                continue

            # Detect lines: "Service: Wi-Fi - Step 1: ..." or "Service: Wi-Fi Windows: Run..."
            m_comp = re.match(
                r"^Service:\s*([A-Za-z\-]+)(?:\s+(Windows|macOS|All))?\s*[:\-]\s*(?:Step\s+(\d+)[:\-]\s*)?(.*)",
                stripped,
                re.IGNORECASE
            )
            if m_comp:
                srv = normalize_service_name(m_comp.group(1))
                s_os = m_comp.group(2) or "All"
                s_num = int(m_comp.group(3)) if m_comp.group(3) else None
                s_text = m_comp.group(4).strip()
                if srv not in service_groups:
                    service_groups[srv] = []
                num = s_num if s_num else len(service_groups[srv]) + 1
                service_groups[srv].append({
                    "step_number": num,
                    "instruction": s_text,
                    "expected_observation": "Verify system response",
                    "os_target": s_os
                })
            elif re.match(r"^(?:Step\s+\d+|\d+\.)", stripped, re.IGNORECASE):
                current_srv = list(service_groups.keys())[-1] if service_groups else "General"
                if current_srv not in service_groups:
                    service_groups[current_srv] = []
                m_plain = re.match(r"^Step\s+(\d+)(?:\s+\[(.*?)\])?[:\-]\s*(.*)", stripped, re.IGNORECASE)
                if m_plain:
                    service_groups[current_srv].append({
                        "step_number": int(m_plain.group(1)),
                        "instruction": m_plain.group(3).strip(),
                        "expected_observation": "Verify system response",
                        "os_target": m_plain.group(2) or "All"
                    })

        for srv, s_list in service_groups.items():
            if not s_list:
                continue
            chunk_id = f"{source_doc}_{len(chunks)+1}"
            chunks.append({
                "id": chunk_id,
                "guide_id": guide_id,
                "guide_version": guide_version,
                "service": srv,
                "section": f"{srv} Troubleshooting",
                "topic": f"{srv} Incident Resolution",
                "symptom": f"{srv} connection or hardware issue",
                "os": "All",
                "steps": s_list,
                "source_document": source_doc,
                "searchable_text": f"{guide_id} v{guide_version} {srv} " + " ".join(s["instruction"] for s in s_list)
            })

    return chunks


def parse_markdown_guide_chunks(text: str, source_doc: str) -> List[Dict[str, Any]]:
    """Backward compatibility alias for parse_guide_chunks."""
    return parse_guide_chunks(text, source_doc)


class GuideIndexer:
    """
    Manages the indexing, validation, and retrieval lifecycle of approved IT guides.
    Enforces registry-driven active guide selection, deterministic rebuilding,
    freshness validation, and atomic persistence.
    """
    def __init__(self, guides_dir: Optional[Path] = None, index_file: Optional[Path] = None):
        self.guides_dir = guides_dir or settings.GUIDES_DIR
        self.index_file = index_file or settings.GUIDE_INDEX_FILE
        self.meta_file = settings.GUIDE_INDEX_META_FILE
        self.approved_file = settings.APPROVED_GUIDES_FILE
        self.index: List[Dict[str, Any]] = []
        self.last_result: Dict[str, Any] = {}
        self.load_index()

    def get_approved_guide_info(self) -> Dict[str, Any]:
        """Loads and parses the approved guides registry."""
        if self.approved_file.exists():
            try:
                return json.loads(self.approved_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"Error reading approval registry: {e}")
        return {"active_guide_id": None, "guides": []}

    def get_active_guide(self) -> Optional[Dict[str, Any]]:
        """Resolves the currently active guide from the approval registry."""
        info = self.get_approved_guide_info()
        active_id = info.get("active_guide_id")
        if not active_id:
            return None
        for g in info.get("guides", []):
            if g.get("id") == active_id and g.get("status") == "Active":
                return g
        return None

    def get_pending_review_guides(self) -> List[Dict[str, Any]]:
        """Returns guides and candidate files that are not active/approved."""
        info = self.get_approved_guide_info()
        active = self.get_active_guide()
        active_fname = active.get("filename") if active else None

        pending = []
        registered_files = set()

        for g in info.get("guides", []):
            registered_files.add(g.get("filename"))
            if g.get("status") != "Active" or g.get("id") != info.get("active_guide_id"):
                pending.append({
                    "id": g.get("id"),
                    "filename": g.get("filename"),
                    "version": g.get("version"),
                    "status": g.get("status", "Pending Review"),
                    "registered": True
                })

        # Also inspect filesystem for any guide files not registered or pending
        if self.guides_dir.exists():
            for f in self.guides_dir.iterdir():
                if f.is_file() and f.suffix.lower() in [".pdf", ".docx", ".md", ".txt"]:
                    if f.name != active_fname and f.name not in registered_files:
                        pending.append({
                            "id": "UNREGISTERED",
                            "filename": f.name,
                            "version": "Draft",
                            "status": "Pending Review",
                            "registered": False
                        })

        return pending

    def compute_file_hash(self, file_path: Path) -> str:
        """Computes SHA-256 hash of a file for cache freshness checking."""
        if not file_path.exists():
            return ""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def get_index_metadata(self) -> Dict[str, Any]:
        """Loads cached index metadata if available."""
        if self.meta_file.exists():
            try:
                return json.loads(self.meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def is_index_stale(self) -> bool:
        """Determines if the persisted index is missing or out of sync with active guide."""
        if not self.index_file.exists():
            return True
        active_guide = self.get_active_guide()
        if not active_guide:
            return True

        active_file = self.guides_dir / active_guide.get("filename", "")
        if not active_file.exists():
            return True

        meta = self.get_index_metadata()
        if not meta:
            return True

        if meta.get("active_guide_id") != active_guide.get("id"):
            return True
        if meta.get("active_guide_version") != active_guide.get("version"):
            return True
        if meta.get("source_filename") != active_guide.get("filename"):
            return True

        current_hash = self.compute_file_hash(active_file)
        if meta.get("content_hash") != current_hash:
            return True

        return False

    def build_and_save_index(self) -> Dict[str, Any]:
        """
        Deterministically indexes the active approved guide, validates chunks,
        and saves index atomically. Prevents corrupting index on failure.
        """
        self.guides_dir.mkdir(parents=True, exist_ok=True)
        active_guide = self.get_active_guide()

        if not active_guide:
            logger.warning("No guide marked 'Active' matching active_guide_id in registry.")
            self.index = []
            res = {
                "status": "error",
                "error": "No guide explicitly marked 'Active' in approved_guides.json.",
                "indexed_chunks": 0,
                "source_files": []
            }
            self.last_result = res
            return res

        active_filename = active_guide.get("filename")
        active_id = active_guide.get("id", "UNKNOWN-GUIDE")
        active_version = active_guide.get("version", "1.0")
        target_file = self.guides_dir / active_filename

        if not target_file.exists():
            err_msg = f"Active approved guide file '{active_filename}' does not exist on disk."
            logger.error(err_msg)
            res = {
                "status": "error",
                "error": err_msg,
                "active_guide_id": active_id,
                "active_guide_version": active_version,
                "source_filename": active_filename,
                "indexed_chunks": 0,
                "source_files": []
            }
            self.last_result = res
            return res

        # 1. Extract Raw Text
        try:
            raw_text = extract_text_from_file(target_file)
        except GuideExtractionError as gee:
            logger.error(f"Text extraction failed for active guide '{active_filename}': {gee}")
            res = {
                "status": "error",
                "error": str(gee),
                "active_guide_id": active_id,
                "active_guide_version": active_version,
                "source_filename": active_filename,
                "indexed_chunks": 0,
                "source_files": []
            }
            self.last_result = res
            return res

        # 2. Parse Chunks
        chunks = parse_guide_chunks(
            text=raw_text,
            source_doc=active_filename,
            guide_id=active_id,
            guide_version=active_version
        )

        # 3. Validate Chunks
        if not chunks:
            err_msg = f"Zero valid troubleshooting chunks extracted from '{active_filename}'."
            logger.error(err_msg)
            res = {
                "status": "error",
                "error": err_msg,
                "active_guide_id": active_id,
                "active_guide_version": active_version,
                "source_filename": active_filename,
                "indexed_chunks": 0,
                "source_files": []
            }
            self.last_result = res
            return res

        # 4. Atomic Persistence
        temp_index_file = self.index_file.with_suffix(".tmp")
        temp_meta_file = self.meta_file.with_suffix(".tmp")

        try:
            temp_index_file.write_text(json.dumps(chunks, indent=2), encoding="utf-8")
            os.replace(temp_index_file, self.index_file)

            meta = {
                "active_guide_id": active_id,
                "active_guide_version": active_version,
                "source_filename": active_filename,
                "content_hash": self.compute_file_hash(target_file),
                "indexed_chunks": len(chunks),
                "status": "success",
                "warnings": []
            }
            temp_meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            os.replace(temp_meta_file, self.meta_file)

            self.index = chunks
            res = {
                "status": "success",
                "active_guide_id": active_id,
                "active_guide_version": active_version,
                "source_filename": active_filename,
                "indexed_chunks": len(self.index),
                "source_files": [active_filename],
                "warnings": [],
                "pending_review_guides": self.get_pending_review_guides()
            }
            self.last_result = res
            logger.info(f"Successfully indexed {len(self.index)} chunks from approved guide '{active_filename}'.")
            return res

        except Exception as e:
            if temp_index_file.exists():
                try: temp_index_file.unlink()
                except Exception: pass
            if temp_meta_file.exists():
                try: temp_meta_file.unlink()
                except Exception: pass
            err_msg = f"Atomic index write failed: {e}"
            logger.error(err_msg)
            return {
                "status": "error",
                "error": err_msg,
                "indexed_chunks": 0,
                "source_files": []
            }

    def load_index(self):
        """Loads valid index from disk, rebuilding automatically if stale."""
        if self.is_index_stale():
            self.build_and_save_index()
            return

        if self.index_file.exists():
            try:
                self.index = json.loads(self.index_file.read_text(encoding="utf-8"))
                return
            except Exception as e:
                logger.warning(f"Corrupt index file found, rebuilding: {e}")
                self.build_and_save_index()


guide_indexer = GuideIndexer()
