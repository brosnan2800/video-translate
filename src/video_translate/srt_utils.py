"""SRT timestamp formatting.

Extracted verbatim from the original gen_srt.py so that generated subtitles are
byte-for-byte identical to the validated golden baseline. Do NOT "clean up" the
rounding logic — the ms==1000 carry fix is load-bearing for golden reproduction.
"""
from __future__ import annotations


def srt_time(t: float) -> str:
    """Format seconds as an SRT timestamp `HH:MM:SS,mmm`.

    Negative inputs clamp to 0. Handles the rounding edge case where
    round(frac*1000) == 1000 by carrying +1 second (avoids ",1000").
    """
    t = max(0.0, float(t))
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        t += 1.0
        ms = 0
    tot = int(t)
    h, r = divmod(tot, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def block(index: int, start: float, end: float, lines: list[str]) -> str:
    """Build one SRT cue block (trailing newline included).

    Format:
        {index}
        {start} --> {end}
        {line1}
        {line2}
    """
    return f"{index}\n{srt_time(start)} --> {srt_time(end)}\n" + "\n".join(lines) + "\n"
