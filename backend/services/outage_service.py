import csv
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from backend.config import settings
from backend.models import OutageRecord

def normalize_service(service: str) -> str:
    if not service:
        return "Other"
    s = service.strip().lower()
    if re.search(r"\bvpn\b", s):
        return "VPN"
    if re.search(r"wi-?fi|network|internet|lan|ethernet|wlan", s):
        return "Wi-Fi"
    if re.search(r"print|printer|scanner|paper jam|toner|spooler", s):
        return "Printer"
    if re.search(r"email|mail|outlook|exchange|mailbox|inbox", s):
        return "Email"
    return service.strip()

def normalize_location(location: str) -> str:
    if not location:
        return ""
    return location.strip().capitalize()

def parse_timestamp(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(ts_str.strip(), fmt)
        except ValueError:
            continue
    return None

def check_network_status(
    location: Optional[str],
    service: Optional[str],
    custom_file: Optional[Path] = None,
    freshness_hours: Optional[int] = None
) -> OutageRecord:
    """
    Checks network and service outage status against configured outage records.
    Never assumes healthy if no record exists.
    """
    now = datetime.now()
    lookup_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    hours_threshold = freshness_hours if freshness_hours is not None else settings.FRESHNESS_HOURS

    # 1. Validate inputs
    if not location or not location.strip():
        return OutageRecord(
            location="Unknown",
            service=normalize_service(service or "Other"),
            status="Unknown",
            message="Location was not provided. Please provide your office location to check for recorded network outages.",
            lookup_timestamp=lookup_time_str
        )

    norm_loc = normalize_location(location)
    norm_srv = normalize_service(service or "Other")
    file_path = custom_file or settings.OUTAGES_FILE

    # 2. Check data source availability
    if not file_path.exists():
        return OutageRecord(
            location=norm_loc,
            service=norm_srv,
            status="Error",
            warning="Outage data file is missing or inaccessible.",
            message="Outage data source is temporarily unavailable. Cannot verify network status. Please proceed with caution.",
            lookup_timestamp=lookup_time_str
        )

    # 3. Read and filter records
    matching_records = []
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            # Validate required columns
            expected_cols = {"outage_ref", "location", "service", "status", "update_text", "last_updated"}
            if not reader.fieldnames or not expected_cols.issubset(set(f.strip() for f in reader.fieldnames)):
                return OutageRecord(
                    location=norm_loc,
                    service=norm_srv,
                    status="Error",
                    warning="Outage data format is malformed.",
                    message="Outage records could not be read due to a schema mismatch. Cannot verify outage status.",
                    lookup_timestamp=lookup_time_str
                )

            for row in reader:
                row_loc = (row.get("location") or "").strip().lower()
                row_srv = normalize_service(row.get("service") or "")
                
                if row_loc == norm_loc.lower() and row_srv == norm_srv:
                    matching_records.append(row)
    except Exception as e:
        return OutageRecord(
            location=norm_loc,
            service=norm_srv,
            status="Error",
            warning=f"Error reading outages file: {str(e)}",
            message="Failed to read the outage database. Cannot confirm if a network outage exists.",
            lookup_timestamp=lookup_time_str
        )

    # 4. If no records matched
    if not matching_records:
        return OutageRecord(
            location=norm_loc,
            service=norm_srv,
            status="Unknown",
            message=f"I found no matching outage record for {norm_srv} in {norm_loc}; status is unknown, not confirmed healthy.",
            lookup_timestamp=lookup_time_str
        )

    # 5. Prioritize most recent / active record
    # Sort matching records by last_updated descending if possible
    def get_sort_key(rec):
        dt = parse_timestamp(rec.get("last_updated", ""))
        return dt or datetime.min

    matching_records.sort(key=get_sort_key, reverse=True)
    selected = matching_records[0]

    raw_status = (selected.get("status") or "").strip()
    outage_ref = (selected.get("outage_ref") or "").strip()
    update_text = (selected.get("update_text") or "").strip()
    last_updated = (selected.get("last_updated") or "").strip()

    # 6. Freshness check
    is_stale = False
    warning = None
    parsed_dt = parse_timestamp(last_updated)
    if parsed_dt:
        age_hours = (now - parsed_dt).total_seconds() / 3600.0
        if age_hours > hours_threshold:
            is_stale = True
            warning = f"Notice: Outage information was last updated {last_updated} (over {int(age_hours)} hours ago) and may be stale."

    # 7. Format output based on status
    if raw_status.lower() == "active":
        msg = f"A recorded active outage matches {norm_srv} in {norm_loc}: {update_text} Reference {outage_ref}; last updated {last_updated}."
        if is_stale:
            msg += f" [{warning}]"
        msg += " I won’t suggest ordinary troubleshooting while this outage is active. If needed, I can escalate to a support ticket."
        return OutageRecord(
            outage_ref=outage_ref,
            location=norm_loc,
            service=norm_srv,
            status="Active",
            update_text=update_text,
            last_updated=last_updated,
            is_stale=is_stale,
            warning=warning,
            message=msg,
            lookup_timestamp=lookup_time_str
        )
    elif raw_status.lower() == "resolved":
        msg = f"A matching outage is marked resolved ({outage_ref}). Recorded resolution: {update_text} (Last updated: {last_updated})."
        if is_stale:
            msg += f" [{warning}]"
        msg += " Continuing with guided troubleshooting since your issue persists."
        return OutageRecord(
            outage_ref=outage_ref,
            location=norm_loc,
            service=norm_srv,
            status="Resolved",
            update_text=update_text,
            last_updated=last_updated,
            is_stale=is_stale,
            warning=warning,
            message=msg,
            lookup_timestamp=lookup_time_str
        )
    else:
        # Ambiguous / Unknown recorded status
        return OutageRecord(
            outage_ref=outage_ref,
            location=norm_loc,
            service=norm_srv,
            status="Unknown",
            update_text=update_text,
            last_updated=last_updated,
            is_stale=is_stale,
            warning=warning or "Outage status recorded is unverified or ambiguous.",
            message=f"Outage record {outage_ref} exists for {norm_srv} in {norm_loc}, but status '{raw_status}' cannot be confirmed healthy.",
            lookup_timestamp=lookup_time_str
        )


def list_all_outages(custom_file: Optional[Path] = None, freshness_hours: Optional[int] = None) -> List[Dict[str, Any]]:
    """Lists all outage records from the CSV file with freshness calculation."""
    file_path = custom_file or settings.OUTAGES_FILE
    if not file_path.exists():
        return []
    records = []
    now = datetime.now()
    hours_threshold = freshness_hours if freshness_hours is not None else settings.FRESHNESS_HOURS
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                item = dict(row)
                last_updated = (item.get("last_updated") or "").strip()
                parsed_dt = parse_timestamp(last_updated)
                is_stale = False
                if parsed_dt:
                    age_hours = (now - parsed_dt).total_seconds() / 3600.0
                    if age_hours > hours_threshold:
                        is_stale = True
                item["is_stale"] = is_stale
                records.append(item)
    except Exception as e:
        pass
    return records


def upsert_outage(
    location: str,
    service: str,
    status: str,
    update_text: str,
    outage_ref: Optional[str] = None,
    custom_file: Optional[Path] = None
) -> Dict[str, Any]:
    """Creates a new outage or updates an existing outage in outages.csv."""
    file_path = custom_file or settings.OUTAGES_FILE
    file_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    fieldnames = ["outage_ref", "location", "service", "status", "update_text", "last_updated"]
    if file_path.exists():
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                fieldnames = [c.strip() for c in reader.fieldnames]
            rows = [dict(r) for r in reader]

    norm_loc = normalize_location(location)
    norm_srv = normalize_service(service)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    norm_status = status.capitalize() if status.lower() in ["active", "resolved"] else status.strip()

    updated_record = None
    if outage_ref and outage_ref.strip():
        clean_ref = outage_ref.strip().upper()
        for r in rows:
            if (r.get("outage_ref") or "").strip().upper() == clean_ref:
                r["location"] = norm_loc
                r["service"] = norm_srv
                r["status"] = norm_status
                r["update_text"] = update_text.strip()
                r["last_updated"] = now_str
                updated_record = dict(r)
                break

    if not updated_record:
        if outage_ref and outage_ref.strip():
            ref = outage_ref.strip().upper()
        else:
            existing_nums = []
            for r in rows:
                m = re.search(r"OUT-(\d+)", r.get("outage_ref", ""), re.IGNORECASE)
                if m:
                    existing_nums.append(int(m.group(1)))
            next_num = max(existing_nums, default=100) + 1
            ref = f"OUT-{next_num:03d}"

        new_row = {
            "outage_ref": ref,
            "location": norm_loc,
            "service": norm_srv,
            "status": norm_status,
            "update_text": update_text.strip(),
            "last_updated": now_str
        }
        rows.append(new_row)
        updated_record = new_row

    with open(file_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return updated_record


def toggle_outage_status(outage_ref: str, custom_file: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Toggles status between Active and Resolved for the specified outage_ref."""
    file_path = custom_file or settings.OUTAGES_FILE
    if not file_path.exists():
        return None

    rows = []
    fieldnames = ["outage_ref", "location", "service", "status", "update_text", "last_updated"]
    with open(file_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            fieldnames = [c.strip() for c in reader.fieldnames]
        rows = [dict(r) for r in reader]

    clean_ref = outage_ref.strip().upper()
    target_row = None
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    for r in rows:
        if (r.get("outage_ref") or "").strip().upper() == clean_ref:
            current_status = (r.get("status") or "").strip()
            r["status"] = "Resolved" if current_status.lower() == "active" else "Active"
            r["last_updated"] = now_str
            target_row = dict(r)
            break

    if target_row:
        with open(file_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return target_row
