# ADR-015 — Per-chunk adaptive VAD routing

Date: 2026-08-19
Status: Accepted
Supersedes: ADR-011 (global VAD opt-in) for *routing granularity* only; VAD
remains opt-in (default bare) per ADR-011.

## Context

`doctor` produces a single `recommend_vad(profile)` verdict (`--vad`, bare, or
`--vad --vad-threshold 0.1`) for the **whole video**. That verdict is applied
globally: `transcribe_video` uses one `use_vad` flag for every chunk
(`src/video_translate/cli.py:182-186`, `transcribe.py:315-318`).

Real videos are mixed. `emily-blunt.mp4` is ~60% clean interview + 40% laughter
/ cheer / sung audio. The global verdict cannot be right for both halves:

- A global `--vad` anchors boundaries to silence but **silently drops speech
  that sits under continuous laughing / cheering / music** (Silero VAD treats the
  non-speech vocalization as the segment, ejecting the masked speech). This is
  exactly the "整段留白里其实有说话" failure the user hit at 4:06, 4:24, 4:59,
  6:15, 15:29.
- A global bare run avoids that drop but loses the silence-anchoring / drift
  cure that VAD gives clean chunks.

`resegment --no-vad` (bare) on those five windows recovered all of them,
confirming the root cause is **VAD applied uniformly to noisy chunks**.

## Decision

Replace the *global* VAD decision inside `transcribe_video` with a **per-chunk**
decision when `adaptive_vad=True` (new `--adaptive-vad` flag, default OFF to keep
ADR-011 opt-in and preserve all existing golden caches/behaviour):

For each chunk, after the wav is extracted, compute a chunk-local audio profile
(`analyze_audio`) and route:

```
def route_vad_chunk(profile, chunk_dur) -> bool:
    if not profile.ok:                 return False   # safe default: bare
    # low level -> tuned VAD (recall for quiet speech)   [ADR-011 low-level branch]
    if mean_vol < -20 or max_vol < -5: return True
    # otherwise: clean (clear pauses) anchors to silence; continuous noise -> bare
    silence_frac = silence_covered / chunk_dur
    return silence_frac >= CLEAN_SILENCE_FRACTION   # 0.10
```

| Chunk type | silence fraction | Decision | Rationale |
|---|---|---|---|
| quiet / low-level | any | **VAD on** | ADR-011 low-level branch (`loudnorm` + threshold 0.1) |
| clean w/ clear pauses | ≥ 0.10 | **VAD on** | anchor segment edges to real silence → kills drift |
| continuous noise (laugh/cheer/music) | < 0.10 | **bare** | VAD would eject masked speech; bare + `no_speech_threshold=0.0` keeps it |

This is **preventive** — it fixes the miss at the transcription source, for every
chunk, automatically, without per-video manual tuning.

## Consequences

- `transcribe_video(..., adaptive_vad=False)` is byte-for-byte identical to today
  (default OFF). No golden regression.
- `adaptive_vad=True` adds one `ffmpeg` volumedetect+silencedetect pass per chunk
  (cheap; the wav is already on disk). Chunk cost rises modestly.
- The chunk fingerprint gains `"adaptive": true` **only when on** (omitted when
  off) so existing caches stay valid and an adaptive run is cleanly separated.
- Per-chunk routing is deterministic from the audio, so resume / cache reuse is
  safe.
- Does **not** replace the recovery net (ADR-016). Adaptive routing prevents most
  misses; the recovery net catches the residual (e.g. speech fully buried) and the
  `verify` lane alarms any still-missed region.

## Rejected alternatives

- *Always bare globally*: regresses drift on clean chunks; not general.
- *Always VAD globally*: this is the bug we are fixing.
- *Source separation (Demucs) pre-stage*: the true root fix for fully-buried
  speech, but a heavy new dependency; deferred. Adaptive routing + recovery net
  covers the common masked-speech case with zero new deps.
