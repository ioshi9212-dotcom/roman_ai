"""Self-hidden card entries are character-writing context, not knowledge authority."""
from app import private_knowledge_runtime as privacy, storage
from test_generator_state_documents import create, story


def test_card_knowledge_authority_omits_explicitly_self_hidden_director_branches():
    card = {
        "character_id": "ren",
        "name": "Рен",
        "habits": {"description": "Пьёт чай"},
        "past": [
            {"text": "Собственный знакомый опыт"},
            {"text": "Пароль фуксия", "hidden_from_self": True},
            {"text": "Код бирюза", "known_to_self": False},
            {"text": "Тайная комиссия", "author_only": True},
            {"text": "Забытый адрес", "unknown_to_self": True},
            {"text": "Скрытый урок", "not_known_to_self": True},
        ],
        "hidden_lore": "Похищение индиго",
        "story_function": "Знает все секреты для автора",
        "author_notes": "Директорская заметка о нефрите",
    }
    visible = str(privacy._self_known_card_view(card))
    assert "Пьёт чай" in visible
    assert "Собственный знакомый опыт" in visible
    for secret in ("фуксия", "бирюза", "комиссия", "адрес",
                   "урок", "индиго", "для автора", "нефрите"):
        assert secret not in visible
    assert card["past"][1]["text"] == "Пароль фуксия"


def test_validator_authorized_corpus_never_reads_hidden_own_card_branch(story):
    sid, root = create(story)
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    for card in cards:
        if storage._card_id(card) == "ren":
            card["hidden_episode"] = {
                "hidden_from_self": True,
                "text": "Конверт из ультрамарина",
            }
            card["known_episode"] = {"text": "Любит прогулки"}
    corpus = privacy._authorized_corpus(root, "ren", cards)
    assert "Любит прогулки" in corpus
    assert "ультрамарина" not in corpus
