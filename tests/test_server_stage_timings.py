"""Server timing is measured independently from model writing and Actions latency."""
from app import main
from app.models import CommitTurnRequest, TurnPrepare


def test_prepare_response_exposes_backend_time_without_leaking_game_context(monkeypatch, caplog):
    caplog.set_level("INFO", logger="uvicorn.error")
    original = {"packet_id": "safe", "chunk_count": 2, "content": "{}"}
    def fake_prepare(session_id, user_input, request_id, **options):
        assert session_id == "test-only"
        assert user_input == "Привет"
        return original
    monkeypatch.setattr(main, "prepare_turn_request", fake_prepare)
    outcome = main.turn_packet_prepare("test-only", TurnPrepare(user_input="Привет"))
    assert isinstance(outcome["server_prepare_ms"], float)
    assert 0 <= outcome["server_prepare_ms"] < 10000
    assert outcome["packet_id"] == "safe"
    assert "server_prepare_ms" not in original
    assert "operation=prepare" in caplog.text
    assert "chunk_count=2" in caplog.text
    assert "Привет" not in caplog.text
    assert "test-only" not in caplog.text
    assert "safe" not in caplog.text


def test_chunk_timing_logs_only_sizes_and_preserves_transport(monkeypatch, caplog):
    caplog.set_level("INFO", logger="uvicorn.error")
    original = {"content": "Личная память", "chunk_count": 3, "chunk_index": 1}
    monkeypatch.setattr(main, "get_turn_packet_chunk", lambda *args: original)
    ticks = iter([10.0, 10.125])
    monkeypatch.setattr(main, "perf_counter", lambda: next(ticks))
    assert main.turn_packet_chunk_get("private-session", "private-packet", 1) is original
    assert "duration_ms=125.00 chunk_index=1 chunk_count=3 content_chars=13" in caplog.text
    for private in ("Личная память", "private-session", "private-packet"):
        assert private not in caplog.text


def test_commit_timing_preserves_receipt_without_logging_scene(monkeypatch, caplog):
    caplog.set_level("INFO", logger="uvicorn.error")
    original = {"ok": True, "turn_number": 7}
    monkeypatch.setattr(main, "commit_turn_request", lambda *args: original)
    ticks = iter([20.0, 20.25])
    monkeypatch.setattr(main, "perf_counter", lambda: next(ticks))
    result = main.turns_commit("private-session", CommitTurnRequest(
        packet_id="private-packet", user_input="Личный ввод", scene_output="Личная сцена", extracted={},
    ))
    assert result == {**original, "server_commit_ms": 250.0}
    assert original == {"ok": True, "turn_number": 7}
    assert "operation=commit duration_ms=250.0" in caplog.text
    for private in ("private-session", "private-packet", "Личный ввод", "Личная сцена"):
        assert private not in caplog.text
