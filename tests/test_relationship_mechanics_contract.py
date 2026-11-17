"""Regression tests for independent NPC feelings and causal relationship changes."""
import pytest
from app import relationship_file_runtime as rel
from app import relationship_policy_runtime as policy


CARDS = [
    {"character_id": "elena", "name": "Елена", "is_pov": True},
    {"character_id": "silas", "name": "Сайлас"},
    {"character_id": "liam", "name": "Лиам"},
]


def store(initial=None):
    return rel.build_initial_store(
        [{"character_id": "elena", "name": "Елена", "is_pov": True},
         {"character_id": "silas", "name": "Сайлас", "relationships": [
             {"target_character_id": "elena", "relationship_type": "знает полгода",
              "dimensions": initial or [{"label": "симпатия", "value": 50}]}]},
         CARDS[2]],
        {"pov": {"character_id": "elena"}}, "elena")


def update(state, rows, participants=("silas",)):
    return rel.apply_updates(state, rows, cards=CARDS, pov_id="elena",
                             turn_number=1, participant_ids=participants)


def test_one_sided_familiarity_preserves_existing_feelings():
    assert store()["npc_to_pov"]["silas"]["dimensions"]["симпатия"]["value"] == 50


def test_no_update_keeps_every_value():
    initial = store()
    assert update(initial, []) == initial


def test_multiple_npcs_can_change_in_same_turn():
    initial = store()
    after = update(initial, [
        {"character_id": "silas", "dimensions": [{"label": "симпатия", "delta": 2}]},
        {"character_id": "liam", "dimensions": [{"label": "симпатия", "value": 18}]},
    ], ("silas", "liam"))
    assert after["npc_to_pov"]["silas"]["dimensions"]["симпатия"]["value"] == 52
    assert after["npc_to_pov"]["liam"]["dimensions"]["симпатия"]["value"] == 18


def test_trust_can_disappear_and_distrust_emerge_same_turn():
    initial = store([{"label": "доверие", "value": 80}])
    after = update(initial, [{"character_id": "silas", "dimensions": [
        {"label": "доверие", "delta": -80},
        {"label": "недоверие", "value": 90},
    ]}])
    assert "доверие" not in after["npc_to_pov"]["silas"]["dimensions"]
    assert after["npc_to_pov"]["silas"]["dimensions"]["недоверие"]["value"] == 90


def test_trust_and_distrust_cannot_coexist():
    initial = store([{"label": "доверие", "value": 80}])
    with pytest.raises(ValueError, match="RELATIONSHIP_INCOMPATIBLE_DIMENSIONS"):
        update(initial, [{"character_id": "silas", "dimensions": [
            {"label": "недоверие", "value": 40}]}])


def test_love_and_hate_can_coexist():
    initial = store([{"label": "любовь", "value": 80}])
    after = update(initial, [{"character_id": "silas", "dimensions": [
        {"label": "ненависть", "value": 30}]}])
    assert set(after["npc_to_pov"]["silas"]["dimensions"]) == {"любовь", "ненависть"}


def test_critical_event_allows_full_scale_change():
    assert policy._update_scope({"change_scale": "ordinary"}) == ("ordinary", 3.0)
    assert policy._update_scope({"change_scale": "critical_event"}) == ("critical_event", 100.0)
