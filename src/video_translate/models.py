"""Data models shared across the pipeline.

The core unit is `Segment` — a single acoustic-timed text span produced by
faster-whisper. `start`/`end` are seconds (float); they are the one source of
truth for timing and must never be recomputed downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Segment:
    """A single subtitle segment with acoustic timestamps.

    Attributes:
        start: Start time in seconds.
        end: End time in seconds.
        text: Segment text (already stripped).
    """

    start: float
    end: float
    text: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Segment":
        """Build a Segment from a raw dict.

        Raises:
            KeyError: if 'start' or 'end' is missing.
        """
        return cls(
            start=float(d["start"]),
            end=float(d["end"]),
            text=(d.get("text") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk schema (rounded to 2 decimals)."""
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
        }
