from app import session_start_guard, storage


def test_session_start_guard_is_installed_on_storage_create_session():
    assert storage.create_session is session_start_guard.create_session


def test_direct_normalizer_lifts_flat_start_shape():
    prepared = session_start_guard.normalise_session_start(
        {
            "novel_id": "diag",
            "title": "Diag",
            "novel": {"pov_character": "rina"},
            "characters": [{"character_id": "rina", "name": "Рината", "is_pov": True}],
            "lore": {},
            "starting_state": {
                "pov": "Рината",
                "date": "03.09.2026",
                "time": "12:00",
                "location": "дом",
                "present_characters": ["Рината"],
            },
        }
    )
    assert prepared["starting_state"]["current"]["date"] == "03.09.2026"
    assert prepared["starting_state"]["current"]["time"] == "12:00"
    assert prepared["starting_state"]["current"]["location"] == "дом"
    assert prepared["starting_state"]["current"]["present_characters"] == ["rina"]
