from app.runtime_access import runtime_payload


def test_npc_agency_is_kept_in_simple_rules_without_separate_contract():
    docs = runtime_payload()["documents"]
    assert set(docs) == {"rules", "scene_builder"}
    rules = docs["rules"]
    builder = docs["scene_builder"]
    assert "NPC действуют сами" in rules
    assert "не подменяй это психологически" in rules
    assert "Они могут ошибаться, врать, давить" in rules
    assert "NPC действуют сами по характеру" in builder
    assert "не обязаны быть удобными" in builder
