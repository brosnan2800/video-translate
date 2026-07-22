# ADR-006 — Language auto-detection by default

Date: 2026-07-19 · Status: accepted

## Context
V1 hardcoded `--lang en`. Any non-English video was mis-segmented unless the user
remembered `--lang`. The user called this out as a usability bug.

## Decision
`--lang` defaults to `None` = Whisper auto-detect (`model.transcribe(...,
language=None)`). The literal `"auto"` normalises to `None`. Users can still
force a language with `--lang en`.

## Rationale
Whisper natively detects the source language when `language=None`. Auto-detect
is the right default for a tool that handles arbitrary videos. Forcing `en` was a
V1 shortcut that hurt non-English content.

## Consequences / risks
- Whisper may mis-detect on short or noisy audio; `--lang` overrides.
- Golden regression: the golden video is English; tests that need stable
  transcription force `lang="en"` (monkeypatch) rather than relying on
  detection. The `@slow` e2e runs with default `lang=None` to exercise detection.
