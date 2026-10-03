import inspect

from app import turn_pipeline as pipeline


def test_active_pipeline_has_no_turn_count_story_gate():
    assert not hasattr(pipeline, "WORLD_STAGNATION_LIMIT")
    assert not hasattr(pipeline, "_validate_world_movement")
    assert not hasattr(pipeline, "_world_movement_context")
    assert "_validate_world_movement" not in inspect.getsource(pipeline.commit_turn)


def test_active_prepare_context_has_no_hidden_world_movement_director_layer():
    source = inspect.getsource(pipeline._prepare_context)
    assert "world_movement_context" not in source
    assert "structural_movement_due" not in source


def test_working_contract_declares_no_hidden_semantic_scene_gate():
    source = inspect.getsource(pipeline._clean_director_layers)
    assert '"backend_semantic_scene_gates": False' in source
    assert "world_movement_when_due" not in source
