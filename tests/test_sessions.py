
import csv
from agent import sessions


def test_create_and_read_session(tmp_path, monkeypatch):
    test_file = tmp_path / "sessions.csv"

    monkeypatch.setattr(sessions, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sessions, "SESSIONS_FILE", test_file)

    result = sessions.create_session(
        employee_id="E1001",
        location="Pune",
        service="Printer",
        operating_system="Windows",
        symptom="Printer offline",
    )

    assert result["status"] == "created"

    session_id = result["session"]["session_id"]
    saved = sessions.get_session(session_id)

    assert saved is not None
    assert saved["employee_id"] == "E1001"
    assert saved["attempt_count"] == 0
    assert saved["attempted_steps"] == []


def test_record_attempt_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        sessions, "SESSIONS_FILE", tmp_path / "sessions.csv"
    )

    created = sessions.create_session(
        employee_id="E1001",
        location="Pune",
        service="Printer",
        operating_system="Windows",
        symptom="Printer offline",
    )

    session_id = created["session"]["session_id"]
    step = "Check the printer power."

    first = sessions.record_attempt(session_id, step)
    second = sessions.record_attempt(session_id, step)

    assert first["status"] == "recorded"
    assert second["status"] == "existing"
    assert second["session"]["attempt_count"] == 1
    assert second["session"]["attempted_steps"] == [step]


def test_update_status_and_ticket(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        sessions, "SESSIONS_FILE", tmp_path / "sessions.csv"
    )

    created = sessions.create_session(
        employee_id="E1001",
        location="Pune",
        service="Printer",
        operating_system="Windows",
        symptom="Printer offline",
    )

    session_id = created["session"]["session_id"]

    result = sessions.update_session_status(
        session_id,
        "Escalated",
        ticket_id="T-0001",
    )

    assert result["status"] == "updated"
    assert result["session"]["status"] == "Escalated"
    assert result["session"]["ticket_id"] == "T-0001"


def test_missing_session(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        sessions, "SESSIONS_FILE", tmp_path / "sessions.csv"
    )

    assert sessions.get_session("S-NOTFOUND") is None


def test_missing_required_information(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        sessions, "SESSIONS_FILE", tmp_path / "sessions.csv"
    )

    result = sessions.create_session(
        employee_id="E1001",
        location="",
        service="Printer",
        operating_system="Windows",
        symptom="Printer offline",
    )

    assert result["status"] == "validation_error"
    assert "location" in result["missing_fields"]
