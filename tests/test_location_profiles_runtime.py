import json
import tempfile
from pathlib import Path

import pytest

from app import location_runtime, session_runtime, simple_setup_runtime, stability_runtime, storage, turn_rollback


def _setup(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel(*, location="Дом Сайласа", location_id=None, zone="Кухня"):
    current = {
        "date": "02.10.2026",
        "time": "17:00",
        "location": location,
        "zone": zone,
        "present_characters": ["pov"],
    }
    if location_id is not None:
        current["location_id"] = location_id
    return {
        "novel_id": "location-profiles",
        "title": "Location Profiles",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {
                "character_id": "rayna",
                "name": "Райна",
                "surname": "Вальтор",
                "work": "HOUSEKEEPER_CARD_ONLY_MARKER",
                "appearance": "HOUSEKEEPER_FULL_CARD_MARKER",
                "story_function": "связана с домом Сайласа",
            },
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "lore": {},
        "locations": [
            {
                "location_id": "silas_house",
                "name": "Дом Сайласа",
                "aliases": ["дом Сайласа"],
                "type": "частный дом",
                "floor": "два этажа",
                "layout": "Кухня и кабинет находятся на первом этаже.",
                "zones": [
                    {"zone_id": "kitchen", "name": "Кухня", "summary": "на первом этаже"},
                    {"zone_id": "study", "name": "Кабинет", "summary": "отдельная рабочая комната"},
                ],
                "linked_characters": [
                    {"character_id": "rayna", "relation": "семья обслуживает дом"}
                ],
                "fixed_features": ["HOUSE_FIXED_MARKER"],
            },
            {
                "location_id": "adrian_school",
                "name": "Школа Эдриана",
                "hours": "09:00–21:00",
                "fixed_features": ["SCHOOL_ONLY_MARKER"],
                "linked_characters": [{"character_id": "adrian", "relation": "работает здесь"}],
            },
        ],
        "canon_notes": [
            {
                "note_id": "household",
                "text": "HOUSE_NOTE_MARKER",
                "subjects": ["silas_house"],
            },
            {
                "note_id": "school",
                "text": "SCHOOL_NOTE_MARKER",
                "subjects": ["adrian_school"],
            },
            {
                "note_id": "unscoped",
                "text": "UNSCOPED_NOTE_MARKER",
                "subjects": [],
            },
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": current,
        },
    }


def _read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    raw = "".join(parts)
    return json.loads(raw), raw


def test_turn_packet_loads_only_the_location_where_pov_is_physically_present():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]

        context, raw = _read_packet(sid, "Я потом заеду в школу Эдриана.")

        location = context["location_context"]
        assert location["physical_presence_only"] is True
        assert location["location_id"] == "silas_house"
        assert "HOUSE_FIXED_MARKER" in location["profile"]
        assert location["current_zone"]["zone_id"] == "kitchen"
        assert location["linked_characters"] == [
            {"character_id": "rayna", "relation": "семья обслуживает дом"}
        ]
        notes = context["canon_notes_context"]["notes"]
        assert [row["text"] for row in notes] == ["HOUSE_NOTE_MARKER"]

        # Mentioning another saved place is not enough to pull its profile or note.
        assert "SCHOOL_ONLY_MARKER" not in raw
        assert "SCHOOL_NOTE_MARKER" not in raw
        assert "UNSCOPED_NOTE_MARKER" not in raw

        # The cast preview can carry work; linking a person still does not load their full card.
        assert "HOUSEKEEPER_FULL_CARD_MARKER" not in raw
        row = next(row for row in context["cast_registry"]["characters"] if row["character_id"] == "rayna")
        assert row["work"] == "HOUSEKEEPER_CARD_ONLY_MARKER"
        assert "rayna" not in {card["character_id"] for card in context["character_cards"]}
        assert "rayna" not in context.get("character_profiles", {})

        # Full persistent location collections must not leak through generic source transport.
        assert "locations" not in context.get("source_extra", {})
        assert "canon_notes" not in context.get("source_extra", {})


def test_setup_source_evidence_is_not_duplicated_in_every_turn_packet():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        evidence = "RAW_SOURCE_EVIDENCE_MARKER_" + "E" * 65000
        novel["foundation"] = {
            "facts": [{
                "fact_id": "rinata_trait",
                "text": "Рината любит свой сад и ухаживает за растениями.",
                "story_use": "reference",
                "stored_in": ["characters[pov].character"],
                "source_unit_ids": ["intake:u1"],
                "source_evidence": [{
                    "source_unit_id": "intake:u1",
                    "block_id": "intake",
                    "stage": "setup",
                    "text": evidence,
                }],
            }],
            "hooks": [{"hook_id": "garden_visit", "summary": "Посещение сада"}],
        }
        novel["custom_world_detail"] = {"text": "UNIQUE_CANON_MARKER"}
        sid = storage.create_session(novel)["session_id"]
        context, raw = _read_packet(sid, "(посмотреть в окно)")

        foundation = context["source_extra"]["foundation"]
        fact = foundation["facts"][0]
        assert fact["text"] == novel["foundation"]["facts"][0]["text"]
        assert "source_evidence" not in fact
        assert "source_unit_ids" not in fact
        assert foundation["hooks"] == novel["foundation"]["hooks"]
        assert context["source_extra"]["custom_world_detail"] == novel["custom_world_detail"]
        assert "RAW_SOURCE_EVIDENCE_MARKER_" not in raw

        # No destructive compaction: source evidence remains fully retrievable
        # from the persisted session source.
        persisted = storage._read_json(storage.SESSIONS_DIR / sid / "source.json", {})
        assert persisted["foundation"]["facts"][0]["source_evidence"][0]["text"] == evidence


def test_unknown_or_one_off_location_does_not_receive_a_saved_location_profile():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(
            _novel(location="Случайное кафе", location_id="silas_house", zone="зал")
        )["session_id"]

        context, raw = _read_packet(sid, "(выпить кофе)")

        assert "location_context" not in context
        assert "HOUSE_FIXED_MARKER" not in raw
        assert "SCHOOL_ONLY_MARKER" not in raw


def test_location_sync_replaces_or_clears_stale_canonical_ids_from_visible_physical_place():
    source = _novel()

    stale = {
        "current": {
            "location": "Случайное кафе",
            "location_id": "silas_house",
            "zone_id": "study",
            "zone": "зал",
        }
    }
    synced = location_runtime.sync_current_location(source, stale)
    assert "location_id" not in synced["current"]
    assert "zone_id" not in synced["current"]

    known = {
        "current": {
            "location": "Школа Эдриана",
            "zone": "главный зал",
        }
    }
    synced = location_runtime.sync_current_location(source, known)
    assert synced["current"]["location_id"] == "adrian_school"


def test_parent_location_id_can_resolve_a_known_child_zone_without_creating_a_new_place():
    source = _novel()
    state = {
        "current": {
            "location": "Кабинет",
            "location_id": "silas_house",
            "zone": "Кабинет",
            "present_characters": ["pov"],
        }
    }

    context = location_runtime.build_location_context(
        source,
        state,
        scene_character_ids=["pov"],
    )

    assert context is not None
    assert context["location_id"] == "silas_house"
    assert context["current_zone"]["zone_id"] == "study"
    assert context["name"] == "Дом Сайласа"


def test_v5_setup_canonicalizes_location_links_to_existing_characters():
    template, coverage = simple_setup_runtime._validate_simple_content({
        "novel_id": "location_setup",
        "title": "Location Setup",
        "version": 5,
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рина", "is_pov": True},
            {"character_id": "rayna", "name": "Райна", "surname": "Вальтор"},
        ],
        "lore": {},
        "locations": [{
            "location_id": "silas_house",
            "name": "Дом Сайласа",
            "linked_characters": [{"character_id": "Райна Вальтор", "relation": "обслуживает дом"}],
            "zones": ["Кухня", "Кабинет"],
        }],
        "canon_notes": [{
            "note_id": "household",
            "text": "Семья Вальтор связана с домом.",
            "subjects": ["silas_house", "rayna"],
        }],
    })

    assert coverage["ok"] is True
    house = template["locations"][0]
    assert house["linked_characters"] == [
        {"character_id": "rayna", "relation": "обслуживает дом"}
    ]
    assert {row["name"] for row in house["zones"]} == {"Кухня", "Кабинет"}
    assert template["canon_notes"][0]["subjects"] == ["silas_house", "rayna"]


def test_v5_setup_rejects_location_link_to_unknown_character():
    with pytest.raises(ValueError, match="DRAFT_LOCATION_CHARACTER_UNKNOWN"):
        simple_setup_runtime._validate_simple_content({
            "novel_id": "bad_location_link",
            "title": "Bad Location Link",
            "version": 5,
            "novel": {"pov_character": "rina"},
            "characters": [{"character_id": "rina", "name": "Рина", "is_pov": True}],
            "lore": {},
            "locations": [{
                "location_id": "house",
                "name": "Дом",
                "linked_characters": [{"character_id": "invented_worker", "relation": "работает здесь"}],
            }],
        })


def test_combined_parent_and_zone_header_keeps_the_same_profile_and_resolves_child_zone():
    source = _novel()
    state = {
        "current": {
            "location": "Дом Сайласа, кухня",
            "location_id": "silas_house",
            "present_characters": ["pov"],
        }
    }

    synced = location_runtime.sync_current_location(source, state)
    context = location_runtime.build_location_context(
        source,
        synced,
        scene_character_ids=["pov"],
    )

    assert synced["current"]["location_id"] == "silas_house"
    assert synced["current"]["zone_id"] == "kitchen"
    assert context is not None
    assert context["current_zone"]["zone_id"] == "kitchen"


def test_combined_parent_and_zone_resolves_without_preexisting_location_id():
    source = _novel()
    state = {
        "current": {
            "location": "Дом Сайласа, кухня",
            "present_characters": ["pov"],
        }
    }

    synced = location_runtime.sync_current_location(source, state)
    context = location_runtime.build_location_context(
        source,
        synced,
        scene_character_ids=["pov"],
    )

    assert synced["current"]["location_id"] == "silas_house"
    assert synced["current"]["zone_id"] == "kitchen"
    assert context is not None
    assert context["current_zone"]["zone_id"] == "kitchen"


def test_location_change_clears_zone_inherited_by_deep_merge():
    source = _novel()
    before = {
        "current": {
            "location": "Дом Сайласа",
            "location_id": "silas_house",
            "zone": "Кухня",
            "zone_id": "kitchen",
        }
    }
    after = {
        "current": {
            "location": "Случайное кафе",
            "location_id": "silas_house",
            "zone": "Кухня",
            "zone_id": "kitchen",
        }
    }

    synced = location_runtime.sync_current_location(source, after, previous_state=before)

    assert "location_id" not in synced["current"]
    assert "zone_id" not in synced["current"]
    assert "zone" not in synced["current"]


def test_canon_notes_load_without_location_profile_when_subject_is_in_scene_or_global():
    source = _novel(location="Случайное кафе", zone="зал")
    source["canon_notes"].extend([
        {"note_id": "pov-note", "text": "POV_NOTE_MARKER", "subjects": ["pov"]},
        {"note_id": "global-note", "text": "GLOBAL_NOTE_MARKER", "subjects": ["global"]},
    ])
    state = source["starting_state"]

    assert location_runtime.build_location_context(
        source,
        state,
        scene_character_ids=["pov"],
    ) is None

    notes = location_runtime.build_canon_notes_context(
        source,
        state,
        scene_character_ids=["pov"],
    )
    assert notes is not None
    texts = [row["text"] for row in notes["notes"]]
    assert "POV_NOTE_MARKER" in texts
    assert "GLOBAL_NOTE_MARKER" in texts
    assert "HOUSE_NOTE_MARKER" not in texts


def test_scene_header_parser_does_not_store_weather_inside_location():
    current = stability_runtime._scene_header_current(
        "🎭 Test · осень\n"
        "🕒 День 1 · пятница, 02.10.2026, 18:20 · 📍 Дом Сайласа, кухня 🌦️ Погода: дождь\n"
        "⚙️ Сцена: разговор"
    )

    assert current["location"] == "Дом Сайласа, кухня"


def test_v5_setup_rejects_ambiguous_named_location_character_link():
    with pytest.raises(ValueError, match="DRAFT_LOCATION_CHARACTER_AMBIGUOUS"):
        simple_setup_runtime._validate_simple_content({
            "novel_id": "ambiguous_location_link",
            "title": "Ambiguous",
            "version": 5,
            "novel": {"pov_character": "rina"},
            "characters": [
                {"character_id": "rina", "name": "Рина", "is_pov": True},
                {"character_id": "alex_one", "name": "Алекс"},
                {"character_id": "alex_two", "name": "Алекс"},
            ],
            "lore": {},
            "locations": [{
                "location_id": "school",
                "name": "Школа",
                "linked_characters": [{"character_id": "Алекс", "relation": "работает здесь"}],
            }],
        })


def test_rollback_replay_recomputes_location_ids_and_clears_old_zone_on_move():
    source = _novel()
    cards, state, memory, chronology = turn_rollback._initial_replay_state(source)
    assert state["current"]["location_id"] == "silas_house"
    assert state["current"]["zone_id"] == "kitchen"

    turn = {
        "turn_number": 1,
        "scene_output": (
            "🎭 Test · осень\n"
            "🕒 День 1 · пятница, 02.10.2026, 18:30 · 📍 Случайное кафе 🌦️ Погода: дождь\n"
            "⚙️ Сцена: кофе"
        ),
        "extracted": {"state_patch": {}},
    }

    cards, state, memory, chronology = turn_rollback._apply_saved_turn(
        source,
        cards,
        state,
        memory,
        chronology,
        [],
        turn,
    )

    assert state["current"]["location"] == "Случайное кафе"
    assert "location_id" not in state["current"]
    assert "zone_id" not in state["current"]
    assert "zone" not in state["current"]


def test_ambiguous_room_name_cannot_keep_inherited_previous_location_id():
    source = _novel()
    source["locations"].append({
        "location_id": "adrian_apartment",
        "name": "Квартира Эдриана",
        "zones": [{"zone_id": "kitchen", "name": "Кухня"}],
    })
    before = {
        "current": {
            "location": "Дом Сайласа",
            "location_id": "silas_house",
            "zone": "Кабинет",
            "zone_id": "study",
        }
    }
    # Deep merge left the previous parent id behind while the visible pointer
    # was changed to a generic room name that exists in more than one place.
    after = {
        "current": {
            "location": "Кухня",
            "location_id": "silas_house",
            "zone": "Кабинет",
            "zone_id": "study",
        }
    }

    synced = location_runtime.sync_current_location(source, after, previous_state=before)

    assert "location_id" not in synced["current"]
    assert "zone_id" not in synced["current"]
    assert "zone" not in synced["current"]


def test_location_id_change_clears_zone_inherited_under_same_generic_location_name():
    source = _novel()
    source["locations"].append({
        "location_id": "adrian_apartment",
        "name": "Квартира Эдриана",
        "aliases": ["дом"],
        "zones": [{"zone_id": "bedroom", "name": "Спальня"}],
    })
    before = {
        "current": {
            "location": "дом",
            "location_id": "silas_house",
            "zone": "Кухня",
            "zone_id": "kitchen",
        }
    }
    after = {
        "current": {
            "location": "дом",
            "location_id": "adrian_apartment",
            "zone": "Кухня",
            "zone_id": "kitchen",
        }
    }

    synced = location_runtime.sync_current_location(source, after, previous_state=before)

    assert synced["current"]["location_id"] == "adrian_apartment"
    assert "zone_id" not in synced["current"]
    assert "zone" not in synced["current"]


def test_room_name_substring_does_not_attach_an_unrelated_one_off_place_to_saved_profile():
    source = _novel()
    state = {
        "current": {
            "location": "Кабинет врача",
            "present_characters": ["pov"],
        }
    }

    synced = location_runtime.sync_current_location(source, state)
    context = location_runtime.build_location_context(
        source,
        synced,
        scene_character_ids=["pov"],
    )

    assert "location_id" not in synced["current"]
    assert context is None


def test_stale_parent_id_cannot_use_room_substring_as_proof_of_same_place():
    source = _novel()
    state = {
        "current": {
            "location": "Кабинет врача",
            "location_id": "silas_house",
            "present_characters": ["pov"],
        }
    }

    synced = location_runtime.sync_current_location(source, state)
    context = location_runtime.build_location_context(
        source,
        synced,
        scene_character_ids=["pov"],
    )

    assert "location_id" not in synced["current"]
    assert context is None


def test_generic_raw_zone_name_does_not_pull_canon_note_from_another_saved_place():
    source = _novel(location="Случайное кафе", zone="Кухня")
    source["canon_notes"].append({
        "note_id": "house-kitchen-only",
        "text": "HOUSE_KITCHEN_ONLY_MARKER",
        "subjects": ["silas_house.kitchen"],
    })
    state = source["starting_state"]

    notes = location_runtime.build_canon_notes_context(
        source,
        state,
        scene_character_ids=["pov"],
    )

    assert notes is None or "HOUSE_KITCHEN_ONLY_MARKER" not in [
        row["text"] for row in notes["notes"]
    ]


def test_zone_subject_loads_when_that_zone_is_resolved_inside_the_saved_location():
    source = _novel()
    source["canon_notes"].append({
        "note_id": "house-kitchen",
        "text": "HOUSE_KITCHEN_MARKER",
        "subjects": ["silas_house.kitchen"],
    })
    state = source["starting_state"]

    notes = location_runtime.build_canon_notes_context(
        source,
        state,
        scene_character_ids=["pov"],
    )

    assert notes is not None
    assert "HOUSE_KITCHEN_MARKER" in [row["text"] for row in notes["notes"]]


def test_short_location_alias_does_not_match_inside_unrelated_word():
    source = _novel()
    source["locations"][0]["aliases"].append("дом")
    state = {
        "current": {
            "location": "роддом",
            "present_characters": ["pov"],
        }
    }

    synced = location_runtime.sync_current_location(source, state)
    context = location_runtime.build_location_context(
        source,
        synced,
        scene_character_ids=["pov"],
    )

    assert "location_id" not in synced["current"]
    assert context is None


def test_short_zone_name_does_not_match_inside_unrelated_word():
    source = _novel()
    source["locations"][0]["zones"].append({"zone_id": "hall", "name": "Зал"})
    state = {
        "current": {
            "location": "Вокзал",
            "present_characters": ["pov"],
        }
    }

    synced = location_runtime.sync_current_location(source, state)
    context = location_runtime.build_location_context(
        source,
        synced,
        scene_character_ids=["pov"],
    )

    assert "location_id" not in synced["current"]
    assert context is None


def test_v5_setup_rejects_duplicate_canonical_location_ids_instead_of_silently_dropping_one():
    with pytest.raises(ValueError, match="DRAFT_LOCATION_ID_DUPLICATE"):
        simple_setup_runtime._validate_simple_content({
            "novel_id": "duplicate_locations",
            "title": "Duplicate Locations",
            "version": 5,
            "novel": {"pov_character": "rina"},
            "characters": [{"character_id": "rina", "name": "Рина", "is_pov": True}],
            "lore": {},
            "locations": [
                {"location_id": "silas_house", "name": "Дом Сайласа"},
                {"location_id": "Silas-House", "name": "Тот же id другой записью"},
            ],
        })


def test_v5_setup_rejects_duplicate_zone_ids_inside_one_location():
    with pytest.raises(ValueError, match="DRAFT_LOCATION_ZONE_ID_DUPLICATE"):
        simple_setup_runtime._validate_simple_content({
            "novel_id": "duplicate_zones",
            "title": "Duplicate Zones",
            "version": 5,
            "novel": {"pov_character": "rina"},
            "characters": [{"character_id": "rina", "name": "Рина", "is_pov": True}],
            "lore": {},
            "locations": [{
                "location_id": "silas_house",
                "name": "Дом Сайласа",
                "zones": [
                    {"zone_id": "guest_room", "name": "Гостевая"},
                    {"zone_id": "guest-room", "name": "Гостевая 2"},
                ],
            }],
        })


def test_location_normalization_preserves_duplicate_rows_for_explicit_validation():
    from app.profile_templates import normalize_location_profiles

    rows = normalize_location_profiles([
        {"location_id": "same", "name": "Первая"},
        {"location_id": "same", "name": "Вторая"},
    ])

    assert len(rows) == 2
    assert [row["name"] for row in rows] == ["Первая", "Вторая"]


def test_display_name_is_not_a_canon_note_subject_key_for_character():
    source = _novel()
    source["canon_notes"].append({
        "note_id": "name-only",
        "text": "DISPLAY_NAME_NOTE_MARKER",
        "subjects": ["Рината"],
    })
    state = source["starting_state"]

    notes = location_runtime.build_canon_notes_context(
        source,
        state,
        scene_character_ids=["pov"],
    )

    assert notes is not None
    assert "DISPLAY_NAME_NOTE_MARKER" not in [row["text"] for row in notes["notes"]]


def test_location_display_name_is_not_a_canon_note_subject_key():
    source = _novel()
    source["canon_notes"].append({
        "note_id": "location-name-only",
        "text": "LOCATION_DISPLAY_NAME_MARKER",
        "subjects": ["Дом Сайласа"],
    })
    state = source["starting_state"]

    notes = location_runtime.build_canon_notes_context(
        source,
        state,
        scene_character_ids=["pov"],
    )

    assert notes is not None
    assert "LOCATION_DISPLAY_NAME_MARKER" not in [row["text"] for row in notes["notes"]]
