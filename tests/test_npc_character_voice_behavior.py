"""NPC speech stays individual; relationship metrics drive behavior before footer."""
import json

from app import session_runtime, storage


def test_scene_builder_prioritizes_character_voice_and_relationship_behavior():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    skin = (root / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    rules = (root / "runtime" / "rules.md").read_text(encoding="utf-8")
    assert "ГОЛОС NPC — СТРОГО ИНДИВИДУАЛЬНЫЙ" in skin
    assert "ОТНОШЕНИЕ NPC К POV — ПРИЧИНА ЕГО ПОВЕДЕНИЯ" in skin
    assert "Любовь НЕ гарантирует уступчивость" in skin
    assert "Не изменяй числа и не придумывай новое чувство ради выбранной реплики" in skin
    assert "ПОВЕДЕНИЕ ПО АКТУАЛЬНОЙ СВЯЗИ" in rules
    assert "ГОЛОС ПО КАРТОЧКЕ" in rules


def test_live_turn_packet_references_real_npc_relationships_and_full_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    storage.ensure_dirs()
    novel = {
        "novel_id": "npc-voice-director-contract",
        "title": "Характеры",
        "version": 5,
        "novel": {"pov_character": "emily"},
        "characters": [
            {"character_id": "emily", "name": "Эмили", "is_pov": True},
            {
                "character_id": "ren", "name": "Рен",
                "character": "Немногословен, подозрителен, не выдаёт тайны от нажима.",
                "relationships": [{
                    "target_character_id": "emily",
                    "relationship_type": "испытывает симпатию, но скрывает опасную тайну",
                    "dimensions": [
                        {"label": "симпатия", "value": 58},
                        {"label": "доверие", "value": 20},
                    ],
                }],
            },
        ],
        "starting_state": {
            "pov": {"character_id": "emily"},
            "current": {
                "date": "01.11.2026", "time": "19:00", "location": "бар",
                "present_characters": ["emily", "ren"],
            },
        },
    }
    sid = storage.create_session(novel)["session_id"]
    session_runtime.prepare_turn_packet(sid, "Эмили просит Рена рассказать тайну.")
    packet = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
    context = json.loads("".join(packet["chunks"]))
    rule = context["npc_voice_relationship_contract"]
    assert rule["mandatory"] is True
    assert rule["scope"] == "director_only"
    assert rule["character_cards_path"] == "character_cards"
    assert rule["relationship_values_path"] == "relationships.npc_to_pov"
    assert rule["scene_npc_ids"] == ["ren"]
    assert "Любовь не гарантирует" in rule["rule"]
    assert "repairs" not in rule
    npc_card = next(row for row in context["character_cards"] if row["character_id"] == "ren")
    assert "Немногословен" in npc_card["character"]
    saved = context["relationships"]["npc_to_pov"]["ren"]["dimensions"]
    assert saved["симпатия"]["value"] == 58
    assert saved["доверие"]["value"] == 20
