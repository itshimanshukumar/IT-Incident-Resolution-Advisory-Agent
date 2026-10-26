
from agent.tools import check_network_status


def test_active_outage():
    result = check_network_status("Chennai", "Wi-Fi")
    assert result["status"] == "active"
    assert result["outage_ref"] == "OUT-104"


def test_resolved_outage():
    result = check_network_status("Pune", "Email")
    assert result["status"] == "resolved"


def test_unknown_outage():
    result = check_network_status("Mumbai", "Wi-Fi")
    assert result["status"] == "unknown"


def test_case_insensitive_lookup():
    result = check_network_status("chennai", "wi-fi")
    assert result["status"] == "active"


def test_missing_location():
    result = check_network_status("   ", "Wi-Fi")
    assert result["status"] == "missing_location"
