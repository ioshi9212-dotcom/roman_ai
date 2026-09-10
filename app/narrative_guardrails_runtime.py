from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from . import session_runtime, storage, writer_first_runtime
from .scene_format import validate_scene_output
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_GUARDRAIL_VERSION = 8
_TERMINAL = {"resolved", "closed", "expired", "cancelled", "canceled", "done", "abandoned"}
_HOOK_KEYS = (
    "fear", "страх", "weak", "слаб", "past", "прошл", "history", "истор",
    "trauma", "травм", "goal", "цель", "secret", "тайн", "profession",
    "професс", "family", "семь", "background", "биограф",
)

_validate_scene_output = validate_scene_output


def _status(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("status") or value.get("state") or value.get("phase") or "").casefold().strip()
    return ""


def _priority(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    raw = value.get("priority")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    return {"critical": 100, "high": 75, "medium": 50, "normal": 50, "low": 25}.get(str(raw or "").casefold(), 0)


def _intent_character_ids(state: Dict[str, Any]) -> set[str]:
    store = state.get("npc_intents")
    result: set[str] = set()
    if isinstance(store, dict):
        for cid, intents in store.items():
            rows = intents.values() if isinstance(intents, dict) else intents if isinstance(intents, list) else []
            if any(isinstance(row, dict) and _status(row) not in _TERMINAL for row in rows):
                result.add(str(cid))
    elif isinstance(store, list):
        for row in store:
            if isinstance(row, dict) and _status(row) not in _TERMINAL and row.get("character_id"):
                result.add(str(row["character_id"]))
    return result


def _cast_pressure(context: Dict[str, Any], state: Dict[str, Any], current_turn: int) -> List[Dict[str, Any]]:
    rows = context.get("cast_index") if isinstance(context.get("cast_index"), list) else []
    intent_ids = _intent_character_ids(state)
    scored: List[tuple[int, Dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("is_pov") or row.get("present"):
            continue
        cid = str(row.get("character_id") or "")
        if not cid:
            continue
        try:
            last_seen = int(row.get("last_seen_turn") or 0)
        except (TypeError, ValueError):
            last_seen = 0
        absent = max(0, current_turn - last_seen) if last_seen else current_turn
        role = str(row.get("role") or "").casefold()
        important = any(word in role for word in ("main", "major", "important", "глав", "ключ", "central"))
        has_intent = cid in intent_ids
        if absent < 15 and not (important and absent >= 8) and not has_intent:
            continue
        score = absent + (30 if has_intent else 0) + (20 if important else 0)
        item = {
            "character_id": cid,
            "name": row.get("name") or row.get("full_name"),
            "turns_since_seen": absent,
            "active_intent": has_intent,
            "important_role": important,
            "guidance": "Consider a natural re-entry/contact only if causally appropriate; load the character bundle before offscreen participation.",
        }
        scored.append((score, {k: v for k, v in item.items() if v not in (None, "", False)}))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:5]]


def _thread_turn(thread: Dict[str, Any]) -> int | None:
    for key in ("last_progress_turn", "last_turn", "updated_turn", "last_updated_turn", "start_turn", "created_turn"):
        raw = thread.get(key)
        try:
            if raw not in (None, ""):
                return int(raw)
        except (TypeError, ValueError):
            pass
    return None


def _story_pressure(context: Dict[str, Any], current_turn: int) -> List[Dict[str, Any]]:
    value = context.get("active_threads")
    if isinstance(value, dict):
        rows = [(str(key), item) for key, item in value.items() if isinstance(item, dict)]
    elif isinstance(value, list):
        rows = [
            (str(item.get("thread_id") or item.get("id") or index), item)
            for index, item in enumerate(value) if isinstance(item, dict)
        ]
    else:
        rows = []

    result: List[tuple[int, Dict[str, Any]]] = []
    for thread_id, thread in rows:
        if _status(thread) in _TERMINAL:
            continue
        priority = _priority(thread)
        last_turn = _thread_turn(thread)
        age = max(0, current_turn - last_turn) if last_turn is not None else None
        overdue = age is not None and age >= 12
        if not overdue and priority < 75:
            continue
        item = {
            "thread_id": thread_id,
            "summary": thread.get("summary") or thread.get("title") or thread.get("name"),
            "priority": priority,
            "turns_since_progress": age,
            "overdue": overdue,
            "guidance": "Keep this line alive through an existing cause, consequence, message, NPC action or scene beat; do not inject a random event.",
        }
        result.append((priority + (age or 0), {k: v for k, v in item.items() if v not in (None, "", False)}))
    result.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in result[:6]]


def _flatten_hooks(value: Any, path: str = "", depth: int = 0) -> Iterable[tuple[str, str]]:
    if depth > 3:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            key_norm = str(key).casefold()
            if any(token in key_norm for token in _HOOK_KEYS) and child not in (None, "", [], {}):
                if isinstance(child, (str, int, float, bool)):
                    yield child_path, str(child)
                elif isinstance(child, list):
                    compact = "; ".join(str(x) for x in child[:4] if isinstance(x, (str, int, float)))
                    if compact:
                        yield child_path, compact
            yield from _flatten_hooks(child, child_path, depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value[:8]):
            yield from _flatten_hooks(child, f"{path}[{index}]", depth + 1)


def _character_relevance(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    cards = context.get("character_cards") if isinstance(context.get("character_cards"), list) else []
    result: List[Dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        cid = str(card.get("character_id") or card.get("id") or "")
        hooks = []
        seen = set()
        for path, value in _flatten_hooks(card):
            pair = (path, value)
            if pair in seen:
                continue
            seen.add(pair)
            hooks.append({"path": path, "fact": " ".join(value.split())[:240]})
            if len(hooks) >= 4:
                break
        if hooks:
            result.append({
                "character_id": cid or None,
                "name": card.get("name") or card.get("full_name"),
                "card_hooks": hooks,
                "guidance": "Existing card facts only. Use when naturally relevant; never convert them into invented prior events or dialogue.",
            })
    return result[:5]


def _pov_activity_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "ordinary_dialogue_expected": True,
        "multiple_pov_lines_allowed": True,
        "automatic_without_player_input": [
            "короткое перемещение, взгляд, взять/положить обычный предмет",
            "достать/проверить телефон, прочитать доступное сообщение или уведомление",
            "обычное бытовое или техническое действие без существенного выбора",
            "продолжение уже выбранной работы, дороги, ожидания, подготовки или рутины",
            "нейтральный ответ, шутка, сарказм, комментарий или очевидный вопрос по характеру",
            "естественная физическая реакция и очевидное продолжение начатого действия",
        ],
        "reserved_for_player": [
            "значимое согласие или отказ, включая принятие/отклонение контакта или предложения, если это выражает позицию POV",
            "обещание, обязательство, признание или сознательная ложь",
            "раскрытие секрета или важной информации",
            "выбор стороны, существенная тактика или серьёзный риск",
            "сексуальное согласие",
            "решение или реплика, способные заметно изменить отношения, конфликт или направление сюжета",
        ],
        "instruction": (
            "POV не выключается после user_input. Доводи его действия, реплики и намерения до естественного завершения и свободно выполняй мелочи без существенного выбора. "
            "Не спрашивай игрока, смотреть ли, брать ли обычный предмет, проверять ли телефон или продолжать ли уже выбранную рутину, если это ничего существенно не меняет. "
            "Остановись, когда следующий шаг действительно определяет позицию POV или может заметно изменить отношения, конфликт, сюжет, риск или раскрытие информации."
        ),
    }


def _character_driven_behavior_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "no_psychological_correctness_filter": True,
        "no_boundary_compliance_filter": True,
        "drivers": [
            "характер и прошлое конкретного NPC",
            "его цели, желания и выгода",
            "отношения к POV и другим персонажам",
            "знания, настроение и текущая ситуация",
        ],
        "allowed_when_character_driven": [
            "подойти ближе или нарушить дистанцию",
            "коснуться, удержать или схватить",
            "поцеловать без предварительного вопроса",
            "решить что-то за POV или вмешаться",
            "надавить, ревновать, солгать, ошибиться или быть уверенным в собственной неправоте",
            "сделать то, что POV может не понравиться",
        ],
        "forbidden_authorial_filters": [
            "здоровая коммуникация как обязательная норма",
            "уважение границ как автоматическая причина поведения",
            "комфорт или безопасность POV как скрытый приоритет автора",
            "моральная или терапевтическая правильность вместо характера персонажа",
        ],
        "forbidden_respectability_narration": [
            "он не коснулся её, хотя мог",
            "он оставил ей пространство",
            "он не стал давить",
            "он ждал, пока она сама решит",
            "объяснение или похвала поведения через границы, правильность или психологию",
        ],
        "player_boundary": (
            "NPC может инициировать неприятное, навязчивое или физическое действие, если это естественно для него. "
            "Не выбирай за POV его значимую реакцию, согласие/отказ или другое решение с последствиями."
        ),
        "instruction": (
            "Пиши NPC изнутри его характера, целей, желаний, выгоды, отношений, знаний и ситуации. Не пропускай его решения через авторский фильтр психологии, здоровой коммуникации, границ, комфорта POV или того, как было бы правильно поступить. "
            "Если конкретный NPC по своим причинам хочет подойти, коснуться, схватить, вмешаться, надавить, решить за POV или поцеловать без предварительного вопроса, это допустимое действие персонажа; значимая реакция POV остаётся игроку. "
            "NPC не обязан нарушать границы: отказ от действия тоже должен идти из его характера, желания, страха, расчёта или ситуации, а не из авторской правильности. "
            "Не выделяй отсутствующее действие как добродетель или романтический знак. Не пиши формулы вроде 'он не коснулся её, хотя мог', 'оставил ей пространство', 'не стал давить', 'ждал, пока она сама решит', если именно эта сдержанность не является конкретным важным поступком данного персонажа. "
            "Показывай то, что реально произошло, без психологического, морального или терапевтического комментария."
        ),
    }


def _npc_intent_drive_rule(context: Dict[str, Any]) -> Dict[str, Any]:
    active = context.get("npc_active_intents")
    active = active if isinstance(active, dict) else {}
    count = sum(len(rows) for rows in active.values() if isinstance(rows, list))
    return {
        "mandatory": True,
        "active_intent_count": count,
        "source_path": "npc_active_intents",
        "closure_semantics": {
            "pursued_now": "NPC attempted to advance the intent; this does NOT mean the intent is satisfied.",
            "resolve": "Use only when the underlying question/goal/agreement is substantively satisfied or otherwise actually closed.",
            "abandon": "Use only when the NPC genuinely stops wanting to pursue it for character/situation reasons.",
        },
        "not_resolution": [
            "POV увилила, отшутилась, промолчала или сменила тему",
            "POV дала неполный, двусмысленный или явно неудовлетворительный ответ",
            "NPC один раз спросил, напомнил или попытался надавить",
            "договорённость или обещание только прозвучали, но ещё не выполнены",
            "разговор прервался, сцена закончилась или персонажи разошлись",
        ],
        "persistence_behavior": [
            "Если NPC всё ещё нужен ответ/результат, intent остаётся активным.",
            "Упрямый, подозрительный, заинтересованный или мотивированный NPC может продолжить дожим в той же сцене, если это естественно.",
            "Не повторяй одну и ту же фразу механически: меняй тактику по характеру — уточнить, переформулировать, поддеть, надавить, привести аргумент, поймать позже, проверить самому.",
            "Приоритет и характер определяют настойчивость. Вежливость или уклонение POV сами по себе не гасят чужую цель.",
            "Договорённость, обещание, долг, просьба или задача остаются активными до исполнения, явной отмены/пересмотра или настоящего отказа NPC от цели.",
        ],
        "instruction": (
            "npc_active_intents — это незакрытые собственные мотивы NPC, а не список тем, которые достаточно один раз упомянуть. "
            "Если eligible intent естественно относится к текущей ситуации, дай ему причинно влиять на поведение NPC. "
            "Увиливание POV, шутка, молчание, смена темы, неполный ответ или сам факт попытки НЕ закрывают intent. "
            "Если NPC всё ещё хочет ответ или результат, он может продолжить добиваться его сейчас или вернуться позже в другой форме, согласно характеру, приоритету, отношениям и обстоятельствам. "
            "Не превращай настойчивость в механическое повторение одной реплики. "
            "Ставь pursued_now=true только за реальную попытку продвинуть intent; operation=resolve только при фактическом удовлетворении/закрытии цели, operation=abandon только когда NPC действительно отказался от неё."
        ),
    }


def _player_input_scope(context: Dict[str, Any]) -> Dict[str, Any]:
    mapping = context.get("player_input_map") if isinstance(context.get("player_input_map"), dict) else {}
    ordered = mapping.get("ordered_segments") if isinstance(mapping.get("ordered_segments"), list) else []
    stage = mapping.get("stage_directions") if isinstance(mapping.get("stage_directions"), list) else []
    spoken = mapping.get("spoken_segments") if isinstance(mapping.get("spoken_segments"), list) else []
    last = ordered[-1] if ordered and isinstance(ordered[-1], dict) else {}
    return {
        "source_path": "player_input_map",
        "has_stage_direction": bool(stage),
        "has_spoken": bool(spoken),
        "last_segment_kind": last.get("kind"),
        "last_segment_text": str(last.get("text") or "")[:500],
        "boundary": "next_meaningful_pov_choice",
        "rule": (
            "Player input defines what POV has already chosen, not a mandatory stop after every literal action. "
            "If it delegates an ongoing ordinary activity, compress its uneventful continuation until completion, interruption or the next meaningful POV choice. "
            "If it is a bounded meaningful act, complete that act and immediate reactions but do not invent a later independent POV phase. "
            "Minor physical/technical actions and neutral communication happen automatically when they carry no meaningful choice."
        ),
    }


def _scene_momentum_rule(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mandatory": True,
        "ending_required": True,
        "important_scene_can_span_turns": True,
        "player_input_scope": _player_input_scope(context),
        "important_scene_is_progress": (
            "If action, contact, position, emotion, risk, information or a character goal is still changing, the scene itself is progressing even if it stays in the same place and plot thread."
        ),
        "visual_coverage": [
            "положение людей и дистанция, когда они важны",
            "видимые действия, взгляд, выражение и реакция",
            "изменение контакта, позы и важных предметов",
            "ощущения POV как дополнительный слой, а не замена внешнего кадра",
        ],
        "automatic_minor_actions": [
            "посмотреть, повернуть голову, сделать несколько шагов",
            "взять/положить обычный предмет",
            "достать или проверить телефон, прочитать доступное сообщение/уведомление",
            "продолжить уже выбранную обычную работу или рутину",
            "нейтрально ответить, если ответ не раскрывает важную информацию и не выражает значимую позицию",
        ],
        "meaningful_choice_boundary": [
            "согласиться или отказаться там, где это выражает позицию POV",
            "принять или отклонить значимый контакт, предложение, просьбу или обязательство",
            "рассказать/скрыть важное, солгать, признаться или дать обещание",
            "выбрать сторону, существенную тактику или серьёзный риск",
            "сделать реплику/действие, способные заметно изменить отношения, конфликт или сюжет",
        ],
        "do_not_compress": [
            "важную эмоциональную или интимную сцену только потому, что её общий исход уже понятен",
            "экшн, опасность или конфликт до абстрактной цепочки глаголов или итогового абзаца",
            "короткое содержательное действие POV вместе со следующим самостоятельным этапом, который игрок ещё не выбирал",
        ],
        "clarity": (
            "Пиши достаточно прямо, чтобы читатель понимал, что физически и эмоционально происходит. Не прячь сам факт действия за туманными эвфемизмами или одним сообщением результата."
        ),
        "player_choice": (
            "Не возвращай управление ради технической мелочи или очевидного продолжения уже выбранной обычной деятельности. "
            "Возвращай его, когда появляется новый значимый выбор. Бounded meaningful action можно закончить на самом действии и реакции NPC; ongoing routine можно сжать дальше."
        ),
        "time_skip_policy": (
            "Time skip сжимает уже выбранный игроком длительный обычный отрезок: работу, дорогу, ожидание, сон, подготовку, повторяющуюся рутину. "
            "Остановись раньше, если NPC/мир вмешались так, что требуется значимый выбор POV. Не используй time skip после короткого содержательного действия как разрешение придумать следующий самостоятельный этап POV."
        ),
        "anti_overstretch": (
            "Не превращай кинематографичность в каталог микродвижений. Несколько ходов оправданы только пока каждый ход даёт новый beat: меняет действие, контакт, положение, эмоцию, риск, информацию или цель."
        ),
        "valid_endings": [
            "следующий действительно значимый выбор POV",
            "событие/вмешательство NPC или мира, которое требует реакции с последствиями",
            "естественный конец порученной длительной рутины",
            "короткое содержательное действие POV и непосредственная реакция NPC, если дальше начинается уже новый самостоятельный этап",
        ],
        "invalid_endings": [
            "посмотреть, взять предмет, проверить телефон, продолжить обычную работу или другую техническую мелочь, если она не несёт существенного выбора",
            "рутина остановлена через несколько минут только ради выдачи трёх одинаково бессодержательных вариантов",
            "автор после короткого содержательного действия сам прожил за POV следующий час, ночь, день или новый самостоятельный этап",
            "важная сцена обрывается итоговым абзацем или time skip, хотя внутри неё ещё есть меняющиеся beats",
        ],
        "anti_therapy": (
            "Отдых и тишина могут существовать как обычная часть жизни, но не превращай их в эмоциональное лечение, безопасный опыт, заслуженное восстановление или отдельную арку облегчения."
        ),
        "causality": (
            "Не придумывай случайное событие только ради крючка. Во время порученной рутины NPC, intents, threads, расписание и последствия могут естественно вмешаться. "
            "Если ничего содержательного не происходит, сожми рутину до её конца вместо остановки на мелочи."
        ),
        "instruction": (
            "Граница хода — следующий значимый выбор POV, а не каждая мелкая операция. Выполняй бытовые/технические мелочи автоматически. "
            "Порученную работу, дорогу, ожидание, сон или повторяющуюся рутину сжимай до конца, естественного вмешательства или значимого выбора. "
            "Короткое содержательное действие доводи до результата и реакции, но не присваивай следующий самостоятельный этап POV. "
            "Важную эмоциональную, интимную, конфликтную или экшн-сцену показывай ясно и визуально, пока в ней есть новые beats."
        ),
    }


def _rewrite_packet(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base
        existing = context.get("narrative_guardrails")
        if isinstance(existing, dict) and existing.get("version") == _GUARDRAIL_VERSION:
            return base

        state = storage._read_json(root / "state.json", {})
        current_turn = max(0, int(packet.get("prepared_for_turn", 1) or 1) - 1)
        context["narrative_guardrails"] = {
            "version": _GUARDRAIL_VERSION,
            "pov_activity": _pov_activity_rule(),
            "character_driven_behavior": _character_driven_behavior_rule(),
            "npc_intent_drive": _npc_intent_drive_rule(context),
            "scene_momentum": _scene_momentum_rule(context),
            "cast_pressure": _cast_pressure(context, state if isinstance(state, dict) else {}, current_turn),
            "story_pressure": _story_pressure(context, current_turn),
            "character_relevance": _character_relevance(context),
            "instruction": (
                "pov_activity, character_driven_behavior, npc_intent_drive and scene_momentum are mandatory behavior rules. "
                "cast_pressure, story_pressure and character_relevance are soft anti-forgetting signals, not canon or mandatory beats. "
                "Prefer causal, natural use and never invent past events."
            ),
        }

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        size = writer_first_runtime.WRITER_PACKET_CHARS
        chunks = [text[index:index + size] for index in range(0, len(text), size)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["narrative_guardrail_version"] = _GUARDRAIL_VERSION
        storage._write_json(root / "turn_packet.json", packet)

        result = dict(base)
        result.update({
            "chunk_count": len(chunks),
            "total_chars": len(text),
            "first_chunk_included": True,
            "chunk_index": 0,
            "content": chunks[0],
            "all_chunks_read": len(chunks) == 1,
            "next_chunk_index": None if len(chunks) == 1 else 1,
            "narrative_guardrails": True,
        })
        return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def install() -> None:
    global _ORIGINAL_PREPARE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    session_runtime.prepare_turn_packet = _prepare_turn