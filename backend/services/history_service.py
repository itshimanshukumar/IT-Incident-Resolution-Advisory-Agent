import csv
from pathlib import Path
from typing import List, Optional
from backend.config import settings
from backend.models import HistoryItem
from backend.services.outage_service import normalize_location, normalize_service

def search_incident_history(
    location: Optional[str] = None,
    service: Optional[str] = None,
    symptom: Optional[str] = None,
    custom_file: Optional[Path] = None
) -> List[HistoryItem]:
    """
    Search historical incident and ticket records.
    Strictly distinguishes historical incidents from active outages.
    """
    file_path = custom_file or settings.INCIDENTS_FILE
    if not file_path.exists():
        return []

    norm_loc = normalize_location(location) if location else None
    norm_srv = normalize_service(service) if service else None
    symptom_tokens = [w.lower() for w in (symptom or "").split() if len(w) > 3]

    results: List[HistoryItem] = []
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_loc = normalize_location(row.get("location", ""))
                row_srv = normalize_service(row.get("service", ""))
                row_symptom = (row.get("symptom", "")).lower()

                # Filter criteria
                loc_match = (not norm_loc) or (row_loc.lower() == norm_loc.lower())
                srv_match = (not norm_srv) or (row_srv == norm_srv)
                
                # Symptom keyword overlap
                symptom_match = True
                if symptom_tokens:
                    symptom_match = any(token in row_symptom for token in symptom_tokens)

                if loc_match and srv_match and symptom_match:
                    results.append(
                        HistoryItem(
                            incident_id=row.get("incident_id", "").strip(),
                            location=row_loc,
                            service=row_srv,
                            symptom=row.get("symptom", "").strip(),
                            resolution=row.get("resolution", "").strip(),
                            status=row.get("status", "Resolved").strip(),
                            created_at=row.get("created_at", "").strip()
                        )
                    )
    except Exception:
        return []

    return results
