import sqlite3
import json
import hashlib
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from backend.config import settings

_hasher = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=1)

def hash_password(password: str) -> str:
    """Return Argon2id hash of password with a cryptographically secure random unique salt."""
    return _hasher.hash(password)

def verify_password_hash(plain_password: str, stored_hash: str) -> bool:
    """
    Verifies a plain password against an Argon2id hash.
    Safely falls back to legacy SHA-256 verification for migration.
    """
    if not stored_hash or not plain_password:
        return False
    if stored_hash.startswith("$argon2"):
        try:
            return _hasher.verify(stored_hash, plain_password)
        except (VerifyMismatchError, InvalidHashError):
            return False
        except Exception:
            return False

    # Legacy SHA-256 verification fallback
    legacy_salt = "ResolveIT_2026_Secure_Salt"
    legacy_hash = hashlib.sha256((legacy_salt + plain_password).encode("utf-8")).hexdigest()
    return legacy_hash == stored_hash

def check_db_health(db_path: Optional[Path] = None) -> bool:
    """Executes an active probe on the SQLite database to verify connectivity."""
    try:
        conn = get_connection(db_path)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        row = cur.fetchone()
        conn.close()
        return bool(row and row[0] == 1)
    except Exception:
        return False

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target = db_path or settings.DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[Path] = None):
    conn = get_connection(db_path)
    cur = conn.cursor()
    
    # 1. Users table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        name TEXT NOT NULL,
        department TEXT NOT NULL,
        location TEXT NOT NULL,
        role TEXT NOT NULL
    )
    """)
    
    # 2. Tickets table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        ticket_id TEXT PRIMARY KEY,
        idempotency_key TEXT UNIQUE,
        employee_id TEXT NOT NULL,
        requester_name TEXT,
        guest_session_id TEXT,
        location TEXT NOT NULL,
        service TEXT NOT NULL,
        issue TEXT NOT NULL,
        priority TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Open',
        department TEXT NOT NULL,
        assigned_technician TEXT,
        attempted_steps TEXT,
        outage_ref TEXT,
        guide_ref TEXT,
        resolution_notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    # Lightweight schema migration for existing databases
    cur.execute("PRAGMA table_info(tickets)")
    existing_cols = {row[1] for row in cur.fetchall()}
    if "requester_name" not in existing_cols:
        cur.execute("ALTER TABLE tickets ADD COLUMN requester_name TEXT")
    if "guest_session_id" not in existing_cols:
        cur.execute("ALTER TABLE tickets ADD COLUMN guest_session_id TEXT")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_employee ON tickets(employee_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_idempotency ON tickets(idempotency_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_guest_session ON tickets(guest_session_id)")

    # 3. Ticket History / Audit table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ticket_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id TEXT NOT NULL,
        action TEXT NOT NULL,
        performed_by TEXT NOT NULL,
        details TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
    )
    """)
    
    # 4. System Audit Logs
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        status TEXT NOT NULL,
        details TEXT
    )
    """)
    
    # 5. Conversation State Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY,
        incident_id TEXT,
        employee_id TEXT,
        requester_name TEXT,
        guest_session_id TEXT,
        location TEXT,
        service TEXT,
        symptom TEXT,
        os_name TEXT,
        outage_status TEXT,
        outage_ref TEXT,
        outage_checked_at TEXT,
        outage_text TEXT,
        suggested_steps TEXT,
        attempted_steps TEXT,
        completed_steps TEXT,
        failed_steps TEXT,
        skipped_steps TEXT,
        resolution_status TEXT DEFAULT 'in_progress',
        ticket_id TEXT,
        ticket_creation_status TEXT DEFAULT 'none',
        ticket_status TEXT DEFAULT 'none',
        escalation_status TEXT DEFAULT 'none',
        last_prompt_type TEXT DEFAULT 'initial',
        retrieved_steps TEXT,
        is_resolved INTEGER DEFAULT 0,
        prev_incident_id TEXT,
        prev_incident_service TEXT,
        prev_incident_symptom TEXT,
        prev_incident_location TEXT,
        prev_incident_resolved INTEGER DEFAULT 0,
        ticket_intake_state TEXT DEFAULT 'none',
        created_at TEXT,
        updated_at TEXT
    )
    """)

    # Automatic schema migration for existing databases
    cur.execute("PRAGMA table_info(conversations)")
    existing_cols = {row["name"] for row in cur.fetchall()}
    columns_to_add = [
        ("incident_id", "TEXT"),
        ("outage_checked_at", "TEXT"),
        ("outage_text", "TEXT"),
        ("suggested_steps", "TEXT"),
        ("completed_steps", "TEXT"),
        ("failed_steps", "TEXT"),
        ("skipped_steps", "TEXT"),
        ("resolution_status", "TEXT DEFAULT 'in_progress'"),
        ("ticket_id", "TEXT"),
        ("ticket_creation_status", "TEXT DEFAULT 'none'"),
        ("ticket_status", "TEXT DEFAULT 'none'"),
        ("escalation_status", "TEXT DEFAULT 'none'"),
        ("last_prompt_type", "TEXT DEFAULT 'initial'"),
        ("requester_name", "TEXT"),
        ("guest_session_id", "TEXT"),
        ("prev_incident_id", "TEXT"),
        ("prev_incident_service", "TEXT"),
        ("prev_incident_symptom", "TEXT"),
        ("prev_incident_location", "TEXT"),
        ("prev_incident_resolved", "INTEGER DEFAULT 0"),
        ("ticket_intake_state", "TEXT DEFAULT 'none'"),
        ("chat_history", "TEXT DEFAULT '[]'"),
    ]
    for col_name, col_type in columns_to_add:
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE conversations ADD COLUMN {col_name} {col_type}")

    # Migrate tickets table columns
    cur.execute("PRAGMA table_info(tickets)")
    ticket_cols = {row["name"] for row in cur.fetchall()}
    ticket_columns_to_add = [
        ("requester_name", "TEXT"),
        ("guest_session_id", "TEXT"),
    ]
    for col_name, col_type in ticket_columns_to_add:
        if col_name not in ticket_cols:
            cur.execute(f"ALTER TABLE tickets ADD COLUMN {col_name} {col_type}")

    # Seed initial demo users if not present
    seed_users = [
        ("E1001", hash_password("Employee@123"), "Aarav Sharma", "Finance", "Bhopal", "employee"),
        ("E1002", hash_password("Employee@123"), "Priya Verma", "Operations", "Pune", "employee"),
        ("IT1001", hash_password("Admin@123"), "IT Administrator", "IT Support", "Bhopal", "it")
    ]
    for u in seed_users:
        cur.execute("INSERT OR IGNORE INTO users (id, password_hash, name, department, location, role) VALUES (?, ?, ?, ?, ?, ?)", u)

    # Seamlessly upgrade any existing legacy SHA-256 hashes for demo accounts
    legacy_known = {"E1001": "Employee@123", "E1002": "Employee@123", "IT1001": "Admin@123"}
    cur.execute("SELECT id, password_hash FROM users")
    for row in cur.fetchall():
        uid = row["id"]
        phash = row["password_hash"]
        if uid in legacy_known and not phash.startswith("$argon2"):
            new_argon2 = hash_password(legacy_known[uid])
            cur.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_argon2, uid))
        
    conn.commit()
    conn.close()


def log_audit(user_id: str, action: str, target: str, status: str, details: str = "", db_path: Optional[Path] = None):
    conn = get_connection(db_path)
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "INSERT INTO audit_logs (timestamp, user_id, action, target, status, details) VALUES (?, ?, ?, ?, ?, ?)",
        (now, user_id, action, target, status, details)
    )
    conn.commit()
    conn.close()

def sync_tickets_to_csv(db_path: Optional[Path] = None, csv_path: Optional[Path] = None):
    """Safely sync SQLite tickets to tickets.csv for data persistence and inspection."""
    target_csv = csv_path or settings.TICKETS_FILE
    target_csv.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT ticket_id, employee_id, location, service, issue, priority, status, department, assigned_technician, attempted_steps, resolution_notes, created_at, updated_at FROM tickets ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    
    with open(target_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticket_id", "employee_id", "location", "service", "issue", "priority", "status", "department", "assigned_technician", "attempted_steps", "resolution_notes", "created_at", "updated_at"])
        for r in rows:
            writer.writerow(list(r))
