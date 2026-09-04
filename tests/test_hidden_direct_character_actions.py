from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_direct_character_reads_are_not_exposed_in_openapi():
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    operation_ids = {
        operation.get("operationId")
        for path_item in spec.get("paths", {}).values()
        for operation in path_item.values()
        if isinstance(operation, dict)
    }
    assert "getCharacterBundle" not in operation_ids
    assert "getCharacterMemory" not in operation_ids
    assert "/sessions/{session_id}/characters/{character_id}" not in spec["paths"]
    assert "/sessions/{session_id}/characters/{character_id}/memory" not in spec["paths"]


def test_backend_character_endpoints_still_exist():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/sessions/{session_id}/characters/{character_id}", operation_id="getCharacterBundle")' in main
    assert '@app.get("/sessions/{session_id}/characters/{character_id}/memory", operation_id="getCharacterMemory")' in main


def test_full_character_cards_and_memory_stay_in_turn_context():
    context = (ROOT / "app" / "turn_context.py").read_text(encoding="utf-8")
    assert '"all_character_cards"' in context
    assert '"memory_full"' in context


def test_gpt_instructions_use_packet_as_character_source():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "all_character_cards[character_id]" in text
    assert "memory_full.characters[character_id]" in text
    assert "карточки и personal memory персонажей брать только из уже прочитанного packet" in text
    assert len(text) <= 8000
