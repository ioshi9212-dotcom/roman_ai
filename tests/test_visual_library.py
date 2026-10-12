import io
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import storage, visual_library
from app.main import app


def _image(color="red", fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (8, 9), color).save(out, format=fmt)
    return out.getvalue()


def _novel():
    return {
        "novel_id": "avern-test",
        "title": "Test Avern",
        "version": 5,
        "novel": {"pov_character": "elen"},
        "characters": [{"character_id": "elen", "name": "Elen", "is_pov": True},
                       {"character_id": "morita", "name": "Morita"}],
        "locations": [{"location_id": "eastern_base", "name": "Eastern Base"}],
        "lore": {},
        "starting_state": {"pov": {"character_id": "elen"},
                           "current": {"location": "Eastern Base",
                                       "present_characters": ["elen"]}},
    }


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setenv("VISUAL_LIBRARY_API_KEY", "test-only-library-key")
    storage.ensure_dirs()
    storage.save_novel(_novel())
    return tmp_path


def test_multi_references_are_originals_and_independent_of_sessions(state):
    sid = storage.create_session(_novel())["session_id"]
    original = _image()
    face = visual_library.save_reference(
        "avern-test", original, subject_type="character",
        subject_id="elen", category="face", original_name="face.png",
    )
    outfit = visual_library.save_reference(
        "avern-test", _image("blue"), subject_type="character",
        subject_id="elen", category="outfit",
    )
    location = visual_library.save_reference(
        "avern-test", _image("green"), subject_type="location",
        subject_id="eastern_base", category="environment",
    )
    assert len(visual_library.list_references("avern-test")) == 3
    assert len(visual_library.list_references("avern-test", subject_id="elen")) == 2
    image_file, mime = visual_library.get_reference_asset("avern-test", face["reference_id"])
    assert image_file.read_bytes() == original
    assert mime == "image/png"
    assert image_file.is_relative_to(state / "visual_library")
    assert not image_file.is_relative_to(state / "sessions")
    assert (state / "sessions" / sid / "source.json").exists()
    assert location["subject_id"] == "eastern_base"
    assert outfit["category"] == "outfit"


def test_reference_replace_preserves_identity_and_delete(state):
    original = visual_library.save_reference(
        "avern-test", _image("red"), subject_type="character", subject_id="morita",
    )
    previous_path, _ = visual_library.get_reference_asset("avern-test", original["reference_id"])
    replacement = visual_library.save_reference(
        "avern-test", _image("blue", "JPEG"), subject_type="character",
        subject_id="morita", category="full_body", reference_id=original["reference_id"],
    )
    current_path, mime = visual_library.get_reference_asset("avern-test", original["reference_id"])
    assert replacement["reference_id"] == original["reference_id"]
    assert replacement["created_at"] == original["created_at"]
    assert current_path != previous_path
    assert not previous_path.exists()
    assert mime == "image/jpeg"
    assert visual_library.delete_reference("avern-test", original["reference_id"])["deleted"]
    assert not current_path.exists()
    assert visual_library.list_references("avern-test") == []


@pytest.mark.parametrize("subject_type,subject_id", [
    ("character", "missing"),
    ("location", "not_a_place"),
    ("invalid", "elen"),
    ("character", "../escape"),
])
def test_unknown_or_invalid_subject_rejected(state, subject_type, subject_id):
    with pytest.raises(visual_library.VisualLibraryError):
        visual_library.save_reference(
            "avern-test", _image(), subject_type=subject_type, subject_id=subject_id,
        )


def test_bad_image_and_traversal_rejected(state):
    with pytest.raises(visual_library.VisualLibraryError):
        visual_library.save_reference(
            "avern-test", b"not-an-image", subject_type="character", subject_id="elen",
        )
    with pytest.raises(visual_library.VisualLibraryError):
        visual_library.save_reference(
            "../secrets", _image(), subject_type="novel", subject_id="",
        )
    assert not (state / "visual_library" / "avern-test").exists()


def test_unpublished_session_only_novel_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    storage.ensure_dirs()
    storage.create_session(_novel())
    saved = visual_library.save_reference(
        "avern-test", _image(), subject_type="character", subject_id="elen",
    )
    assert saved["novel_id"] == "avern-test"
    assert not (storage.LIBRARY_DIR / "avern-test.json").exists()


def test_authenticated_upload_gallery_replace_and_delete(state):
    client = TestClient(app)
    url = "/visual-library/avern-test/references"
    data = {"subject_type": "character", "subject_id": "elen", "category": "face"}
    img = {"file": ("elen.png", _image(), "image/png")}
    assert client.post(url, data=data, files=img).status_code == 401
    key = {"X-Visual-Library-Key": "test-only-library-key"}
    response = client.post(url, data=data, files=img, headers=key)
    assert response.status_code == 201, response.text
    item = response.json()
    image_url = f"{url}/{item['reference_id']}/image"
    assert client.get(image_url).status_code == 401
    assert client.get(image_url, headers=key).content == _image()
    assert client.get(url, headers=key).json()["references"][0]["reference_id"] == item["reference_id"]
    updated = client.put(
        f"{url}/{item['reference_id']}",
        data=data, files={"file": ("new.png", _image("blue"), "image/png")},
        headers=key,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["reference_id"] == item["reference_id"]
    assert client.delete(f"{url}/{item['reference_id']}", headers=key).status_code == 200
    assert client.get(url, headers=key).json()["references"] == []


def test_auth_requires_configured_key(state, monkeypatch):
    monkeypatch.delenv("VISUAL_LIBRARY_API_KEY")
    assert TestClient(app).get("/visual-library/avern-test/references").status_code == 503
