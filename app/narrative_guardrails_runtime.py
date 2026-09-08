from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from . import session_runtime, storage, writer_first_runtime
from .scene_format import validate_scene_output
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_GUARDRAIL_VERSION = 2
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
        "allowed_without_player_input": [
            "бытовой или нейтральный ответ",
            "шутка, сарказм, подкол или огрызание по характеру",
            "очевидный вопрос из доступных POV фактов",
            "комментарий к происходящему",
            "несколько обычных реплик в продолжающемся разговоре",
            "мелкая реакция, мысль, движение или завершение уже выбранного действия",
        ],
        "reserved_for_player": [
            "согласие или отказ с заметными последствиями",
            "обещание, признание или сознательная ложь",
            "раскрытие секрета или важной тайны",
            "выбор стороны, серьёзный риск или существенная тактика",
            "сексуальное согласие",
            "решение или реплика, заметно меняющие сюжет, конфликт или отношения",
        ],
        "instruction": (
            "После выполнения user_input POV не выключается из сцены. В обычном разговоре продолжай его голос самостоятельно по характеру и ситуации. "
            "Не заканчивай ход только потому, что NPC задал обычный вопрос: если ответ не является значимым выбором, напиши ответ и продолжи обмен. "
            "POV может говорить несколько раз за один ход. Не своди его к кивкам, молчанию, однословным ответам или описанию тела из осторожности. "
            "Не придумывай за POV секреты, обязательства, признания, сознательную ложь или решения с заметными последствиями: перед таким выбором остановись для игрока."
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
            "cast_pressure": _cast_pressure(context, state if isinstance(state, dict) else {}, current_turn),
            "story_pressure": _story_pressure(context, current_turn),
            "character_relevance": _character_relevance(context),
            "instruction": (
                "pov_activity is a mandatory participation rule. cast_pressure, story_pressure and character_relevance are soft anti-forgetting signals, not canon or mandatory beats. "
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
