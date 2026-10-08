"""Opt-in-safe timings for Roman AI critical paths.

Only stage labels, operation names and durations are logged. Never log raw
user input, novel text, IDs or any character information.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from time import perf_counter
from typing import Iterator

LOG = logging.getLogger("uvicorn.error")  # Already configured for Amvera server logs


@contextmanager
def timed(operation: str, stage: str) -> Iterator[None]:
    started = perf_counter()
    try:
        yield
    finally:
        LOG.info(
            "roman_ai_perf operation=%s stage=%s duration_ms=%.2f",
            operation,
            stage,
            (perf_counter() - started) * 1000.0,
        )
