"""Read scene pointers from both archived and compact author headers."""
import re
from typing import Any

_CLOCK = re.compile(
    r"^(?:🕒|◷)\s*День\s+(?P<game_day>\d+)\s*·[^\n]*?"
    r"(?P<date>\d{2}\.\d{2}\.\d{4})\s*[,·]\s*(?P<time>\d{1,2}:\d{2})",
    re.MULTILINE,
)
_LOCATION = re.compile(r"^(?:📍|⌖)\s*(?P<location>[^\n]+)", re.MULTILINE)
_SCENE = re.compile(r"^(?:⚙️\s*Сцена:|▸)\s*(?P<scene>.+?)\s*$", re.MULTILINE)


def parse_scene_header(text: Any) -> dict:
    if not isinstance(text, str):
        return {}
    # Do not interpret dialogue or footer as a header.
    text = text[:1200].split("Что я могу сделать:", 1)[0]
    clock = _CLOCK.search(text)
    result = {}
    if clock:
        result.update(date=clock["date"], time=clock["time"], game_day=max(1, int(clock["game_day"])))
        # Old archives may place the location on the same line as the clock.
        location = _LOCATION.search(text) or re.search(r"📍\s*(?P<location>[^\n]+)", clock.string[clock.end():])
        if location:
            result["location"] = re.split(r"\s*🌦️|\s*·\s*☁[︎️]?", location["location"], maxsplit=1)[0].strip()
    scene = _SCENE.search(text)
    if scene:
        result["scene"] = scene["scene"].strip()
    return result
