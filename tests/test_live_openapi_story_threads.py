from app.main import app


def test_live_fastapi_openapi_matches_story_thread_contract():
    schema = app.openapi()
    assert schema["info"]["version"] == "1.11.1"
    schemas = schema["components"]["schemas"]
    assert "StoryThreadUpdate" in schemas
    assert "TurnExtracted" in schemas
    extracted = schemas["TurnExtracted"]
    assert "story_thread_updates" in extracted["required"]
    story_items = extracted["properties"]["story_thread_updates"]["items"]
    assert story_items["$ref"].endswith("/StoryThreadUpdate")
    assert "scene_progressed" in extracted["properties"]
