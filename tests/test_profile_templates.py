from app.profile_templates import (
    normalize_canon_notes,
    normalize_character_profile,
    normalize_hidden_lore,
    normalize_location_profile,
    normalize_location_profiles,
    normalize_novel_profile,
    render_character_profile,
    render_knowledge_journal,
    render_location_profile,
)


def test_character_profile_shape_is_fixed_and_unknown_fields_go_to_additional():
    profile = normalize_character_profile({
        "id": "silas",
        "full_name": "Сайлас Вейн",
        "age": 400,
        "profession": "часовщик",
        "favorite_tea": "улун",
    })

    assert profile["character_id"] == "silas"
    assert profile["name"] == "Сайлас"
    assert profile["surname"] == "Вейн"
    assert profile["age"] == 400
    assert profile["work"] == "часовщик"
    assert profile["additional"]["favorite_tea"] == "улун"
    assert set(profile) == {
        "character_id", "name", "surname", "aliases", "age", "status", "role",
        "is_pov", "story_function", "appearance", "character", "speech", "habits",
        "work", "residence", "relationships", "abilities", "weaknesses", "goals",
        "background", "secrets_known_to_self", "notes", "generated_details", "additional",
    }


def test_novel_profile_shape_is_fixed():
    profile = normalize_novel_profile({
        "genres": "мистика",
        "pov": "rinata",
        "weird_custom_rule": "дождь важен",
    }, title="Пока мир не сгорит")

    assert profile["title"] == "Пока мир не сгорит"
    assert profile["genres"] == ["мистика"]
    assert profile["pov_character"] == "rinata"
    assert profile["additional"]["weird_custom_rule"] == "дождь важен"


def test_legacy_pov_silence_story_rule_is_removed_but_other_rules_stay():
    profile = normalize_novel_profile({
        "story_rules": (
            "Если игрок не дал реплику, не придумывать её. "
            "NPC действуют самостоятельно. Романтика остаётся основной линией."
        )
    })

    assert "если игрок не дал реплику" not in str(profile["story_rules"]).casefold()
    assert "не придумывать её" not in str(profile["story_rules"]).casefold()
    assert "NPC действуют самостоятельно" in profile["story_rules"]
    assert "Романтика остаётся основной линией" in profile["story_rules"]


def test_render_character_profile_is_plain_human_readable_text():
    text = render_character_profile({
        "character_id": "silas",
        "name": "Сайлас",
        "surname": "Вейн",
        "age": 400,
        "work": "часовщик",
    })

    assert "Имя: Сайлас" in text
    assert "Фамилия: Вейн" in text
    assert "Возраст: 400" in text
    assert "Работа: часовщик" in text
    assert "character_id" not in text
    assert "fact_id" not in text


def test_hidden_lore_is_plain_entries_without_fact_ids():
    lore = normalize_hidden_lore({
        "true_origin": "Сайлас не человек",
        "future_twist": "Рината пока не знает правду",
    })

    assert lore == {
        "entries": [
            "true_origin: Сайлас не человек",
            "future_twist: Рината пока не знает правду",
        ]
    }


def test_knowledge_journal_renders_date_period_and_plain_text():
    text = render_knowledge_journal([
        {"date": "24.09.2026", "period": "утро", "text": "Рината сказала, что ей 19 лет."},
        {"date": "24.09.2026", "period": "утро", "text": "Сайлас увидел её кольцо."},
        {"date": "24.09.2026", "period": "день", "text": "Она показала ему фотографии."},
    ])

    assert "24.09.2026 · утро" in text
    assert "Рината сказала, что ей 19 лет." in text
    assert "24.09.2026 · день" in text
    assert "fact_id" not in text


def test_location_profile_is_short_fixed_shape_and_accepts_mapped_zones_and_people():
    profile = normalize_location_profile({
        "id": "silas_house",
        "name": "Дом Сайласа",
        "type": "частный дом",
        "floor": "2 этажа",
        "hours": "частный дом, без режима",
        "people": {"rayna": "обслуживает дом"},
        "rooms": {
            "kitchen": "кухня на первом этаже",
            "study": {"name": "Кабинет", "summary": "отдельная рабочая комната"},
        },
        "style": "старый ухоженный дом",
        "features": ["кабинет на первом этаже"],
        "sofa_angle": "неважная микродеталь",
    })

    assert profile["location_id"] == "silas_house"
    assert profile["linked_characters"] == [{"character_id": "rayna", "relation": "обслуживает дом"}]
    assert profile["zones"] == [
        {"zone_id": "kitchen", "name": "kitchen", "summary": "кухня на первом этаже"},
        {"zone_id": "study", "name": "Кабинет", "summary": "отдельная рабочая комната"},
    ]
    assert profile["appearance"] == "старый ухоженный дом"
    assert profile["additional"]["sofa_angle"] == "неважная микродеталь"
    assert set(profile) == {
        "location_id", "name", "aliases", "type", "parent_location_id", "where", "floor",
        "hours", "staff", "linked_characters", "layout", "zones", "appearance", "fixed_features",
        "notes", "additional",
    }


def test_location_profile_renderer_stays_human_readable_and_compact():
    text = render_location_profile({
        "location_id": "adrian_school",
        "name": "Школа Эдриана",
        "hours": "09:00–21:00",
        "zones": [{"zone_id": "small_hall", "name": "Малый зал"}],
        "linked_characters": [{"character_id": "adrian", "relation": "владелец"}],
    })

    assert "Название: Школа Эдриана" in text
    assert "Часы работы: 09:00–21:00" in text
    assert "Малый зал" in text
    assert "владелец" in text
    assert "location_id:" not in text


def test_location_collection_and_scoped_canon_notes_normalize_without_new_freeform_shapes():
    locations = normalize_location_profiles({
        "school": {"name": "Школа", "zones": ["зал"]},
        "home": {"name": "Дом"},
    })
    notes = normalize_canon_notes({
        "valtor_household": {
            "text": "Семья Вальтор поколениями обслуживает дом.",
            "subjects": ["silas_house", "valtor_family"],
        }
    })

    assert [row["location_id"] for row in locations] == ["school", "home"]
    assert notes == [{
        "note_id": "valtor_household",
        "text": "Семья Вальтор поколениями обслуживает дом.",
        "subjects": ["silas_house", "valtor_family"],
    }]
