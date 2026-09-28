import json
import tempfile
from pathlib import Path

import pytest

from app import operation_service, session_runtime, storage
from app.runtime_contract import (
    RUNTIME_CONTRACT_VERSION,
    RuntimeContractError,
    validate_runtime_contract,
)


def _setup(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return {
        "novel_id": "runtime_contract_test",
        "title": "Тестовая новелла",
        "novel": {"pov_character": "elena"},
        "characters": [
            {"character_id": "elena", "name": "Елена", "is_pov": True},
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "elena"},
            "current": {
                "date": "22.09.2026",
                "time": "10:00",
                "location": "база",
                "present_characters": ["elena"],
            },
        },
    }


def _main_scene(include_spoken: bool = True) -> str:
    lead = "Елена сказала: Проверка речи. " if include_spoken else "Елена молча осмотрелась. "
    filler = (
        "В комнате было тихо, но за дверью слышались шаги и приглушённые голоса. "
        "Елена перевела взгляд на окно, потом на стол и снова на дверь. "
        "Она поправила рукав, проверила телефон и осталась на месте, следя за происходящим. "
    )
    text = lead
    while len(text) < 2200:
        text += filler
    return text[:2350]


def _scene(
    *,
    title: str = "Тестовая новелла",
    turn: int = 1,
    cycle: int = 1,
    include_spoken: bool = True,
    extra_lower: str = "",
) -> str:
    main = _main_scene(include_spoken=include_spoken)
    return f"""🎭 {title} · осень
🕒 День 1 · вторник, 22.09.2026, 10:00 ·
📍 база 🌦️ Погода: ясно
⚙️ Сцена: разговор в комнате
✦ Елена 🧥 Одежда, волосы: футболка, хвост ◈ Инвентарь: телефон

--------------------------------------------------------
{main}

Что я могу сделать:
1. Подойти к двери.
2. Проверить телефон.
3. Сесть за стол.
{extra_lower}
Что я могу сказать:
1. «Кто там?»
2. «Я сейчас выйду.»
3. «Подождите минуту.»

Что я могу подумать:
1. Шум какой-то странный.
2. Надо сначала понять, что происходит.
3. Посмотрим, кто пришёл.

Состояние: спокойная, собранная, внимательная
Отношения:

Ход {turn} · цикл {cycle}/15"""


def _payload(scene_output: str, **extracted_overrides):
    extracted = {
        "runtime_rules_reviewed": True,
        "scene_builder_reviewed": True,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
    }
    extracted.update(extracted_overrides)
    return {
        "packet_id": "packet",
        "user_input": "Проверка речи. (осмотреться)",
        "scene_output": scene_output,
        "extracted": extracted,
    }


def test_runtime_contract_accepts_exact_scene_builder_and_rules_review():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        validate_runtime_contract(sid, _payload(_scene()))


def test_runtime_contract_requires_both_document_reviews_and_version():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]

        with pytest.raises(RuntimeContractError, match="RUNTIME_RULES_REVIEW_REQUIRED"):
            validate_runtime_contract(
                sid,
                _payload(_scene(), runtime_rules_reviewed=False),
            )
        with pytest.raises(RuntimeContractError, match="SCENE_BUILDER_REVIEW_REQUIRED"):
            validate_runtime_contract(
                sid,
                _payload(_scene(), scene_builder_reviewed=False),
            )
        with pytest.raises(RuntimeContractError, match="RUNTIME_CONTRACT_VERSION_MISMATCH"):
            validate_runtime_contract(
                sid,
                _payload(_scene(), runtime_contract_version=999),
            )


def test_runtime_contract_rejects_short_and_oversized_scene_wrong_title_and_wrong_footer():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]

        short = _scene().replace(_main_scene(), "Проверка речи. Короткая сцена.")
        with pytest.raises(RuntimeContractError, match="SCENE_BUILDER_MAIN_LENGTH_INVALID"):
            validate_runtime_contract(sid, _payload(short))

        oversized = _scene().replace(_main_scene(), "Длинная сцена. " + ("x" * 3100))
        with pytest.raises(RuntimeContractError, match="SCENE_BUILDER_MAIN_LENGTH_INVALID"):
            validate_runtime_contract(sid, _payload(oversized))

        with pytest.raises(RuntimeContractError, match="SCENE_BUILDER_TITLE_MISMATCH"):
            validate_runtime_contract(sid, _payload(_scene(title="Чужое название")))

        with pytest.raises(RuntimeContractError, match="SCENE_BUILDER_TURN_FOOTER_MISMATCH"):
            validate_runtime_contract(sid, _payload(_scene(turn=2, cycle=2)))


def test_runtime_contract_rejects_lower_block_drift():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        broken = _scene(extra_lower="Елена ещё раз посмотрела на дверь.\n")
        with pytest.raises(RuntimeContractError, match="SCENE_BUILDER_OPTIONS_INVALID"):
            validate_runtime_contract(sid, _payload(broken))


def test_runtime_contract_rejects_lost_player_speech():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        with pytest.raises(RuntimeContractError, match="RUNTIME_RULE_PLAYER_SPEECH_NOT_PRESERVED"):
            validate_runtime_contract(sid, _payload(_scene(include_spoken=False)))


def test_runtime_contract_requires_player_spoken_segments_in_left_to_right_order():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]

        ordered_main = "Первая реплика. Елена убрала телефон. Вторая реплика.\n" + _main_scene(include_spoken=False)
        ordered_scene = _scene().replace(_main_scene(), ordered_main)
        ordered_payload = _payload(ordered_scene)
        ordered_payload["user_input"] = "Первая реплика. (убрать телефон) Вторая реплика."
        validate_runtime_contract(sid, ordered_payload)

        reversed_main = "Вторая реплика. Елена убрала телефон. Первая реплика.\n" + _main_scene(include_spoken=False)
        reversed_scene = _scene().replace(_main_scene(), reversed_main)
        reversed_payload = _payload(reversed_scene)
        reversed_payload["user_input"] = "Первая реплика. (убрать телефон) Вторая реплика."
        with pytest.raises(RuntimeContractError, match="RUNTIME_RULE_PLAYER_SPEECH_ORDER_INVALID"):
            validate_runtime_contract(sid, reversed_payload)


def test_v5_runtime_contract_capability_exposes_hard_contract_without_strict_knowledge():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        packet = operation_service.prepare_turn_request(
            sid,
            "(осмотреться)",
            request_id="runtime-contract-packet",
            scene_archive_capable=True,
            knowledge_review_capable=True,
            runtime_contract_capable=True,
            strict_knowledge_capable=False,
        )
        parts = [packet["content"]]
        for index in range(1, packet["chunk_count"]):
            parts.append(storage.get_turn_packet_chunk(sid, packet["packet_id"], index)["content"])
        context = json.loads("".join(parts))

        assert packet["runtime_contract_capable"] is True
        assert context["runtime_contract"]["version"] == RUNTIME_CONTRACT_VERSION
        assert "main_scene_2000_3000_chars" in context["runtime_contract"]["hard_checks"]
        assert context["runtime_rules"]
        assert context["scene_builder"]


def test_commit_turn_enforces_runtime_contract_when_capability_was_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        packet = operation_service.prepare_turn_request(
            sid,
            "Проверка речи. (осмотреться)",
            request_id="runtime-contract-commit",
            runtime_contract_capable=True,
        )
        short = _scene().replace(_main_scene(), "Проверка речи. Короткая сцена.")
        payload = _payload(short)
        payload["packet_id"] = packet["packet_id"]

        with pytest.raises(RuntimeContractError, match="SCENE_BUILDER_MAIN_LENGTH_INVALID"):
            operation_service.commit_turn_request(sid, payload)


def test_existing_pending_turn_is_not_retroactively_hard_gated():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        first = operation_service.prepare_turn_request(
            sid,
            "(осмотреться)",
            request_id="runtime-contract-pending",
            runtime_contract_capable=False,
        )
        resumed = operation_service.prepare_turn_request(
            sid,
            "(осмотреться)",
            request_id="runtime-contract-pending",
            runtime_contract_capable=True,
        )

        assert first["runtime_contract_capable"] is False
        assert resumed["runtime_contract_capable"] is False



def test_first_scene_control_command_is_not_forced_into_pov_speech():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        payload = _payload(_scene(include_spoken=False))
        payload["user_input"] = "запускай первую сцену"
        validate_runtime_contract(sid, payload)
