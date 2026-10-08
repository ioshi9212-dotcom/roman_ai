from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Dict, List

from fastapi import HTTPException

from . import relationship_file_runtime, storage


_NUMERIC_RESULTS = {
    "updated",
    "unchanged",
    "no_numeric_dimension_justified",
}


def _error(code: str, message: str, **extra: Any) -> None:
    detail: Dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code=409, detail=detail)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _dimensions(relation: Dict[str, Any] | None) -> Dict[str, float]:
    if not isinstance(relation, dict):
        return {}
    raw = relation.get("dimensions") if isinstance(relation.get("dimensions"), dict) else {}
    result: Dict[str, float] = {}
    for label, row in raw.items():
        if not isinstance(row, dict):
            continue
        value = row.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[_norm(label)] = float(value)
    return result


def _dynamic(relation: Dict[str, Any] | None) -> str:
    if not isinstance(relation, dict):
        return ""
    return " ".join(str(relation.get("dynamic") or "").split())


_DURABLE_DYNAMIC_PATTERNS = (
    ("настороженность", re.compile(r"(?iu)\bнасторож\w*")),
    ("недоверие", re.compile(r"(?iu)\bнедовери\w*|\bне\s+доверя\w*")),
    ("подозрение", re.compile(r"(?iu)\bподозрева\w*|\bподозрен\w*")),
    ("страх", re.compile(r"(?iu)\bбо(?:итс|юсь|ятся)\w*|\bстрах\w*")),
    ("ревность", re.compile(r"(?iu)\bревну\w*|\bревност\w*")),
    ("привязанность", re.compile(r"(?iu)\bпривязан\w*")),
    ("уважение", re.compile(r"(?iu)\bуважа\w*|\bуважен\w*")),
    ("симпатия", re.compile(r"(?iu)\bсимпат\w*")),
    ("влечение", re.compile(r"(?iu)\bвлечен\w*|\bвлечение\w*")),
    ("соперничество", re.compile(r"(?iu)\bсопернич\w*")),
)

_POSITIVE_TRUST_RE = re.compile(r"(?iu)\bдоверя(?:ет|ют|ю|ем|ете)\b(?!\s+ли)")
_NEGATED_PREFIX_RE = re.compile(r"(?iu)(?:\bне|\bнет|\bбольше\s+не)\s*$")


def _durable_dynamic_evidence(*texts: str) -> List[str]:
    """Return explicit durable relational attitudes that make an empty numeric store suspicious."""
    text = " ".join(str(value or "") for value in texts if str(value or "").strip())
    normalized = _norm(text)
    if not normalized:
        return []

    result: List[str] = []

    for label, pattern in _DURABLE_DYNAMIC_PATTERNS:
        for match in pattern.finditer(normalized):
            prefix = normalized[max(0, match.start() - 18):match.start()]
            if label != "недоверие" and _NEGATED_PREFIX_RE.search(prefix):
                continue
            result.append(label)
            break

    for match in _POSITIVE_TRUST_RE.finditer(normalized):
        prefix = normalized[max(0, match.start() - 40):match.start()]
        if _NEGATED_PREFIX_RE.search(prefix):
            continue
        if re.search(r"(?iu)(?:не\s+решил\w*|не\s+зна\w*|не\s+уверен\w*|сомнева\w*)[^.!?]{0,35}$", prefix):
            continue
        result.append("доверие")
        break

    return list(dict.fromkeys(result))


def validate_relationship_review(
    session_id: str,
    payload: Dict[str, Any],
) -> None:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or packet.get("relationship_review_required") is not True:
        return

    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    state_before = storage._read_json(root / "state.json", {})
    pov = state_before.get("pov") if isinstance(state_before.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")

    before_store = relationship_file_runtime.load(
        root,
        cards=cards,
        state=state_before,
        pov_id=pov_id,
    )
    after_store = payload.get("_relationships_after")
    if not isinstance(after_store, dict):
        after_store = deepcopy(before_store)

    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    state_after = storage._deep_merge(state_before, patch)
    required_ids = relationship_file_runtime.physical_participant_ids(
        state_before,
        state_after,
        extracted,
        cards=cards,
    )
    review_rows = extracted.get("relationship_review")

    if not required_ids and (not isinstance(review_rows, list) or not review_rows):
        return

    if not isinstance(review_rows, list):
        for cid in required_ids:
            relation = relationship_file_runtime.character_relation(after_store, cid) or {}
            evidence = _durable_dynamic_evidence(_dynamic(relation))
            if not _dimensions(relation) and evidence:
                _error(
                    "RELATIONSHIP_DIMENSIONS_EMPTY_WITH_DURABLE_DYNAMIC",
                    "NPC has an explicitly evidenced durable relational attitude but empty numeric dimensions.",
                    character_id=cid,
                    dynamic=_dynamic(relation),
                    evidenced_dimensions=evidence,
                )
        _error(
            "RELATIONSHIP_REVIEW_DETAIL_REQUIRED",
            "relationship_review must contain one row for every NPC who physically participated in this turn.",
            required_character_ids=required_ids,
        )

    by_owner: Dict[str, Dict[str, Any]] = {}
    for raw in review_rows:
        if not isinstance(raw, dict):
            _error("RELATIONSHIP_REVIEW_DETAIL_INVALID", "Each relationship_review row must be an object.")
        owner_id = relationship_file_runtime._resolve_character_id(cards, raw.get("character_id"))
        if not owner_id or owner_id == pov_id:
            _error(
                "RELATIONSHIP_REVIEW_DETAIL_INVALID",
                "relationship_review contains an unknown or invalid NPC.",
            )
        owner_id = str(owner_id)
        if owner_id in by_owner:
            _error(
                "RELATIONSHIP_REVIEW_DETAIL_INVALID",
                "relationship_review contains a duplicate NPC row.",
                character_id=owner_id,
            )
        by_owner[owner_id] = raw

    missing = [cid for cid in required_ids if cid not in by_owner]
    if missing:
        for cid in missing:
            relation = relationship_file_runtime.character_relation(after_store, cid) or {}
            evidence = _durable_dynamic_evidence(_dynamic(relation))
            if not _dimensions(relation) and evidence:
                _error(
                    "RELATIONSHIP_DIMENSIONS_EMPTY_WITH_DURABLE_DYNAMIC",
                    "NPC has an explicitly evidenced durable relational attitude but empty numeric dimensions.",
                    character_id=cid,
                    dynamic=_dynamic(relation),
                    evidenced_dimensions=evidence,
                )
        _error(
            "RELATIONSHIP_REVIEW_DETAIL_REQUIRED",
            "relationship_review is missing one or more physically participating NPCs.",
            missing_character_ids=missing,
        )

    extras = [cid for cid in by_owner if cid not in set(required_ids)]
    if extras:
        _error(
            "RELATIONSHIP_REVIEW_DETAIL_INVALID",
            "relationship_review may contain only NPCs who physically participated in this turn.",
            unexpected_character_ids=extras,
        )

    update_rows = extracted.get("relationship_updates")
    update_rows = update_rows if isinstance(update_rows, list) else []
    updates_by_owner: Dict[str, Dict[str, Any]] = {}
    for raw in update_rows:
        if not isinstance(raw, dict):
            continue
        owner_id = relationship_file_runtime._resolve_character_id(cards, raw.get("character_id"))
        if owner_id:
            updates_by_owner[str(owner_id)] = raw

    for owner_id in required_ids:
        row = by_owner[owner_id]
        reason = " ".join(str(row.get("reason") or "").split())
        if not reason:
            _error(
                "RELATIONSHIP_REVIEW_REASON_REQUIRED",
                "Every relationship_review row requires a concrete review reason.",
                character_id=owner_id,
            )

        numeric_result = str(row.get("numeric_result") or "").strip().casefold()
        if numeric_result not in _NUMERIC_RESULTS:
            _error(
                "RELATIONSHIP_REVIEW_NUMERIC_RESULT_REQUIRED",
                "numeric_result must be updated, unchanged, or no_numeric_dimension_justified.",
                character_id=owner_id,
            )

        before_relation = relationship_file_runtime.character_relation(before_store, owner_id) or {}
        after_relation = relationship_file_runtime.character_relation(after_store, owner_id) or {}
        before_dims = _dimensions(before_relation)
        after_dims = _dimensions(after_relation)
        numeric_changed = before_dims != after_dims
        dynamic_changed = _dynamic(before_relation) != _dynamic(after_relation)
        actual_changed = numeric_changed or dynamic_changed
        declared_changed = row.get("changed")

        if not isinstance(declared_changed, bool):
            _error(
                "RELATIONSHIP_REVIEW_DETAIL_INVALID",
                "relationship_review.changed must be boolean.",
                character_id=owner_id,
            )
        if declared_changed != actual_changed:
            _error(
                "RELATIONSHIP_REVIEW_EFFECT_MISMATCH",
                "relationship_review.changed does not match the actual relationship persistence effect.",
                character_id=owner_id,
                actual_changed=actual_changed,
            )

        update = updates_by_owner.get(owner_id)
        if actual_changed and update is None:
            _error(
                "RELATIONSHIP_REVIEW_CHANGED_WITHOUT_UPDATE",
                "A real relationship change requires a matching relationship_updates row.",
                character_id=owner_id,
            )
        if not actual_changed and update is not None:
            _error(
                "RELATIONSHIP_REVIEW_UPDATE_CONTRADICTION",
                "An unchanged reviewed relationship cannot carry a relationship_updates row.",
                character_id=owner_id,
            )

        if not after_dims:
            durable_evidence = _durable_dynamic_evidence(
                _dynamic(after_relation),
                reason,
            )
            if durable_evidence:
                _error(
                    "RELATIONSHIP_DIMENSIONS_EMPTY_WITH_DURABLE_DYNAMIC",
                    "NPC has an explicitly evidenced durable relational attitude but empty numeric dimensions. Initialize the supported dimension instead of waiving numeric persistence.",
                    character_id=owner_id,
                    dynamic=_dynamic(after_relation),
                    evidenced_dimensions=durable_evidence,
                )
            if numeric_result != "no_numeric_dimension_justified":
                _error(
                    "RELATIONSHIP_REVIEW_NUMERIC_RESULT_INVALID",
                    "An empty dimension store is valid only after explicit no_numeric_dimension_justified review when no durable numeric relational attitude is evidenced.",
                    character_id=owner_id,
                )
            continue

        expected_numeric_result = "updated" if numeric_changed else "unchanged"
        if numeric_result != expected_numeric_result:
            _error(
                "RELATIONSHIP_REVIEW_NUMERIC_RESULT_INVALID",
                f"numeric_result must be {expected_numeric_result} for the final persisted relationship state.",
                character_id=owner_id,
                expected=expected_numeric_result,
            )
