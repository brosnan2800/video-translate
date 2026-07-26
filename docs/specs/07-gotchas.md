# Spec 07 — Gotchas & hard-won lessons

Invariants that are easy to break and must stay covered by tests / review.

## 1. Timestamps are acoustic facts
Never recompute or "smooth" timestamps in translate/generate. Translation only
rewrites text; the cue window stays exactly as transcribed. This is THE reason
subtitles track the audio. (Regression protected by the golden test.)

## 2. HTTP proxy only — never SOCKS
`huggingface_hub` uses httpx, which chokes on a SOCKS proxy. `setup_http_proxy`
forces the four HTTP proxy env vars (`http_proxy/https_proxy/HTTP_PROXY/
HTTPS_PROXY`), **pops** `all_proxy/ALL_PROXY`, and raises `ValueError` on a
`socks5://`/`socks4://` URL → CLI exit 4. (ADR-003)

## 3. Model runs on CPU / int8
CTranslate2 (faster-whisper's backend) has no AMD-ROCm or Apple-Metal support on
this machine class. `device=cpu`, `compute_type=int8` are forced, not
configurable. An NVIDIA CUDA box could change this — that's a future ADR. (ADR-001)

## 4. Resume is real, not cosmetic
`transcribe_video` skips any chunk whose `chunk_N.json` already exists (the
original `transcribe_chunked.py` re-ran every chunk — fixed here). All JSON
writes are atomic (temp + `os.replace`) so a SIGKILL mid-write can't corrupt a
checkpoint. (ADR-002)

## 5. zh index is positional & 0-based in the map
`zh_segments.json` keys are stringified 0-based indices; segment `i` (1-based)
reads `zh[i-1]`. Off-by-one here silently misaligns every subtitle.

## 6. Byte-exact output
`.rstrip()` + single trailing `\n`, `ensure_ascii=False`, LF newlines. Any change
to spacing, ordering, or the empty-line-drop rule breaks the golden test.

## 7. Lazy heavy imports
`faster_whisper` / `deep_translator` are imported **inside** functions, so unit
tests and `doctor` run without the multi-GB deps installed.

## 8. Millisecond carry
`srt_time` rolls `ms == 1000` into the next second. Regression-tested edge case.

## 9. Merge preserves timestamps (V2)
`merge_segments` takes the group's first `start` / last `end` verbatim — no
arithmetic. Enforced by `test_all_boundary_values_come_from_input` and the merge
golden. `max_chars` is NOT a merge gate (it's a split constraint; V2 can't split
without word timestamps — see ADR-004).

## 10. Agent engine adds no LLM client dependency (V2)
`--engine agent` only writes a task file; the calling agent translates. Never
`pip install openai`/`httpx`/`anthropic` into this project. Google stays as the
`--engine google` headless fallback. (ADR-005)

## 11. `--lang` auto-detect may misjudge (V2)
Default `lang=None` lets Whisper auto-detect. On short/noisy audio it can be
wrong; `--lang en` overrides. Golden tests force `lang="en"` (monkeypatch) so
regression doesn't depend on detection. (ADR-006)

## 12. Proxy auto-detect returns None, not raise (V2 deviation)
`detect_proxy` returns `None` (direct) when no proxy source is found, rather than
raising. Direct egress often works; raising would break local-only transcribe.
`--no-proxy` forces direct. SOCKS still rejected (ADR-003/007).

## 13. Exit 6 is not an error (V2)
`run --engine agent` returns 6 after emitting the translation task. Do not retry
`run` on exit 6 — translate the task and run `generate`. (Spec 05/11)

## 14. Golden zh must be deterministic (V2)
Golden `zh_segments.json` is retranslated with Google (pinned
`deep-translator==1.9.1`), NOT the agent engine — LLM output drifts and would
break the byte-exact regression. Agent translation is for production only.
(决裁定稿 §1)

## 15. `words` must survive merge (V3)
`_emit` must carry the `words` list through merge; the split pass consumes them.
If merge drops `words`, the word-boundary tightening (Spec 15) and word-level
split (Spec 13) silently no-op and fall back to segment-level timing — defeating
V3. Enforced by `test_emit_carries_words`. (ADR-008/009)

## 16. Split changes cue count → zh must be retranslated (V3)
`split_long_cues` raises the number of cues vs V2. A `zh_segments.json` built
against V2 `segments_en.json` is therefore **invalid** for V3. Always
retranslate from the new `segments_en.json`. Golden regen archives V2 as `.v2`.
(决裁定稿 §2 / Spec 13)
