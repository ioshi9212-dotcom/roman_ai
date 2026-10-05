from app.runtime_access import runtime_documents


def test_npc_and_world_agency_do_not_depend_on_pov_turn():
    builder = runtime_documents()["scene_builder"]
    assert "POV не центр мира." in builder
    assert "NPC сами принимают свои решения и действуют по своим целям." in builder
    assert "Не передавай игроку решение, которое принадлежит NPC" in builder
    assert "не ставь мир на паузу ради хода POV." in builder
    assert "NPC не обязан ждать инициативы, разрешения, выбора или команды POV." in builder
