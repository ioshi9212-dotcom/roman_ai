"""Single parser for dialogue lines across Roman AI's current and legacy formats.

Current Scene Builder: **Имя** *слова персонажа* (ремарка)
Legacy saved scenes: > **Имя** — слова персонажа
This is a data parser, not an alternative scene-writing instruction.
"""
from __future__ import annotations

import re
from typing import Iterator, TypedDict


class SpeechLine(TypedDict):
    speaker: str
    text: str
    position: int


_PATTERN = re.compile(
    r"(?m)^[ \t]*(?:>[ \t]*)?\*\*(?P<speaker>[^*\r\n]+)\*\*[ \t]*(?P<body>(?:[—-][ \t]*|\*(?!\*)).*)$"
)
_ITALIC_OPENING = re.compile(r"^\*(?P<speech>[^*\r\n]+)\*(?P<remainder>.*)$")


def iter_scene_speech(scene_output: str) -> Iterator[SpeechLine]:
    """Yield actual speaker lines with Markdown/dash wrappers removed."""
    for match in _PATTERN.finditer(str(scene_output or "")):
        body = match.group("body").strip()
        if body[:1] in {"—", "-"}:
            body = body[1:].strip()
        else:
            italic = _ITALIC_OPENING.match(body)
            if italic:
                body = (italic.group("speech") + italic.group("remainder")).strip()
        if body:
            yield {
                "speaker": match.group("speaker").strip(),
                "text": body,
                "position": match.start(),
            }
