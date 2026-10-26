
from agent.retriever import retrieve_troubleshooting_steps


def test_retrieval_finds_printer_instructions():
    result = retrieve_troubleshooting_steps(
        service="Printer",
        operating_system="Windows",
        symptom="printer offline and not printing",
        top_k=3,
    )

    assert result["status"] == "found"
    assert len(result["results"]) > 0

    # At least one result should contain printer-related guidance.
    assert any(
        "printer" in item["text"].lower()
        for item in result["results"]
    )


def test_missing_service_returns_validation_error():
    result = retrieve_troubleshooting_steps(
        service="",
        operating_system="Windows",
        symptom="printer offline",
    )

    assert result["status"] == "validation_error"
