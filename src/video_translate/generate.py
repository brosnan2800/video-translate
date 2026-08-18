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
import re
from typing import Any

from .io_utils import load_json, write_text, save_json
from .srt_utils import block, srt_time

OUTPUT_SUFFIXES = (".bilingual.srt", ".zh.srt", ".en.srt", ".txt")


def build_outputs(
    segments: list[dict[str, Any]], zh: dict[int, str], *,
    gap: float = 0.0, min_dur: float = 0.0,
    offset: float = 0.0, tail: float = 0.0,
) -> dict[str, str]:
    """Build the four output strings from segments + zh mapping.

    V3: each cue's window uses word-level boundaries when available
    (first word start / last word end) instead of the segment-level start/end —
    this drops leading silence so cues no longer appear "early" (Spec 15).
    `--gap` (default 0.2s) additionally trims trailing silence so adjacent cues
    keep at least `gap` spacing and never overlap; it never fabricates gaps where
    the real silence is already larger (ADR-009).

    V4: `min_dur` extends the DISPLAY window of too-short cues (display-only;
    the cue's start — the alignment fact — never moves). Constraint order:
    min_dur is a soft target, `--gap` non-overlap is the hard constraint and
    wins when the room is insufficient. Library default is 0.0 (off) so golden
    outputs stay byte-exact; the CLI enables it by default (--min-dur 1.0).

    V6 (B1'): `offset` and `tail` are display-layer corrections for Whisper's
    word-timestamp drift. Whisper derives word boundaries by DTW over attention
    weights — a *posterior estimate*, not an acoustic fact — and it systematically
    lands early on speech onsets, which reads as "the subtitle fires before the
    line is spoken and clears before it ends".
      - `offset`: shifts the whole window later (positive) or earlier (negative).
        Corrects systematic drift. Start is clamped at 0.
      - `tail`: extends only the end, so a cue lingers long enough to finish
        reading. Safe by default because the gap clamp reclaims any overlap.
    Both are display-only: the underlying segment/word timestamps in
    segments_en.json are never rewritten (the alignment invariant).

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
    # pass 2: display shift (drift correction) + tail extension
    if offset:
        for i in range(len(bounds)):
            st, en = bounds[i]
            st = max(0.0, st + offset)
            en = max(st, en + offset)
            bounds[i] = [st, en]
    if tail and tail > 0:
        for i in range(len(bounds)):
            bounds[i][1] += tail
    # pass 3: min-dur display extension (soft target; start never moves)
    if min_dur and min_dur > 0:
        for i in range(len(bounds)):
            st, en = bounds[i]
            if en - st < min_dur:
                bounds[i] = [st, st + min_dur]
    # pass 4: --gap clamp (hard constraint, wins over tail/min-dur extension).
    # Also runs whenever tail/offset are active, because those are the passes
    # that can manufacture an overlap in the first place.
    if (gap and gap > 0) or (tail and tail > 0) or offset:
        gap = max(gap or 0.0, 0.0)
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


def _resolve_out_base(outdir: str, base: str, flat: bool) -> tuple[str, str]:
    """Resolve the actual (directory, file-base) for the final outputs.

    Default mode (flat=False): the four outputs are written into a per-video
    subfolder ``<outdir>/<base>/`` with a collision-based version suffix. The
    first run writes ``<base>.{suffix}``; if that already exists, outputs bump
    to ``<base>_v1.{suffix}``, ``_v2``, ... so re-runs never overwrite and
    video editors (e.g. Jianying) treat each as a fresh import — no stale cache.

    Legacy mode (flat=True): write ``<base>{suffix}`` directly into ``outdir``
    with no subfolder and no version suffix (deterministic, for tests/scripts).
    """
    if flat:
        return outdir, base
    sub = os.path.join(outdir, base)
    pat = re.compile(re.escape(base) + r"(?:_v(\d+))?\.bilingual\.srt$")
    has_plain = False
    max_n = 0
    if os.path.isdir(sub):
        for fn in os.listdir(sub):
            m = pat.match(fn)
            if not m:
                continue
            if m.group(1) is None:
                has_plain = True
            else:
                max_n = max(max_n, int(m.group(1)))
    ver = "" if not has_plain and max_n == 0 else f"_v{max_n + 1}"
    return sub, base + ver


def _prune_old_versions(out_dir: str, base: str) -> None:
    """Keep only the 2 newest versioned output sets inside ``out_dir``.

    A "set" is the four files sharing one stem: ``<base>`` (plain) or
    ``<base>_vN``. The plain set counts as the oldest. Everything except the
    two most-recently-modified stems is removed.
    """
    stem_pat = re.compile(r"^" + re.escape(base) + r"(?:_v(\d+))?$")
    stems: dict[str, float] = {}
    for fn in os.listdir(out_dir):
        for suffix in OUTPUT_SUFFIXES:
            if fn.endswith(suffix):
                stem = fn[: -len(suffix)]
                break
        else:
            continue
        if not stem_pat.match(stem):
            continue
        if stem in stems:
            continue
        try:
            stems[stem] = os.path.getmtime(os.path.join(out_dir, fn))
        except OSError:
            stems[stem] = 0.0
    if len(stems) <= 2:
        return
    for stem, _ in sorted(stems.items(), key=lambda kv: kv[1])[:-2]:
        for suffix in OUTPUT_SUFFIXES:
            p = os.path.join(out_dir, stem + suffix)
            if os.path.exists(p):
                os.remove(p)


def generate_subtitles(
    segments_path: str,
    zh_path: str,
    outdir: str,
    *,
    base: str = "apollo_story",
    gap: float = 0.0,
    min_dur: float = 0.0,
    offset: float = 0.0,
    tail: float = 0.0,
    flat: bool = False,
    prune_old: bool = False,
    progress=print,
) -> list[str]:
    """Read segments + zh JSON, write the four outputs.

    By default (flat=False) the four outputs are written into a per-video
    subfolder ``<outdir>/<base>/`` with a collision-based version suffix (see
    ``_resolve_out_base``). Pass flat=True for the legacy behavior of writing
    directly into ``outdir`` with no subfolder and no version.

    Returns the list of written file paths.
    """
    segments = load_json(segments_path)
    zh_raw = load_json(zh_path)
    zh = {int(k): v for k, v in zh_raw.items()}

    outputs = build_outputs(segments, zh, gap=gap, min_dur=min_dur,
                            offset=offset, tail=tail)
    out_dir, out_base = _resolve_out_base(outdir, base, flat)
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for suffix, content in outputs.items():
        path = os.path.join(out_dir, out_base + suffix)
        write_text(path, content)
        written.append(path)
    # Sidecar: persist display-window options so `verify` (Spec 18 presentation
    # lane) can auto-check that the perceived window wasn't over-tightened.
    save_json(os.path.join(out_dir, out_base + ".generate_opts.json"),
              {"gap": gap, "min_dur": min_dur, "offset": offset, "tail": tail})
    if prune_old:
        _prune_old_versions(out_dir, base)
    progress(
        f"[generate] bilingual/zh/en/txt written for base={base!r} "
        f"-> {out_dir} ({len(segments)} segments, gap={gap}, min_dur={min_dur}, "
        f"offset={offset}, tail={tail})"
    )
    return written
