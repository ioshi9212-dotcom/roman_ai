from app.author_reaction_runtime import scene_reactions


CARDS = [
    {"character_id": "rinata_dale", "name": "Рината"},
    {"character_id": "silas_wayne", "name": "Сайлас"},
    {"character_id": "adrian_morwell", "name": "Эдриан"},
]


def test_verbatim_reactions_for_two_active_characters():
    raw = (
        "Реакция Ринаты на Сайласа: при взглядах и касаниях начинаются неясные вспышки.\n"
        "Реакция Сайласа на Ринату: при сближении он испытывает непонятную тоску.\n"
        "Реакция Эдриана на Ринату: иное авторское условие.\n"
    )
    entries = scene_reactions(
        {"source_intake": [{"block_id": "raw", "raw_text": raw}]},
        CARDS,
        ["rinata_dale", "silas_wayne"],
    )
    assert len(entries) == 2
    assert entries[0]["from_character_id"] == "rinata_dale"
    assert entries[0]["toward_character_id"] == "silas_wayne"
    assert entries[0]["verbatim_author_rule"] == raw.splitlines()[0]
    assert entries[1]["from_character_id"] == "silas_wayne"
    assert entries[1]["toward_character_id"] == "rinata_dale"
    assert entries[1]["verbatim_author_rule"] == raw.splitlines()[1]


def test_no_unrelated_reaction_in_current_scene():
    source = {
        "source_intake": [{
            "raw_text": "Реакция Ринаты на Сайласа: загадочные впечатления.",
        }]
    }
    assert scene_reactions(source, CARDS, ["rinata_dale", "adrian_morwell"]) == []
    assert scene_reactions(source, CARDS, ["rinata_dale"]) == []
    assert scene_reactions({}, CARDS, ["rinata_dale", "silas_wayne"]) == []


def test_ambiguous_names_and_duplicate_raw_not_misassigned():
    cards = CARDS + [{"character_id": "rinata_other", "name": "Ринателла"}]
    raw = "Реакция Ринаты на Сайласа: неясная память."
    # No guessing between two characters whose long-form first names share a stem.
    assert scene_reactions(
        {"source_intake": [{"raw_text": raw}, {"raw_text": raw}]},
        cards,
        ["rinata_dale", "silas_wayne"],
    ) == []
