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
from .artifacts import workdir
from .srt_utils import block, srt_time
from .text_utils import to_single_line

OUTPUT_SUFFIXES = (".bilingual.srt", ".zh.srt", ".en.srt", ".txt")


# ------------------------- Spec 26: display-layer merge -------------------------
#
# Display-layer short-cue merge. When enabled, adjacent *short* cues whose
# display gap is small are joined into ONE display cue: zh/en lines are
# concatenated and the window spans the whole group. This is a PURE
# presentation-layer transform — segments/words/start/end are never rewritten
# (same class as the existing --tail/--min-dur/--gap/--offset passes, ADR-009/
# 012). It only re-lays-out the SRT; the acoustic timeline is untouched.
DEFAULT_DM_GAP = 0.8          # s, max display gap between merged cues
DEFAULT_DM_MAX_DUR = 6.0      # s, joined cue window upper bound
DEFAULT_DM_MAX_CHARS = 84     # en char budget (2 lines x 42)
DEFAULT_DM_MAX_ZH = 40         # zh char budget (2 lines x 20)
DEFAULT_DM_SHORT_DUR = 2.0    # s, a cue is "short" only if dur <= this
DEFAULT_DM_SHORT_WORDS = 8     # ... and word count <= this
_DM_EN_WIDTH = 42             # en wrap width per line
_DM_ZH_WIDTH = 20             # zh wrap width per line
_DM_MAX_LINES = 2             # max lines per language


def _resolve_bounds(
    segments: list[dict[str, Any]], *, offset: float = 0.0, tail: float = 0.0,
) -> list[list[float]]:
    """Pass 1+2: word-level window per cue, then display drift shift + tail."""
    bounds: list[list[float]] = []
    for s in segments:
        words = s.get("words")
        if words:
            st, en = words[0]["start"], words[-1]["end"]
        else:
            st, en = s["start"], s["end"]
        if offset:
            st = max(0.0, st + offset)
            en = max(st, en + offset)
        if tail and tail > 0:
            en += tail
        bounds.append([st, en])
    return bounds


def _wrap_en(text: str, width: int = _DM_EN_WIDTH,
             max_lines: int = _DM_MAX_LINES) -> list[str] | None:
    """Wrap English by words to <=max_lines lines of <=width chars. None if it
    would overflow (caller then refuses to merge)."""
    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        return None
    return lines


def _wrap_zh(text: str, width: int = _DM_ZH_WIDTH,
             max_lines: int = _DM_MAX_LINES) -> list[str] | None:
    """Wrap Chinese by characters to <=max_lines lines of <=width chars. None if
    overflow."""
    lines: list[str] = []
    cur = ""
    for ch in text:
        if len(cur) + 1 > width:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        return None
    return lines


def merge_display_cues(
    bounds: list[list[float]],
    segments: list[dict[str, Any]],
    zh: dict[int, str],
    *,
    gap: float = DEFAULT_DM_GAP,
    max_dur: float = DEFAULT_DM_MAX_DUR,
    max_chars: int = DEFAULT_DM_MAX_CHARS,
    max_zh: int = DEFAULT_DM_MAX_ZH,
    short_dur: float = DEFAULT_DM_SHORT_DUR,
    short_words: int = DEFAULT_DM_SHORT_WORDS,
) -> tuple[list[list[int]], list[dict[str, Any]]]:
    """Greedy group of adjacent cues into display cues (Spec 26).

    Cue ``i`` joins the current group iff ALL hold:
      * the display gap from the group's last end to cue i's start <= ``gap``
      * cue i is "short" (dur <= short_dur AND word count <= short_words)
      * joined window span <= max_dur
      * joined en text wraps to <= max_lines (budget max_chars)
      * joined zh text wraps to <= max_lines (budget max_zh)
    A long cue (or a cue after a large gap) starts its own group; groups are
    never split. Returns (groups, rejected) where ``rejected`` is the audit
    trail of adjacent pairs that could NOT merge (with the reason).
    """
    n = len(bounds)
    groups: list[list[int]] = []
    rejected: list[dict[str, Any]] = []
    group_mergeable: list[bool] = []
    for i in range(n):
        st, en = bounds[i]
        dur = en - st
        nwords = len(segments[i].get("words") or [])
        is_short = (dur <= short_dur) and (nwords <= short_words if nwords else True)
        if groups and group_mergeable[-1] and is_short:
            prev = groups[-1]
            g_start = bounds[prev[0]][0]
            real_gap = st - bounds[prev[-1]][1]
            why = None
            if real_gap > gap:
                why = f"gap={real_gap:.2f}>{gap}"
            else:
                joined_en = " ".join(
                    (segments[j].get("text") or "").strip() for j in prev + [i])
                joined_zh = "".join(
                    (zh.get(j) or "").strip() for j in prev + [i])
                if _wrap_en(joined_en) is None:
                    why = "en-overflow"
                elif _wrap_zh(joined_zh) is None:
                    why = "zh-overflow"
                elif (bounds[i][1] - g_start) > max_dur:
                    why = "dur-overflow"
            if why is None:
                prev.append(i)
                continue
            rejected.append({"between": [prev[-1], i],
                             "right_start": round(st, 3),
                             "right_end": round(en, 3),
                             "reason": why})
        groups.append([i])
        group_mergeable.append(is_short)
    return groups, rejected


def build_outputs(
    segments: list[dict[str, Any]], zh: dict[int, str], *,
    gap: float = 0.0, min_dur: float = 0.0,
    offset: float = 0.0, tail: float = 0.0,
    display_merge: bool = False,
    dm_gap: float = DEFAULT_DM_GAP,
    dm_max_dur: float = DEFAULT_DM_MAX_DUR,
    dm_max_chars: int = DEFAULT_DM_MAX_CHARS,
    dm_max_zh: int = DEFAULT_DM_MAX_ZH,
    dm_short_dur: float = DEFAULT_DM_SHORT_DUR,
    dm_short_words: int = DEFAULT_DM_SHORT_WORDS,
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

    V9 (Spec 26): when ``display_merge`` is on, adjacent short cues with a small
    display gap are joined into one display cue (zh/en concatenated, window
    spanning the group). Pure presentation layer — segments/words are untouched.
    Library default OFF so golden output stays byte-exact.
    """
    # pass 1+2: word-level window per cue, then display drift shift + tail
    bounds = _resolve_bounds(segments, offset=offset, tail=tail)

    # pass 5 (Spec 26): display-layer merge of adjacent short cues. Runs BEFORE
    # min-dur/gap clamp so those passes re-apply on the merged groups.
    if display_merge:
        groups, _rej = merge_display_cues(
            bounds, segments, zh, gap=dm_gap, max_dur=dm_max_dur,
            max_chars=dm_max_chars, max_zh=dm_max_zh,
            short_dur=dm_short_dur, short_words=dm_short_words)
    else:
        groups, _rej = [[i] for i in range(len(bounds))], []

    # collapse groups into merged windows + joined text
    merged_bounds: list[list[float]] = []
    merged_texts: list[tuple[str, str]] = []
    for grp in groups:
        merged_bounds.append([bounds[grp[0]][0], bounds[grp[-1]][1]])
        # ADR-040 边界清洗：segments_en.json / zh_segments.json 可被 Agent / 人工
        # 直接编辑，必须在此再兜一次单行化（多行 cue 只能来自 block 的 lines）。
        en = " ".join(to_single_line(segments[j].get("text") or "") for j in grp)
        cn = "".join(to_single_line(zh.get(j) or "") for j in grp)
        merged_texts.append((cn, en))

    # pass 3: min-dur display extension (soft target; start never moves)
    if min_dur and min_dur > 0:
        for k in range(len(merged_bounds)):
            st, en = merged_bounds[k]
            if en - st < min_dur:
                merged_bounds[k] = [st, st + min_dur]
    # pass 4: --gap clamp (hard constraint, wins over tail/min-dur extension).
    # Also runs whenever tail/offset are active, because those are the passes
    # that can manufacture an overlap in the first place.
    if (gap and gap > 0) or (tail and tail > 0) or offset:
        gap = max(gap or 0.0, 0.0)
        for k in range(len(merged_bounds)):
            st, en = merged_bounds[k]
            nxt = merged_bounds[k + 1][0] if k + 1 < len(merged_bounds) else None
            if nxt is not None:
                en = min(en, nxt - gap)
            if en < st:
                en = st
            merged_bounds[k] = [st, en]

    bi, zhl, enl, txt = [], [], [], []
    for k, grp in enumerate(groups):
        st, en = merged_bounds[k]
        cn, en_t = merged_texts[k]
        if display_merge:
            cn_lines = _wrap_zh(cn) or ([cn] if cn else [])
            en_lines = _wrap_en(en_t) or ([en_t] if en_t else [])
        else:
            cn_lines = [cn] if cn else []
            en_lines = [en_t] if en_t else []
        both = [l for l in cn_lines + en_lines if l]
        bi.append(block(k + 1, st, en, both))
        if cn:
            zhl.append(block(k + 1, st, en, cn_lines))
        if en_t:
            enl.append(block(k + 1, st, en, en_lines))
        txt.append(f"[{srt_time(st)} -> {srt_time(en)}]\n中文: {cn}\n英文: {en_t}\n")
    return {
        ".bilingual.srt": "\n".join(bi).rstrip() + "\n",
        ".zh.srt": "\n".join(zhl).rstrip() + "\n",
        ".en.srt": "\n".join(enl).rstrip() + "\n",
        ".txt": "\n".join(txt).rstrip() + "\n",
    }


def _resolve_out_base(outdir: str, base: str, flat: bool, style: str | None = None) -> tuple[str, str]:
    """Resolve the actual (directory, file-base) for the final outputs.

    Default mode (flat=False): the four outputs are written into a per-video
    subfolder ``<outdir>/<base>/`` with a collision-based version suffix. The
    first run writes ``<base>.{suffix}``; if that already exists, outputs bump
    to ``<base>_v1.{suffix}``, ``_v2``, ... so re-runs never overwrite and
    video editors (e.g. Jianying) treat each as a fresh import — no stale cache.

    ``style`` (T3 / ADR-027): when set, the output stem becomes ``<base>.<style>``
    so multiple style tracks (film / literal / bilingual_study) coexist in the
    same subfolder without colliding on the plain stem.

    Legacy mode (flat=True): write ``<base>{suffix}`` directly into ``outdir``
    with no subfolder and no version suffix (deterministic, for tests/scripts).
    """
    out_base = f"{base}.{style}" if style else base
    if flat:
        return outdir, out_base
    sub = workdir(outdir, base)
    pat = re.compile(re.escape(out_base) + r"(?:_v(\d+))?\.bilingual\.srt$")
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
    return sub, out_base + ver


def _prune_old_versions(out_dir: str, base: str) -> None:
    """Keep only the 2 newest versioned output sets inside ``out_dir``.

    A "set" is the four files sharing one stem: ``<base>`` (plain) or
    ``<base>_vN``. The plain set counts as the oldest. Everything except the
    two most-recently-modified stems is removed. ``base`` here already includes
    any ``<style>`` suffix (see ``_resolve_out_base``).
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
    style: str | None = None,
    display_merge: bool = False,
    dm_gap: float = DEFAULT_DM_GAP,
    dm_max_dur: float = DEFAULT_DM_MAX_DUR,
    dm_max_chars: int = DEFAULT_DM_MAX_CHARS,
    dm_max_zh: int = DEFAULT_DM_MAX_ZH,
    dm_short_dur: float = DEFAULT_DM_SHORT_DUR,
    dm_short_words: int = DEFAULT_DM_SHORT_WORDS,
    progress=print,
) -> list[str]:
    """Read segments + zh JSON, write the four outputs.

    By default (flat=False) the four outputs are written into a per-video
    subfolder ``<outdir>/<base>/`` with a collision-based version suffix (see
    ``_resolve_out_base``). Pass flat=True for the legacy behavior of writing
    directly into ``outdir`` with no subfolder and no version.

    ``style`` (T3 / ADR-027): when set, output filenames gain a ``<base>.<style>``
    stem (e.g. ``clip.literal.bilingual.srt``) so multiple style tracks coexist.

    Returns the list of written file paths.
    """
    segments = load_json(segments_path)
    zh_raw = load_json(zh_path)
    zh = {int(k): v for k, v in zh_raw.items()}

    outputs = build_outputs(
        segments, zh, gap=gap, min_dur=min_dur, offset=offset, tail=tail,
        display_merge=display_merge, dm_gap=dm_gap, dm_max_dur=dm_max_dur,
        dm_max_chars=dm_max_chars, dm_max_zh=dm_max_zh,
        dm_short_dur=dm_short_dur, dm_short_words=dm_short_words)
    out_dir, out_base = _resolve_out_base(outdir, base, flat, style=style)
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    for suffix, content in outputs.items():
        path = os.path.join(out_dir, out_base + suffix)
        write_text(path, content)
        written.append(path)
    # Sidecar: persist display-window options so `verify` (Spec 18 presentation
    # lane) can auto-check that the perceived window wasn't over-tightened.
    save_json(os.path.join(out_dir, out_base + ".generate_opts.json"),
              {"gap": gap, "min_dur": min_dur, "offset": offset, "tail": tail,
               "style": style,
               "display_merge": display_merge, "dm_gap": dm_gap,
               "dm_max_dur": dm_max_dur, "dm_max_chars": dm_max_chars,
               "dm_max_zh": dm_max_zh, "dm_short_dur": dm_short_dur,
               "dm_short_words": dm_short_words})
    # Spec 26: when display-merge is on, write an audit sidecar recording which
    # source indices each display cue covers (and which adjacent pairs were
    # refused, with the reason). Pure observability — no gate.
    if display_merge:
        bounds = _resolve_bounds(segments, offset=offset, tail=tail)
        groups, rejected = merge_display_cues(
            bounds, segments, zh, gap=dm_gap, max_dur=dm_max_dur,
            max_chars=dm_max_chars, max_zh=dm_max_zh,
            short_dur=dm_short_dur, short_words=dm_short_words)
        merged_bounds: list[list[float]] = []
        for grp in groups:
            merged_bounds.append([bounds[grp[0]][0], bounds[grp[-1]][1]])
        if min_dur and min_dur > 0:
            for bi_i in range(len(merged_bounds)):
                st, en = merged_bounds[bi_i]
                if en - st < min_dur:
                    merged_bounds[bi_i] = [st, st + min_dur]
        if (gap and gap > 0) or (tail and tail > 0) or offset:
            g = max(gap or 0.0, 0.0)
            for bi_i in range(len(merged_bounds)):
                st, en = merged_bounds[bi_i]
                nxt = merged_bounds[bi_i + 1][0] if bi_i + 1 < len(merged_bounds) else None
                if nxt is not None:
                    en = min(en, nxt - g)
                if en < st:
                    en = st
                merged_bounds[bi_i] = [st, en]
        audit = {
            "params": {"display_merge": True, "gap": dm_gap,
                       "max_dur": dm_max_dur, "max_chars": dm_max_chars,
                       "max_zh": dm_max_zh, "short_dur": dm_short_dur,
                       "short_words": dm_short_words, "min_dur": min_dur,
                       "tail": tail, "offset": offset},
            "total_segments": len(segments),
            "display_cues": len(groups),
            "merged_groups": sum(1 for g in groups if len(g) > 1),
            "groups": [
                {"cue": k + 1, "source_indices": grp,
                 "window": [round(merged_bounds[k][0], 3),
                            round(merged_bounds[k][1], 3)],
                 "zh": "".join((zh.get(j) or "").strip() for j in grp),
                 "en": " ".join(
                     (segments[j].get("text") or "").strip() for j in grp)}
                for k, grp in enumerate(groups) if len(grp) > 1
            ],
            "rejected": rejected,
        }
        save_json(os.path.join(out_dir, out_base + ".display_merge.json"), audit)
    if prune_old:
        _prune_old_versions(out_dir, out_base)
    progress(
        f"[generate] bilingual/zh/en/txt written for base={base!r} "
        f"-> {out_dir} ({len(segments)} segments, gap={gap}, min_dur={min_dur}, "
        f"offset={offset}, tail={tail}"
        f"{', display_merge=on' if display_merge else ''})"
    )
    return written
