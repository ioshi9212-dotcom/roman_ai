import hashlib
from pathlib import Path

from app.runtime_access import author_runtime_sources, runtime_documents


ROOT = Path(__file__).resolve().parents[1]


AUTHOR_BLOB_SHA = {
    "rules.md": "6d1c2ebd917df8df46b07d2ec51c5d1312031203",
    "scene_builder.md": "6f554b685442b1ebc7fdab75fb007bf911a55c18",
}


def _git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    payload = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    return hashlib.sha1(payload).hexdigest()


def test_author_runtime_sources_are_byte_locked():
    for name, expected in AUTHOR_BLOB_SHA.items():
        assert _git_blob_sha(ROOT / "runtime" / name) == expected


def test_active_runtime_is_exactly_the_two_author_documents():
    author = author_runtime_sources()
    active = runtime_documents()

    assert set(active) == {"rules", "scene_builder"}
    assert active == author
    assert active["rules"] == (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    assert active["scene_builder"] == (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
