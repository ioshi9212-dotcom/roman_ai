"""Native turn context avoids legacy copies; relationship values can progress subtly."""
from app import relationship_file_runtime, transport_scope_runtime
from test_generator_state_documents import create, prepare, story


def test_canonical_v5_turn_skips_legacy_full_payload_scrubber(story, monkeypatch):
    sid, _ = create(story)

    def forbidden_legacy_scrub(*args, **kwargs):
        raise AssertionError("V5 context must bypass the legacy full-payload pass")

    monkeypatch.setattr(
        transport_scope_runtime, "_strip_legacy_full_payloads",
        forbidden_legacy_scrub,
    )
    _, packet = prepare(sid)
    assert packet["scene_builder"] and packet["runtime_rules"]
    assert set(packet["character_memory"]) == {"pov", "ren"}
    assert packet["chronology"]["дни"] == {}
    assert "character_cards" in packet
    assert "source_full" not in packet


def test_small_signed_relationship_deltas_remain_fractional_after_normalization():
    cards = [
        {"character_id": "pov", "name": "Рината", "is_pov": True},
        {"character_id": "ren", "name": "Рен"},
    ]
    base = relationship_file_runtime.normalize_store({
        "pov_character_id": "pov",
        "npc_to_pov": {
            "ren": {"dimensions": {"доверие": {"value": 12}}}
        },
    })
    after = relationship_file_runtime.apply_updates(
        base,
        [{"character_id": "ren", "dimensions": [
            {"label": "доверие", "delta": 0.25},
        ]}],
        cards=cards, pov_id="pov", turn_number=1, participant_ids=["ren"],
    )
    assert after["npc_to_pov"]["ren"]["dimensions"]["доверие"]["value"] == 12.25
    after = relationship_file_runtime.apply_updates(
        relationship_file_runtime.normalize_store(after),
        [{"character_id": "ren", "dimensions": [
            {"label": "доверие", "delta": -0.5},
        ]}],
        cards=cards, pov_id="pov", turn_number=2, participant_ids=["ren"],
    )
    assert after["npc_to_pov"]["ren"]["dimensions"]["доверие"]["value"] == 11.75
    unchanged = relationship_file_runtime.apply_updates(
        after, [], cards=cards, pov_id="pov", turn_number=3,
        participant_ids=["ren"],
    )
    assert unchanged == after
