from app import startup_migration


def test_fetch_turns_accepts_main_api_wrapper(monkeypatch):
    calls = []

    def fake_get_json(url: str, timeout: int = 60):
        calls.append(url)
        return {
            "turns": [
                {"turn_number": 1, "scene_output": "one"},
                {"turn_number": 2, "scene_output": "two"},
            ]
        }

    monkeypatch.setattr(startup_migration, "_get_json", fake_get_json)

    turns = startup_migration._fetch_turns(
        "https://example.test",
        "session-1",
        2,
        batch_size=50,
    )

    assert [row["turn_number"] for row in turns] == [1, 2]
    assert len(calls) == 1
    assert "start_turn=1" in calls[0]
    assert "end_turn=2" in calls[0]


def test_fetch_turns_keeps_legacy_list_compatibility(monkeypatch):
    monkeypatch.setattr(
        startup_migration,
        "_get_json",
        lambda url, timeout=60: [{"turn_number": 1}],
    )

    turns = startup_migration._fetch_turns(
        "https://example.test",
        "session-1",
        1,
        batch_size=50,
    )

    assert turns == [{"turn_number": 1}]
