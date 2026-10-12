import json

from apps.api import debug_capture


def test_complete_debug_run_writes_document_json_and_events(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_capture, "DEBUG_ROOT", tmp_path)
    monkeypatch.setenv("CCITECHECK_DEBUG_CAPTURE", "1")

    run_id = debug_capture.create_run("document", b"PK-test-docx")
    debug_capture.write_json(run_id, "response.json", {"中文": "完整"})
    debug_capture.append_event(run_id, {"event": "locate_error", "code": "AccessDenied"})

    directory = tmp_path / run_id
    assert (directory / "document.docx").read_bytes() == b"PK-test-docx"
    assert json.loads((directory / "response.json").read_text())["中文"] == "完整"
    event = json.loads((directory / "word-events.jsonl").read_text())
    assert event["code"] == "AccessDenied"


def test_debug_capture_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_capture, "DEBUG_ROOT", tmp_path)
    monkeypatch.setenv("CCITECHECK_DEBUG_CAPTURE", "0")
    assert debug_capture.create_run("document", b"secret") is None
    assert list(tmp_path.iterdir()) == []
