"""Incremental Google translation of English segments (via HTTP proxy).

Google Translate is the primary engine (best quality when the proxy is up).
Translation is incremental and resumable: already-translated indices are skipped,
progress is checkpointed every N segments, and any segment that fails after
retries is recorded to a pending file for the agent to backfill.

deep_translator is imported lazily so unit tests don't require it.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .io_utils import load_json, load_json_default, save_json
from .proxy import DEFAULT_PROXY, setup_http_proxy

CHECKPOINT_EVERY = 10
MAX_RETRIES = 3


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
    proxy: str = DEFAULT_PROXY,
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
