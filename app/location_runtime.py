from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List

from .profile_templates import (
    normalize_canon_notes,
    normalize_location_profiles,
    render_location_profile,
)


def _norm(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е").strip()
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _current(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.get("current") if isinstance(state.get("current"), dict) else {}


def _locations(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = source.get("locations")
    if raw is None:
        raw = source.get("location_profiles")
    return normalize_location_profiles(raw)


def _profile_refs(profile: Dict[str, Any]) -> set[str]:
    values = [
        profile.get("location_id"),
        profile.get("name"),
        *(profile.get("aliases") if isinstance(profile.get("aliases"), list) else []),
    ]
    return {_norm(value) for value in values if _norm(value)}


def _zone_refs(zone: Dict[str, Any]) -> set[str]:
    values = [zone.get("zone_id"), zone.get("name")]
    return {_norm(value) for value in values if _norm(value)}


def _profile_by_id(profiles: List[Dict[str, Any]], location_id: Any) -> Dict[str, Any] | None:
    needle = _norm(location_id)
    if not needle:
        return None
    for profile in profiles:
        if _norm(profile.get("location_id")) == needle:
            return profile
    return None


def _profile_by_visible_location(profiles: List[Dict[str, Any]], location: Any) -> Dict[str, Any] | None:
    needle = _norm(location)
    if not needle:
        return None

    exact = [profile for profile in profiles if needle in _profile_refs(profile)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    # Scene headers may contain "parent, zone" while old sessions may have only
    # the zone name. Resolve only when exactly one saved profile fits.
    candidates: List[Dict[str, Any]] = []
    for profile in profiles:
        refs = _profile_refs(profile)
        parent_match = any(ref and ref in needle for ref in refs)
        zone_match = any(
            ref and ref in needle
            for row in profile.get("zones", [])
            if isinstance(row, dict)
            for ref in _zone_refs(row)
        )
        if parent_match or zone_match:
            candidates.append(profile)
    return candidates[0] if len(candidates) == 1 else None


def _resolve_zone(profile: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any] | None:
    zones = profile.get("zones") if isinstance(profile.get("zones"), list) else []

    # Human-readable current.zone is the fresher physical pointer. Do not let an
    # old canonical zone_id override a newly written zone name.
    visible_zone = _norm(current.get("zone"))
    if visible_zone:
        matches = [zone for zone in zones if isinstance(zone, dict) and visible_zone in _zone_refs(zone)]
        if len(matches) == 1:
            return matches[0]
        return None

    explicit = _norm(current.get("zone_id"))
    if explicit:
        matches = [zone for zone in zones if isinstance(zone, dict) and explicit in _zone_refs(zone)]
        if len(matches) == 1:
            return matches[0]

    # Headers may render a parent + child together, e.g. "Дом Сайласа, кухня".
    visible_location = _norm(current.get("location") or current.get("place") or current.get("area"))
    if visible_location:
        matches = [
            zone
            for zone in zones
            if isinstance(zone, dict)
            and any(ref and ref in visible_location for ref in _zone_refs(zone))
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def resolve_physical_location(source: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any] | None:
    """Resolve only the place where POV physically is now.

    Mentioned, planned, remote or nearby locations are intentionally ignored.
    """
    profiles = _locations(source)
    if not profiles:
        return None

    current = _current(state)
    visible = current.get("location") or current.get("place") or current.get("area")
    profile = _profile_by_visible_location(profiles, visible)

    if profile is None:
        explicit = _profile_by_id(profiles, current.get("location_id"))
        if explicit is not None:
            # location_id may survive a state merge, so accept it only when the visible
            # pointer is absent or the visible text is this profile/one of its zones.
            visible_norm = _norm(visible)
            visible_is_profile = bool(
                visible_norm
                and any(ref and ref in visible_norm for ref in _profile_refs(explicit))
            )
            visible_is_zone = bool(
                visible_norm
                and any(
                    ref and ref in visible_norm
                    for row in explicit.get("zones", [])
                    if isinstance(row, dict)
                    for ref in _zone_refs(row)
                )
            )
            if not visible_norm or visible_norm in _profile_refs(explicit) or visible_is_profile or visible_is_zone:
                profile = explicit

    if profile is None:
        return None

    return {
        "profile": deepcopy(profile),
        "zone": deepcopy(_resolve_zone(profile, current)),
    }


def sync_current_location(
    source: Dict[str, Any],
    state: Dict[str, Any],
    *,
    previous_state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Keep canonical ids aligned with the visible physical scene pointer."""
    result = deepcopy(state) if isinstance(state, dict) else {}
    current = result.get("current") if isinstance(result.get("current"), dict) else {}
    result["current"] = current

    previous = _current(previous_state) if isinstance(previous_state, dict) else {}
    old_location = _norm(previous.get("location") or previous.get("place") or previous.get("area"))
    new_location = _norm(current.get("location") or current.get("place") or current.get("area"))
    if old_location and new_location and old_location != new_location:
        old_zone = _norm(previous.get("zone"))
        new_zone = _norm(current.get("zone"))
        old_zone_id = _norm(previous.get("zone_id"))
        new_zone_id = _norm(current.get("zone_id"))
        # Deep merge keeps omitted child fields. If the physical place changed
        # but zone/zone_id stayed byte-for-byte the same, they are stale.
        if old_zone == new_zone and old_zone_id == new_zone_id:
            current.pop("zone", None)
            current.pop("zone_id", None)

    resolved = resolve_physical_location(source, result)
    if resolved is None:
        current.pop("location_id", None)
        current.pop("zone_id", None)
        return result

    profile = resolved["profile"]
    current["location_id"] = str(profile.get("location_id") or "")
    zone = resolved.get("zone") if isinstance(resolved.get("zone"), dict) else None
    if zone is not None:
        current["zone_id"] = str(zone.get("zone_id") or "")
        if current.get("zone") in (None, ""):
            current["zone"] = str(zone.get("name") or zone.get("zone_id") or "")
    else:
        current.pop("zone_id", None)
    return result


def relevant_canon_notes(
    source: Dict[str, Any],
    state: Dict[str, Any],
    *,
    scene_character_ids: List[str],
) -> List[Dict[str, Any]]:
    notes = normalize_canon_notes(source.get("canon_notes"))
    if not notes:
        return []

    relevant: set[str] = {_norm("global")}
    relevant.update(_norm(value) for value in scene_character_ids if _norm(value))

    current = _current(state)
    for value in (
        current.get("location_id"),
        current.get("location"),
        current.get("place"),
        current.get("area"),
        current.get("zone_id"),
        current.get("zone"),
    ):
        if _norm(value):
            relevant.add(_norm(value))

    location = resolve_physical_location(source, state)
    if isinstance(location, dict):
        profile = location.get("profile") if isinstance(location.get("profile"), dict) else {}
        relevant.update(_profile_refs(profile))
        zone = location.get("zone") if isinstance(location.get("zone"), dict) else {}
        relevant.update(_zone_refs(zone))

    result: List[Dict[str, Any]] = []
    for note in notes:
        subjects = {_norm(value) for value in note.get("subjects", []) if _norm(value)}
        if subjects and not (subjects & relevant):
            continue
        # Unscoped notes stay persisted but are not injected into every scene.
        if not subjects:
            continue
        result.append(deepcopy(note))
    return result


def build_location_context(
    source: Dict[str, Any],
    state: Dict[str, Any],
    *,
    scene_character_ids: List[str],
) -> Dict[str, Any] | None:
    resolved = resolve_physical_location(source, state)
    if resolved is None:
        return None

    profile = resolved["profile"]
    zone = resolved.get("zone") if isinstance(resolved.get("zone"), dict) else None
    result: Dict[str, Any] = {
        "physical_presence_only": True,
        "location_id": str(profile.get("location_id") or ""),
        "name": str(profile.get("name") or profile.get("location_id") or ""),
        "profile": render_location_profile(profile),
        "linked_characters": deepcopy(profile.get("linked_characters", [])),
        "rule": (
            "This profile is loaded only because POV is physically here. Treat its layout, listed zones and fixed features "
            "as stable spatial canon. Do not invent a new permanent room, floor, facility or fixed feature just to serve the scene; "
            "do not move fixed layout between turns. Temporary ordinary objects may vary when they do not contradict canon. "
            "linked_characters is an author directory, not personal knowledge; load a registered offscreen character bundle before participation."
        ),
    }
    if zone is not None:
        result["current_zone"] = deepcopy(zone)
    return result


def build_canon_notes_context(
    source: Dict[str, Any],
    state: Dict[str, Any],
    *,
    scene_character_ids: List[str],
) -> Dict[str, Any] | None:
    notes = relevant_canon_notes(
        source,
        state,
        scene_character_ids=scene_character_ids,
    )
    if not notes:
        return None
    return {
        "notes": notes,
        "rule": (
            "Scoped durable author canon only. Subjects make a note relevant to the current physical place, zone, "
            "scene character or global context. These notes are not personal knowledge unless a character has a real source."
        ),
    }
