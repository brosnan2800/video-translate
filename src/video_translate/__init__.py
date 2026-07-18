"""video-translate: video → bilingual (zh/en) subtitle pipeline.

Three-stage pipeline:
    transcribe (faster-whisper, chunked+resumable)
        -> segments_en.json  [{start, end, text}]
    translate  (Google Translate via HTTP proxy, incremental)
        -> zh_segments.json  {index: zh_text}
    generate   (pure function)
        -> {base}.bilingual.srt / .zh.srt / .en.srt / .txt

Timestamps come straight from the acoustic model and are NEVER recomputed;
translation only touches text, so audio/subtitle alignment is preserved.
"""

__version__ = "1.0.0"
