"""Detect zh/en index misalignment before rendering subtitles.

Motivation
----------
Agent translation is produced batch-by-batch keyed by segment index. If a
single line is skipped or duplicated while writing a batch, every subsequent
translation shifts by one. The English track stays correct, so the failure is
invisible in the pipeline logs — it only shows up when a human watches the
Chinese track and notices the lines drifting out of sync.

This module catches that class of bug automatically, without any per-video
glossary, using two language-agnostic signals:

1. Digit co-occurrence — a digit present in the source should also appear in
   its translation. A shifted translation loses those pairings.
2. Length-profile correlation — per-segment source length and translation
   length correlate strongly when aligned. Comparing the correlation at
   shift 0 against shifts +/-1, +/-2 over a sliding window exposes any region
   where a shifted pairing explains the data better than the actual one.

Signal 2 is the load-bearing one: it needs no vocabulary and works for any
language pair.
"""
from __future__ import annotations

import re
from typing import Any

_DIGITS = re.compile(r"\d+")


def _corr(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation; 0.0 when undefined (constant series / too short)."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def check_alignment(
    segments: list[dict[str, Any]],
    zh: dict[str, str],
    *,
    window: int = 24,
    margin: float = 0.20,
    max_shift: int = 2,
) -> list[dict[str, Any]]:
    """Return regions where the translation looks shifted against the source.

    Each finding is ``{start, end, shift, corr_aligned, corr_shifted}``.
    ``shift=s`` means ``zh[i+s]`` pairs with ``en[i]`` better than ``zh[i]``
    does. So ``shift=-1`` is the classic "translator skipped a line" case:
    every ``zh[i]`` actually belongs to ``en[i+1]``.
    An empty list means no misalignment was detected.
    """
    n = len(segments)
    if n < window + max_shift:
        return []

    en_len = [float(len((s.get("text") or "").strip())) for s in segments]
    zh_len = [float(len((zh.get(str(i)) or "").strip())) for i in range(n)]

    findings: list[dict[str, Any]] = []
    step = max(1, window // 2)
    for st in range(0, n - window + 1, step):
        ed = st + window
        base = _corr(en_len[st:ed], zh_len[st:ed])
        best_shift, best_corr = 0, base
        for sh in range(-max_shift, max_shift + 1):
            if sh == 0:
                continue
            a, b = st + sh, ed + sh
            if a < 0 or b > n:
                continue
            c = _corr(en_len[st:ed], zh_len[a:b])
            if c > best_corr:
                best_shift, best_corr = sh, c
        if best_shift != 0 and (best_corr - base) > margin:
            findings.append({
                "start": st, "end": ed, "shift": best_shift,
                "corr_aligned": round(base, 3),
                "corr_shifted": round(best_corr, 3),
            })

    # merge adjacent windows reporting the same shift
    merged: list[dict[str, Any]] = []
    for f in findings:
        if merged and merged[-1]["shift"] == f["shift"] and f["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = f["end"]
            merged[-1]["corr_aligned"] = min(merged[-1]["corr_aligned"], f["corr_aligned"])
            merged[-1]["corr_shifted"] = max(merged[-1]["corr_shifted"], f["corr_shifted"])
        else:
            merged.append(dict(f))
    return merged


def check_digits(
    segments: list[dict[str, Any]],
    zh: dict[str, str],
) -> list[dict[str, Any]]:
    """Flag segments whose source digits are missing from the translation.

    Reports ``found_at`` when the digits turn up in a neighbouring segment,
    which is a direct fingerprint of an off-by-N shift.
    """
    n = len(segments)
    out: list[dict[str, Any]] = []
    for i, s in enumerate(segments):
        nums = set(_DIGITS.findall((s.get("text") or "")))
        if not nums:
            continue
        cur = zh.get(str(i)) or ""
        if all(x in cur for x in nums):
            continue
        found_at = [
            d for d in (-2, -1, 1, 2)
            if 0 <= i + d < n and all(x in (zh.get(str(i + d)) or "") for x in nums)
        ]
        out.append({"index": i, "digits": sorted(nums), "found_at": found_at,
                    "text": (s.get("text") or "")[:60]})
    return out


def report(
    segments: list[dict[str, Any]],
    zh: dict[str, str],
    *,
    progress=print,
) -> bool:
    """Run both checks and print a summary. Returns True when clean."""
    shifts = check_alignment(segments, zh)
    digits = [d for d in check_digits(segments, zh) if d["found_at"]]
    if not shifts and not digits:
        progress(f"[align] ok — {len(segments)} segments, no shift detected")
        return True
    for f in shifts:
        drift = -f["shift"]
        progress(
            f"[align] WARNING segs {f['start']}-{f['end']}: zh[i] appears to "
            f"belong to en[i{drift:+d}] — translation drifted {drift:+d} "
            f"(corr aligned={f['corr_aligned']} vs shifted={f['corr_shifted']})"
        )
    for d in digits:
        progress(f"[align] WARNING seg {d['index']}: digits {d['digits']} "
                 f"missing here but present at offset {d['found_at']}")
    return False
