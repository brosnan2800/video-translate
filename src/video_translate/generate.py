"""Generate the four Jianying-importable subtitle outputs.

Pure transformation (no network, no model): reads English segments + Chinese
mapping, emits bilingual/zh/en SRT plus a review TXT. Output is byte-for-byte
stable, which is why this stage carries the golden regression test.

Contract (preserved from the original gen_srt.py):
  - bilingual: Chinese line on top, English below.
  - zh index maps by position: zh[i-1] corresponds to segment i (1-based).
  - Empty lines are dropped from a cue (a segment with no zh still appears in
    bilingual with just the English line).
  - Files end with a single trailing newline; cues separated by blank lines.
"""
from __future__ import annotations

import os
from typing import Any

from .io_utils import load_json, write_text
from .srt_utils import block, srt_time

OUTPUT_SUFFIXES = (".bilingual.srt", ".zh.srt", ".en.srt", ".txt")


def build_outputs(
    segments: list[dict[str, Any]], zh: dict[int, str], *, gap: float = 0.0
) -> dict[str, str]:
    """Build the four output strings from segments + zh mapping.

    V3: each cue's window uses word-level boundaries when available
    (first word start / last word end) instead of the segment-level start/end —
    this drops leading silence so cues no longer appear "early" (Spec 15).
    `--gap` (default 0.2s) additionally trims trailing silence so adjacent cues
    keep at least `gap` spacing and never overlap; it never fabricates gaps where
    the real silence is already larger (ADR-009).

    Returns a dict keyed by suffix (".bilingual.srt", ".zh.srt", ".en.srt", ".txt").
    """
    # pass 1: resolve each cue's window (word-level if present)
    bounds: list[list[float]] = []
    for s in segments:
        words = s.get("words")
        if words:
            st, en = words[0]["start"], words[-1]["end"]
        else:
            st, en = s["start"], s["end"]
        bounds.append([st, en])
    # pass 2: --gap clamp (two-pass safe: needs the next cue's start)
    if gap and gap > 0:
        for i in range(len(bounds)):
            st, en = bounds[i]
            nxt = bounds[i + 1][0] if i + 1 < len(bounds) else None
            if nxt is not None:
                en = min(en, nxt - gap)
            if en < st:
                en = st
            bounds[i] = [st, en]

    bi, zhl, enl, txt = [], [], [], []
    for i, s in enumerate(segments, 1):
        st, en = bounds[i - 1]
        en_t = (s.get("text") or "").strip()
        cn = (zh.get(i - 1) or "").strip()
        bi.append(block(i, st, en, [l for l in [cn, en_t] if l]))
        if cn:
            zhl.append(block(i, st, en, [cn]))
        if en_t:
            enl.append(block(i, st, en, [en_t]))
        txt.append(f"[{srt_time(st)} -> {srt_time(en)}]\n中文: {cn}\n英文: {en_t}\n")
    return {
        ".bilingual.srt": "\n".join(bi).rstrip() + "\n",
        ".zh.srt": "\n".join(zhl).rstrip() + "\n",
        ".en.srt": "\n".join(enl).rstrip() + "\n",
        ".txt": "\n".join(txt).rstrip() + "\n",
    }


def generate_subtitles(
    segments_path: str,
    zh_path: str,
    outdir: str,
    *,
    base: str = "apollo_story",
    gap: float = 0.0,
    progress=print,
) -> list[str]:
    """Read segments + zh JSON, write the four outputs into `outdir`.

    Returns the list of written file paths.
    """
    segments = load_json(segments_path)
    zh_raw = load_json(zh_path)
    zh = {int(k): v for k, v in zh_raw.items()}

    outputs = build_outputs(segments, zh, gap=gap)
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []
    for suffix, content in outputs.items():
        path = os.path.join(outdir, base + suffix)
        write_text(path, content)
        written.append(path)
    progress(
        f"[generate] bilingual/zh/en/txt written for base={base!r} "
        f"({len(segments)} segments, gap={gap})"
    )
    return written
