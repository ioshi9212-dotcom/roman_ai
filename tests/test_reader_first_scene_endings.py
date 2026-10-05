from app.runtime_access import runtime_documents


def test_scene_builder_is_reader_first_and_skips_mundane_choice_gates():
    builder = runtime_documents()["scene_builder"]
    assert "Игрок в первую очередь читатель" in builder
    assert "не на ближайшем возможном выборе" in builder
    assert "Бытовой микровыбор сам по себе не является причиной остановки" in builder
    assert "Если естественного крючка ещё нет, продолжай сцену" in builder
    assert "не выполняет обслуживание сцены" in builder
