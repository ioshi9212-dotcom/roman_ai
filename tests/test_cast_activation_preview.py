"""Writer packet should surface missing recurring characters without forced cameos."""

from app.turn_pipeline import _cast_activation_view


def _state():
    return {
        "pov": {"character_id": "pov"},
        "current": {"location": "cafe", "present_characters": ["pov", "markus"]},
    }


def test_cast_activation_prefers_local_unseen_npc_without_marking_present():
    rows = [
        {"character_id": "pov", "is_pov": True, "present": True},
        {"character_id": "markus", "name": "Маркус", "present": True},
        {
            "character_id": "wife",
            "name": "Жена Маркуса",
            "importance": "support",
            "last_appearance_turn": 0,
            "last_contact_turn": 0,
            "npc_relation_refs": [
                {"direction": "outgoing", "other_character_id": "markus"},
            ],
        },
        {"character_id": "stranger", "name": "Неизвестный", "importance": "support"},
    ]
    view = _cast_activation_view(
        rows, _state(),
        {"location_id": "cafe", "linked_characters": ["wife"]},
        current_turn=3,
    )
    assert view["offscreen_registered_count"] == 2
    assert view["never_participated_count"] == 2
    assert view["candidates"][0]["character_id"] == "wife"
    assert "location_link" in view["candidates"][0]["causal_signals"]
    assert "connected_to_scene_participant" in view["candidates"][0]["causal_signals"]
    assert all(row["character_id"] not in {"pov", "markus"} for row in view["candidates"])
    assert "НЕ квота" in view["instruction"]


def test_cast_activation_rotates_equally_plausible_unseen_characters():
    rows = [
        {"character_id": "pov", "is_pov": True, "present": True},
        *[
            {"character_id": f"npc_{index}", "name": f"NPC {index}", "importance": "support"}
            for index in range(22)
        ],
    ]
    first = _cast_activation_view(rows, _state(), {}, current_turn=0)
    later = _cast_activation_view(rows, _state(), {}, current_turn=9)
    assert first["offscreen_registered_count"] == 22
    assert first["never_participated_count"] == 22
    assert len(first["candidates"]) == 8
    assert len(later["candidates"]) == 8
    assert {
        row["character_id"] for row in first["candidates"]
    } != {
        row["character_id"] for row in later["candidates"]
    }


def test_contacted_and_absent_characters_can_return_without_being_called():
    rows = [
        {"character_id": "pov", "is_pov": True, "present": True},
        {
            "character_id": "known",
            "name": "Знакомый",
            "last_contact_turn": 2,
            "active_intents": ["Возвращение по собственному делу"],
        },
        {
            "character_id": "remote",
            "name": "Удалённый собеседник",
            "remote": True,
        },
        {
            "character_id": "inactive",
            "name": "Недоступный",
            "status": "inactive",
        },
    ]
    view = _cast_activation_view(rows, _state(), {}, current_turn=4)
    assert view["offscreen_registered_count"] == 1
    assert view["never_participated_count"] == 0
    assert view["candidates"][0]["character_id"] == "known"
    assert "eligible_intent" in view["candidates"][0]["causal_signals"]
