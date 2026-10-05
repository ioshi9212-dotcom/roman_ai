from app.runtime_access import runtime_documents


def test_scene_builder_hands_off_only_when_player_is_needed():
    builder = runtime_documents()["scene_builder"]
    assert "Если POV может естественно продолжать без нового значимого решения - продолжай." in builder
    assert "доведи его до следующего содержательного момента" in builder
    assert "Остановись там, где игрок действительно нужен" in builder
    assert "Не придумывай событие специально ради конца хода." not in builder
    assert "Тихая сцена может оставаться тихой." not in builder
    assert "Не вставляй случайное событие" not in builder
    assert "небольшой бытовой выбор" not in builder
