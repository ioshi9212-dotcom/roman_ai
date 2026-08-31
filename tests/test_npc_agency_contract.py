from app.runtime_access import runtime_documents


def test_npc_agency_contract_rejects_boundary_optimization():
    docs = runtime_documents()
    assert "npc_agency_contract" in docs
    contract = docs["npc_agency_contract"]
    assert "не обязаны выбирать психологически правильное" in contract
    assert "NPC не ждёт разрешения POV" in contract
    assert "поцеловать первым без предварительного вопроса" in contract
    assert "он хотел взять её за руку, но не стал" in contract
    assert "Не тормози действие только ради" in contract
    assert "значимая реакция POV передаётся игроку" in contract
