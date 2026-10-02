import json
import tempfile
from pathlib import Path

from app import location_runtime, session_runtime, simple_setup_runtime, storage


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
        assert location["canon_notes"][0]["text"] == "HOUSE_NOTE_MARKER"

        # Mentioning another saved place is not enough to pull its profile.
        assert "SCHOOL_ONLY_MARKER" not in raw
        assert "SCHOOL_NOTE_MARKER" not in raw
        assert "UNSCOPED_NOTE_MARKER" not in raw

        # A linked person is only a short directory reference. Their full card stays unloaded.
        assert "HOUSEKEEPER_CARD_ONLY_MARKER" not in raw
        assert "rayna" not in context.get("character_profiles", {})

        # Full persistent location collections must not leak through generic source transport.
        assert "locations" not in context.get("source_extra", {})
        assert "canon_notes" not in context.get("source_extra", {})


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
    with __import__("pytest").raises(ValueError, match="DRAFT_LOCATION_CHARACTER_UNKNOWN"):
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
