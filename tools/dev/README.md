# tools/dev — historical originals (reference only)

These are the original throwaway scripts the `video_translate` package was
migrated from. They are kept for provenance/diffing and are **not** part of the
supported CLI. Do not import them at runtime.

| Script                | Migrated into                          |
|-----------------------|----------------------------------------|
| `step1_chunked.py`    | `src/video_translate/transcribe.py`    |
| `step1_transcribe.py` | `src/video_translate/transcribe.py`    |
| `translate.py`        | `src/video_translate/translate.py`     |
| `gen_subs.py`         | `src/video_translate/generate.py`      |
| `generate_srt.py`     | `src/video_translate/srt_utils.py` + `generate.py` |

The migration is faithful: the golden regression in
`tests/test_generate_golden.py` proves the new code reproduces the validated
output byte-for-byte. Notable improvement over the originals: **true resumable
transcription** (skip completed `chunk_N.json`), which the originals lacked.
