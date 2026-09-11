from app import writer_first_runtime


def test_historical_relationship_footer_is_removed_from_writer_context():
    scene = """🎭 Test

Сцена с важным диалогом.

Состояние: спокойно
Отношения:
Эдриан - доверие 47; близость 62

Ход 9 · цикл 9/15"""
    stripped = writer_first_runtime._strip_relationship_display(scene)

    assert "Сцена с важным диалогом." in stripped
    assert "Эдриан - доверие 47" not in stripped
    assert "Отношения:" in stripped
    assert "Ход 9 · цикл 9/15" in stripped


def test_compact_recent_turn_does_not_reintroduce_old_relationship_numbers():
    turn = {
        "turn_number": 9,
        "user_input": "test",
        "scene_output": """🎭 Test

Сцена.

Состояние: спокойно
Отношения:
Эдриан - доверие 31

Ход 9 · цикл 9/15""",
        "extracted": {},
    }

    compact = writer_first_runtime._compact_full_turn(turn)

    assert "доверие 31" not in compact["scene_output"]
    assert compact["turn_number"] == 9
