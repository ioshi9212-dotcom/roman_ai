from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_custom_gpt_uses_scoped_packet_and_chunked_dormant_dossier_reads():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "scene-scoped packet" in text
    assert "prepareCharacterBundleRead" in text
    assert "getCharacterBundleChunk" in text
    assert "Не использовать direct `getCharacterBundle`/`getCharacterMemory`" in text
    assert "getTurnPacketChunkBatch" not in text
    assert len(text) <= 8000
