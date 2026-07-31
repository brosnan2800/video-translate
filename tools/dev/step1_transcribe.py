#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step1: faster-whisper large-v3 英文转写, 先把英文段落存盘。"""
import os, time, json
from pathlib import Path

VIDEO = os.path.expanduser("~/Movies/翻译/steveharvy-the apollo story.mp4")
OUTDIR = Path(os.path.expanduser("~/Movies/翻译/subtitles"))
OUTDIR.mkdir(parents=True, exist_ok=True)

PROXY = "http://127.0.0.1:7890"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ[k] = PROXY
os.environ.pop("all_proxy", None); os.environ.pop("ALL_PROXY", None)

def srt_ts(s):
    if s is None or s < 0: s = 0.0
    ms = int(round(s * 1000)); h, ms = divmod(ms, 3600_000); m, ms = divmod(ms, 60_000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def main():
    from faster_whisper import WhisperModel
    print("[1] 加载模型 large-v3 ...", flush=True)
    t0 = time.time()
    model = WhisperModel("large-v3", device="cpu", compute_type="int8", cpu_threads=max(1, os.cpu_count() or 4))
    print(f"    模型加载 {time.time()-t0:.1f}s", flush=True)

    print("[2] 转写 (英文, VAD) ...", flush=True)
    t0 = time.time()
    segments, info = model.transcribe(VIDEO, language="en", task="transcribe", beam_size=5, best_of=5,
        vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        condition_on_previous_text=True)
    segs = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    print(f"    语言={info.language} 概率={info.language_probability:.2f} 段数={len(segs)} 耗时={time.time()-t0:.1f}s", flush=True)

    # 立即存盘英文, 防止后续步骤崩溃丢数据
    base = OUTDIR / "apollo_story"
    with open(f"{base}.en.srt", "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            f.write(f"{i}\n{srt_ts(s['start'])} --> {srt_ts(s['end'])}\n{s['text']}\n\n")
    with open(f"{base}.segments_en.json", "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False, indent=2)
    print(f"[OK] 已保存英文 SRT 与 segments_en.json ({len(segs)} 段)", flush=True)

if __name__ == "__main__":
    main()
