from app.models import TurnExtracted


def _base_payload(**extra):
    payload = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
        "story_thread_updates": [],
    }
    payload.update(extra)
    return payload


def test_nullable_optional_commit_arrays_are_normalized_to_empty_lists():
    model = TurnExtracted.model_validate(
        _base_payload(
            character_upserts=None,
            presence_updates=None,
            relationship_updates=None,
        )
    )

    dumped = model.model_dump()
    assert dumped["character_upserts"] == []
    assert dumped["presence_updates"] == []
    assert dumped["relationship_updates"] == []


def test_omitted_optional_commit_arrays_are_also_empty_lists():
    model = TurnExtracted.model_validate(_base_payload())

    dumped = model.model_dump()
    assert dumped["character_upserts"] == []
    assert dumped["presence_updates"] == []
    assert dumped["relationship_updates"] == []
