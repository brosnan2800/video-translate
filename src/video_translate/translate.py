"""Translation of English segments to Chinese.

V1: incremental Google translation (via HTTP proxy), resumable, with a pending
file for segments Google cannot translate.

V2: ``--engine agent`` (default) does NOT call any LLM API. It emits a
"translation task" file (batched, with sliding-window context + persona) for the
calling agent (WorkBuddy/Claude Code/...) to fill using its own LLM. Google
remains as the ``--engine google`` headless fallback. No LLM client dependency is
added — the agent IS the engine.

deep_translator is imported lazily so unit tests don't require it.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .config import DEFAULT_PERSONA
from .io_utils import load_json, load_json_default, save_json
from .proxy import DEFAULT_PROXY, setup_http_proxy

CHECKPOINT_EVERY = 10
MAX_RETRIES = 3

# --- agent engine defaults -------------------------------------------------
DEFAULT_BATCH_SIZE = 8
DEFAULT_CONTEXT_WINDOW = 2  # segments of context before + after each batch
# V6 (B3): the sliding ±2-segment window is enough for local coherence but not
# for *global* understanding — the agent could not tell that "Withdraw, or we
# all die here" was a battlefield order, and mistranslated it. We therefore ship
# the entire transcript in the task file so the agent reads the whole scene
# before translating any batch. Capped so a 2-hour film doesn't blow the
# context window; beyond the cap the transcript is truncated with a marker.
FULL_TRANSCRIPT_MAX_CHARS = 24000

# V6 (B3): explicit rules the agent must follow. Shipped in the task file so the
# contract lives with the data, not in whatever prompt happens to invoke it.
TRANSLATION_GUIDELINES = [
    "先通读 full_transcript 建立全局理解（场景、说话人关系、剧情走向），再逐 batch 翻译。",
    "source 字段给出视频出处/背景。若是已有影视、文学或历史题材作品，专有名词、人名、"
    "称谓与经典台词一律沿用通行中文译法，不要自创。",
    "语义歧义时以全局语境判定，不要只看单句。例如军事场景中的 'terms' 是「（议和）条件」"
    "而非「术语」，'withdraw' 是「撤军」而非「退出」。",
    "context_before / context_after 仅供参考，不要翻译、不要出现在输出里。",
    "输出必须覆盖 to_translate[*].index 的每一个下标（字符串形式）。",
]


def _make_translator(src: str, tgt: str) -> Callable[[str], str]:
    """Build a translate(text)->text callable backed by GoogleTranslator."""
    from deep_translator import GoogleTranslator  # lazy

    tr = GoogleTranslator(source=src, target=tgt)

    def translate_one(text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return tr.translate(text)
            except Exception as ex:  # noqa: BLE001 — engine raises many types
                err = ex
                time.sleep(1 + attempt)
        assert err is not None
        raise err

    return translate_one


def translate_segments(
    segments_path: str,
    out_path: str,
    *,
    pending_path: str | None = None,
    proxy: str | None = DEFAULT_PROXY,
    src: str = "en",
    tgt: str = "zh-CN",
    translate_fn: Callable[[str], str] | None = None,
    progress=print,
) -> dict[str, str]:
    """Translate all segments in `segments_path`, writing `{index: zh}` to `out_path`.

    Args:
        translate_fn: injectable translator (defaults to Google); enables testing
            and agent-fallback wiring without hitting the network.

    Returns:
        The completed `{str_index: zh_text}` mapping.
    """
    if translate_fn is None:
        setup_http_proxy(proxy)
        translate_fn = _make_translator(src, tgt)

    segs: list[dict[str, Any]] = load_json(segments_path)
    n = len(segs)
    done: dict[int, str] = {
        int(k): v for k, v in load_json_default(out_path, {}).items()
    }
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        save_json(out_path, {str(k): v for k, v in done.items()}, indent=None)

    for i, s in enumerate(segs):
        if i in done:
            continue
        try:
            done[i] = translate_fn(s.get("text", ""))
        except Exception as e:  # noqa: BLE001
            progress(f"[fail] seg {i}: {str(e)[:80]}")
            pending.append({
                "index": i, "start": s.get("start"),
                "end": s.get("end"), "text": s.get("text"),
            })
        if (i + 1) % CHECKPOINT_EVERY == 0 or (i + 1) == n:
            flush()
            progress(f"[progress] {i + 1}/{n} translated={len(done)}")

    flush()
    if pending_path is not None:
        save_json(pending_path, pending, indent=2)
    progress(f"[done] translated {len(done)}/{n}, failed {len(pending)}")
    return {str(k): v for k, v in done.items()}


# --- V2: agent engine ------------------------------------------------------


def build_full_transcript(
    segs: list[dict[str, Any]],
    *,
    max_chars: int = FULL_TRANSCRIPT_MAX_CHARS,
) -> tuple[str, bool]:
    """Render every segment as ``[index] text`` lines for whole-scene context.

    Returns ``(text, truncated)``. Truncation drops whole lines from the end so
    the transcript never ends mid-sentence, and appends an explicit marker —
    a silently clipped transcript would be worse than a short one, because the
    agent would assume it saw the ending.
    """
    lines: list[str] = []
    used = 0
    truncated = False
    for i, s in enumerate(segs):
        line = f"[{i}] {(s.get('text') or '').strip()}"
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    if truncated:
        lines.append(f"...[truncated: {len(segs) - len(lines)} more segments]")
    return "\n".join(lines), truncated


def build_persona(
    persona: str,
    *,
    source: str | None = None,
    glossary: str | None = None,
) -> str:
    """Compose the effective persona: source hint + glossary + base persona."""
    parts: list[str] = []
    if source:
        parts.append(
            f"【视频出处/背景】{source}\n"
            "翻译时必须据此还原专有名词、人名、称谓与既有译法；"
            "涉及该作品已有中文版的台词，优先沿用通行译文。"
        )
    if glossary:
        parts.append(glossary)
    parts.append(persona)
    return "\n\n".join(parts)


def prepare_translate_task(
    segments_path: str,
    task_path: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    persona: str = DEFAULT_PERSONA,
    index_key: str | None = None,
    glossary: str | None = None,
    source: str | None = None,
    full_transcript: bool = True,
    max_transcript_chars: int = FULL_TRANSCRIPT_MAX_CHARS,
    progress=print,
) -> str:
    """Read segments, batch them with sliding-window context, write a translation
    task file for the calling agent to fill. Returns task_path.

    The agent reads this file, translates each ``to_translate`` item per the
    persona, and writes ``{str(index): zh}`` to ``<base>.zh_segments.json``.

    ``index_key``: if given, use ``seg[index_key]`` as the item's index (used by
    ``backfill`` where pending items carry their original zh_segments index);
    otherwise use the positional index (0-based).

    ``glossary``: optional pre-formatted glossary context string (from
    ``glossary.load_glossary``). When present it is prepended to the persona so the
    agent keeps character/proper-noun names consistent (Spec 14 / ADR-010).

    ``source`` (V6/B3): free-text provenance hint, e.g. "电影《天国王朝》——
    鲍德温四世与萨拉丁会面片段". Injected at the top of the persona so the agent
    resolves proper nouns and domain-specific senses correctly.

    ``full_transcript`` (V6/B3): ship the whole transcript alongside the batches
    so the agent has global context, not just a ±2-segment window.
    """
    segs: list[dict[str, Any]] = load_json(segments_path)
    n = len(segs)

    effective_persona = build_persona(persona, source=source, glossary=glossary)
    transcript, transcript_truncated = (
        build_full_transcript(segs, max_chars=max_transcript_chars)
        if full_transcript else ("", False)
    )

    def _idx(j: int):
        return segs[j][index_key] if index_key else j

    batches: list[dict[str, Any]] = []
    for i in range(0, n, batch_size):
        lo, hi = i, min(i + batch_size, n)
        cb = [{"index": _idx(j), "text": segs[j].get("text", "")}
              for j in range(max(0, lo - context_window), lo)]
        tt = [{"index": _idx(j), "text": segs[j].get("text", "")} for j in range(lo, hi)]
        ca = [{"index": _idx(j), "text": segs[j].get("text", "")}
              for j in range(hi, min(n, hi + context_window))]
        batches.append({
            "batch_index": i // batch_size,
            "context_before": cb,
            "to_translate": tt,
            "context_after": ca,
        })
    task = {
        "version": 2,
        "persona": effective_persona,
        "source": source,
        "glossary": glossary,
        "guidelines": TRANSLATION_GUIDELINES,
        "full_transcript": transcript,
        "full_transcript_truncated": transcript_truncated,
        "output_schema": {
            "type": "object",
            "description": (
                "Dict mapping str(index) -> Chinese translation. Keys MUST cover "
                "every index in to_translate[*].index (as strings)."
            ),
            "required_keys": "all indices in to_translate[*].index (as strings)",
        },
        "batches": batches,
    }
    save_json(task_path, task, indent=2)
    extra = ""
    if source:
        extra += f", source={source!r}"
    if glossary:
        extra += f", glossary={len(glossary)} chars"
    if transcript:
        extra += f", full_transcript={len(transcript)} chars"
        if transcript_truncated:
            extra += " (truncated)"
    progress(f"[agent-translate] task written: {task_path} "
             f"({len(batches)} batches, {n} segments){extra}")
    return task_path


def validate_zh(
    segments_path: str,
    zh_path: str,
    *,
    progress=print,
) -> tuple[bool, list[int]]:
    """Check zh_segments.json completeness: every segment index has a zh entry.

    Returns (ok, missing_indices).
    """
    segs: list[dict[str, Any]] = load_json(segments_path)
    zh_raw: dict[str, Any] = load_json_default(zh_path, {})
    zh_keys = {int(k) for k in zh_raw.keys()}
    missing = [i for i in range(len(segs)) if i not in zh_keys]
    ok = not missing
    if ok:
        progress(f"[validate] ok: {len(segs)} segments all translated")
    else:
        progress(f"[validate] missing {len(missing)} indices: {missing[:10]}...")
    return ok, missing


def merge_agent_zh(
    zh_path: str,
    agent_zh_path: str,
    *,
    progress=print,
) -> dict[str, str]:
    """Merge agent-filled zh into zh_segments.json (existing keys kept, new ones
    added/overwritten). Returns the merged dict.
    """
    existing: dict[str, Any] = load_json_default(zh_path, {})
    new: dict[str, Any] = load_json_default(agent_zh_path, {})
    existing.update({str(k): v for k, v in new.items()})
    save_json(zh_path, existing, indent=None)
    progress(f"[merge-zh] merged {len(new)} translations into {zh_path}")
    return {str(k): v for k, v in existing.items()}
