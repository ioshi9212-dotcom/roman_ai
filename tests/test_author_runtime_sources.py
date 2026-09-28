import hashlib
from pathlib import Path

from app.runtime_access import author_runtime_sources, runtime_documents


ROOT = Path(__file__).resolve().parents[1]


AUTHOR_BLOB_SHA = {
    "rules.md": "6f229fd34d5c954551a82c608041c11b2abc8bde",
    "scene_builder.md": "758d20ecea4f66b31a504f486cb9efde7d9aded7",
}


def _git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    payload = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    return hashlib.sha1(payload).hexdigest()


def test_author_runtime_sources_are_byte_locked():
    for name, expected in AUTHOR_BLOB_SHA.items():
        assert _git_blob_sha(ROOT / "runtime" / name) == expected


def test_active_runtime_contract_is_separate_from_author_rules():
    author = author_runtime_sources()
    active = runtime_documents()

    assert author["rules"] == (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    assert author["scene_builder"] == (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert active["rules"] == (ROOT / "runtime" / "runtime_contract.md").read_text(encoding="utf-8")
    assert active["scene_builder"] == author["scene_builder"]
    assert active["rules"] != author["rules"]
