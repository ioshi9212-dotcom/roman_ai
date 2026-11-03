"""Single parser for dialogue lines across Roman AI's current and legacy formats.

Current Scene Builder: **Имя** *слова персонажа* (ремарка)
Legacy saved scenes: > **Имя** — слова персонажа
This is a data parser, not an alternative scene-writing instruction.
"""
from __future__ import annotations

import re
from typing import Iterator, NotRequired, TypedDict


class SpeechLine(TypedDict):
    speaker: str
    text: str
    position: int
    channel: NotRequired[str]


_PATTERN = re.compile(
    r"(?mi)^[ \t]*(?:>[ \t]*)?\*\*(?P<speaker>[^*\r\n]+)\*\*[ \t]*"
    r"(?:\((?P<channel>(?:сообщен\w*|звон\w*|смс|sms|телефон\w*|переписк\w*|по\s+(?:телефону|рации))[^)\r\n]*)\)[ \t]*)?"
    r"(?P<body>(?:[—-][ \t]*|\*(?!\*)).*)$"
)
_ITALIC_OPENING = re.compile(r"^\*(?P<speech>[^*\r\n]+)\*(?P<remainder>.*)$")


def iter_scene_speech(scene_output: str) -> Iterator[SpeechLine]:
    """Yield actual speaker lines with Markdown/dash wrappers removed."""
    story = re.split(r"(?m)^\s*(?:\*\*)?Что я могу сделать(?:[ \t]+\([^\r\n)]*\))?:", str(scene_output or ""), maxsplit=1)[0]
    for match in _PATTERN.finditer(story):
        body = match.group("body").strip()
        if body[:1] in {"—", "-"}:
            body = body[1:].strip()
        italic = _ITALIC_OPENING.match(body)
        if italic and not italic.group("speech").lstrip().startswith("("):
            body = (italic.group("speech") + italic.group("remainder")).strip()
        if body:
            line: SpeechLine = {
                "speaker": match.group("speaker").strip(),
                "text": body,
                "position": match.start(),
            }
            if match.group("channel"):
                line["channel"] = match.group("channel").strip()
            yield line
