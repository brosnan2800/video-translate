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
