from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_custom_gpt_uses_bounded_packet_and_chunked_dormant_dossier_reads():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "writer-first" in text
    assert "prepareCharacterBundleRead" in text
    assert "getCharacterBundleChunk" in text
    assert "Direct `getCharacterBundle`/`getCharacterMemory` не использовать" in text
    assert "getTurnPacketChunkBatch" not in text
    assert len(text) <= 8000
