# ADR-001 — Force CPU + int8 for transcription

- **Status**: Superseded（硬编码部分被 ADR-014 撤销；CPU/int8 仍为无 CUDA 机器的默认）
- **Date**: 2026-07-16
- **Superseded by**: ADR-014（`device`/`compute_type` 改为 `auto` 自动探测 + 可显式覆盖）
- **Context**: The dev machine is a Mac (Apple Silicon / AMD Radeon) plus a
  Windows box with an 8GB RTX 3070 Ti. faster-whisper runs on CTranslate2, which
  provides CUDA (NVIDIA) and CPU backends only — no ROCm (AMD) and no Metal
  (Apple) support. Attempting `device="cuda"` on the Mac fails; on the 8GB card
  large-v3 in fp16 risks OOM.

## Decision
Force `device="cpu"`, `compute_type="int8"` for all transcription. These two
values are **not** exposed in config — they are hard-coded constants in
`transcribe.py`.

## Consequences
- **Positive**: Runs anywhere without a GPU driver; deterministic; low memory;
  `int8` is fast enough for offline batch subtitling; identical output across
  machines (supports the golden regression).
- **Negative**: Slower than GPU. Mitigated by chunked, resumable transcription
  (ADR-002) so long videos can be processed incrementally.
- **Revisit when**: running on a dedicated NVIDIA box. A future ADR could add an
  opt-in `device=cuda, compute_type=float16` path guarded by `nvidia-smi`.
