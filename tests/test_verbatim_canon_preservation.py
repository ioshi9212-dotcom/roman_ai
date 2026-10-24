"""Never replace the author's language with a normalized writer-friendly version."""
from app.profile_templates import normalize_character_profile, normalize_novel_profile


def test_novel_profile_keeps_explicit_author_language_and_exact_whitespace():
    original = "  Он, блять, не добрый!\nНе делай его мягче.  "
    novel = normalize_novel_profile({"story_rules": original, "premise": original})
    assert novel["story_rules"] == original
    assert novel["premise"] == original


def test_verbatim_legacy_phrase_is_preserved_in_stored_source():
    # Retired behavior can be excluded from a writer packet, but the user data
    # must never be quietly modified when it is first recorded.
    original = "Если игрок не дал реплику, не придумывай её. Он грубый, блять."
    novel = normalize_novel_profile({"story_rules": original})
    assert novel["story_rules"] == original


def test_character_characteristics_keep_harsh_source_unchanged():
    original = "Скотина, хамит даже близким.  Никогда просто так не извинится."
    card = normalize_character_profile({
        "character_id": "silas",
        "name": "Сайлас",
        "character": original,
        "speech": "Матерится, когда бесится.\nГоворит грубо!",
    })
    assert card["character"] == original
    assert card["speech"] == "Матерится, когда бесится.\nГоворит грубо!"
