from __future__ import annotations

import json
from typing import Any, Dict, List

from . import relationship_runtime, runtime_fixes as base, storage


_STRICT_REWRITE_TURN_PACKET = None
_INSTALLED = False


def _soft_validate_visible_footer(
    scene_output: str,
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    state_after: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Validate relationship data that is actually present without turning formatting into a save gate.

    Established dimensions are still protected from accidental replacement and bad delta math.
    A fresh NPC is allowed to have no numeric baseline until the scene genuinely establishes one.
    Missing/stale cosmetic footer rows never destroy already-persisted relationship state.
    """
    footer = base._parse_footer(
        scene_output,
        cards=cards,
        resolve_character_id=base._resolve_character_id,
    )
    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    final_present = set(base.storage._present_character_ids(state_after))

    for owner_id, incoming in footer.items():
        owner_id = str(owner_id)
        if owner_id == pov_id:
            # POV -> NPC is never a valid relationship direction. Ignore the cosmetic row
            # rather than rejecting an otherwise persistable scene.
            continue
        if owner_id not in final_present:
            # A stale visible row cannot mutate an absent NPC because relationship persistence
            # already filters to final-present owners. Treat it as display noise, not corruption.
            continue
        baseline = base._numeric_relationships(state_before, owner_id)
        if baseline:
            base._validate_dimensions(incoming, baseline)
        elif incoming and not 1 <= len(incoming) <= 3:
            base._http_error(
                409,
                "RELATIONSHIP_BASELINE_INVALID",
                "A newly established relationship baseline may contain 1-3 natural dimensions.",
            )

    return footer


def _rewrite_turn_packet(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    if _STRICT_REWRITE_TURN_PACKET is None:
        raise RuntimeError("RELATIONSHIP_RESILIENCE_NOT_INSTALLED")

    result = _STRICT_REWRITE_TURN_PACKET(session_id, manifest)
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    raw = "".join(packet.get("chunks", []))
    if not raw:
        return result
    context = json.loads(raw)

    policy = context.get("relationship_policy")
    if not isinstance(policy, dict):
        policy = {}
    policy["source_of_truth"] = "relationship_lens + relationship_contract"
    policy["footer_required_for_every_present_npc"] = False
    policy["fresh_baseline_required"] = False
    policy["saved_dimensions_are_durable"] = True
    policy["footer_validation"] = (
        "Server-enforced persistence is tolerant: a missing cosmetic footer row does not fail the turn or erase saved relationship state. "
        "If a row for an established relationship is printed, all established dimensions and any displayed delta arithmetic must remain consistent. "
        "A fresh NPC gets 1-3 numeric dimensions only when the scene genuinely establishes a directed attitude."
    )
    policy["instruction"] = (
        "Use relationship_lens and relationship_contract as the causal NPC->POV relationship model. "
        "Preserve established dimensions across scenes and absences. Do not invent a fresh numeric baseline merely because an NPC is physically present. "
        "When the current interaction genuinely establishes sympathy, suspicion, attraction, respect, irritation, jealousy, trust, resentment, closeness or another specific attitude, "
        "create 1-3 natural dimensions and show them. Do not use generic interest as a filler. "
        "For an established relationship, prefer showing its complete saved row even when unchanged; if a cosmetic row is accidentally omitted, persistence keeps the saved state instead of rejecting the turn."
    )
    context["relationship_policy"] = policy

    lens = context.get("relationship_lens")
    if isinstance(lens, dict):
        lens["initialization_required"] = False
        lens["initialization_instruction"] = (
            "Evaluate every present NPC, but initialize numeric dimensions only when this scene genuinely establishes a directed attitude toward POV. "
            "Mere presence, visibility or professional neutrality does not require invented numbers. Existing saved dimensions remain durable."
        )
        lens["instruction"] = (
            "Relationship dimensions are causal state, not decorative numbers. Combine saved dimensions with personality, goals, knowledge and current state. "
            "Preserve established labels and values across absences. For an empty relation, initialize 1-3 natural dimensions only when a real attitude is established in play."
        )
        candidates = lens.get("present_npc_candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                candidate["initialization_rule"] = (
                    "If saved_dimensions exist, continue exactly this relationship. If empty, evaluate the NPC normally and create 1-3 dimensions only when the current scene genuinely establishes an attitude; presence alone is not a reason to invent a baseline."
                )
    context["relationship_lens_instruction"] = (
        "relationship_lens is authoritative for current NPC->POV relations. Existing dimensions persist and should normally be shown in the visible Relationships footer. "
        "Fresh NPCs do not need filler numbers solely because they are present; initialize dimensions only from a real interaction or perception that establishes an attitude."
    )

    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    chunks = [
        text[i : i + storage.MAX_PACKET_CHARS]
        for i in range(0, len(text), storage.MAX_PACKET_CHARS)
    ] or ["{}"]
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    packet["read_chunks"] = []
    packet["relationship_resilience_version"] = 1
    storage._write_json(root / "turn_packet.json", packet)

    result = dict(result)
    result["chunk_count"] = len(chunks)
    result["instruction"] = (
        str(result.get("instruction", "")).replace(
            "Every present NPC requires a visible NPC->POV relationship row; fresh NPCs initialize a 1-3 dimension baseline.",
            "Relationship persistence is resilient: preserve saved NPC->POV dimensions; fresh numeric baselines arise only from genuine attitude-forming interaction.",
        )
    ).strip()
    return result


def install() -> None:
    global _STRICT_REWRITE_TURN_PACKET, _INSTALLED
    if _INSTALLED:
        return
    _STRICT_REWRITE_TURN_PACKET = base._rewrite_turn_packet
    base._validate_visible_footer = _soft_validate_visible_footer
    base._rewrite_turn_packet = _rewrite_turn_packet
    _INSTALLED = True
