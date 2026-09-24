from app.profile_templates import (
    normalize_character_profile,
    normalize_hidden_lore,
    normalize_novel_profile,
    render_character_profile,
    render_knowledge_journal,
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
