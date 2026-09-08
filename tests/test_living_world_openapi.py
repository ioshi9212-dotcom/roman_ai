from pathlib import Path

import yaml

from app.main import app


ROOT = Path(__file__).resolve().parents[1]


def test_static_actions_schema_exposes_foundation_and_relationship_opinion_updates():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    draft_enum = schema["components"]["schemas"]["NovelDraftSection"]["properties"]["section_name"]["enum"]
    assert "foundation" in draft_enum
    relation = schema["components"]["schemas"]["RelationshipUpdate"]
    assert relation["required"] == ["character_id"]
    assert "opinion" in relation["properties"]
    assert "beliefs_about_target" in relation["properties"]
    assert "dimensions" in relation["properties"]


def test_live_fastapi_schema_allows_relationship_opinion_without_dimensions():
    schema = app.openapi()
    relation = schema["components"]["schemas"]["RelationshipUpdate"]
    assert relation["required"] == ["character_id"]
    assert "opinion" in relation["properties"]
    assert "current_dynamic" in relation["properties"]
    assert "beliefs_about_target" in relation["properties"]
    story = schema["components"]["schemas"]["StoryThreadUpdate"]
    assert "pillar_ids" in story["properties"]
