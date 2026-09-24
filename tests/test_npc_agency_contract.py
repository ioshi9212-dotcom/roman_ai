from app.runtime_access import runtime_payload


def test_npc_agency_is_kept_in_simple_rules_without_separate_contract():
    docs = runtime_payload()["documents"]
    assert set(docs) == {"rules", "scene_builder"}
    rules = docs["rules"]
    builder = docs["scene_builder"]
    assert "NPC не ждут инициативы, разрешения, выбора или команды POV" in rules
    assert "не перекладывай на POV выбор, который принадлежит NPC" in rules
    assert "не ставь мир на паузу ради её хода" in rules
    assert "Не делай их автоматически удобными, правильными или терапевтичными" in rules
    assert "Значимая реакция POV" in rules
    assert "NPC действуют самостоятельно и как реальные люди" in builder
    assert "Персонажи действуют по своему характеру, целям и отношениям" in builder
    assert len(rules) < 5000
