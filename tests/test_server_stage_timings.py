"""Server timing is measured independently from model writing and Actions latency."""
from app import main
from app.models import TurnPrepare


def test_prepare_response_exposes_backend_time_without_leaking_game_context(monkeypatch):
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
