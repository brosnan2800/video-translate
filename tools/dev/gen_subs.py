#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为视频生成剪映可用的中英双语字幕。
- 转写: faster-whisper large-v3 (英文)
- 主翻译: Google 翻译 (经 7890 HTTP 代理)
- 兜底翻译: 当前 agent (本脚本仅留占位并生成待翻译队列, 由 agent 后续补齐)
- 输出: 双语 SRT / 纯中文 SRT / 纯英文 SRT / TXT 全文 / segments.json
"""
import os, sys, time, json
from pathlib import Path

VIDEO = "/Users/yanglei/Movies/翻译/steveharvy-the apollo story.mp4"
OUTDIR = Path("/Users/yanglei/Movies/翻译/subtitles")
OUTDIR.mkdir(parents=True, exist_ok=True)

# 仅使用 HTTP 代理 (7890 是 HTTP 代理, 不要用 socks, 否则 huggingface_hub 的 httpx 会报缺 socksio)
PROXY = "http://127.0.0.1:7890"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ[k] = PROXY
os.environ.pop("all_proxy", None)
os.environ.pop("ALL_PROXY", None)

AGENT_SENTINEL = "__AGENT_FALLBACK__"

# ---------- 时间格式 ----------
def srt_ts(seconds: float) -> str:
    if seconds is None or seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

# ---------- 翻译 (Google 主, agent 兜底) ----------
def translate(text: str):
    from deep_translator import GoogleTranslator
    text = (text or "").strip()
    if not text:
        return ""
    last_err = None
    for attempt in range(3):
        try:
            out = GoogleTranslator(source="en", target="zh-CN").translate(text)
            if out and out.strip():
                return out.strip()
        except Exception as e:
            last_err = e
            time.sleep(0.6 * (attempt + 1))
    # Google 连续失败 -> 交给 agent 兜底
    print(f"      [warn] Google 失败, 转交 agent 兜底: {text[:60]}", flush=True)
    return AGENT_SENTINEL

# ---------- 主流程 ----------
def main():
    from faster_whisper import WhisperModel

    print(f"[1/4] 加载模型 large-v3 (经代理 {PROXY}) ...", flush=True)
    t0 = time.time()
    model = WhisperModel(
        model_size_or_path="large-v3",
        device="cpu",
        compute_type="int8",
        cpu_threads=max(1, os.cpu_count() or 4),
    )
    print(f"      模型加载耗时 {time.time()-t0:.1f}s", flush=True)

    print("[2/4] 转写中 (英文, VAD 过滤) ...", flush=True)
    t0 = time.time()
    segments, info = model.transcribe(
        VIDEO,
        language="en",
        task="transcribe",
        beam_size=5,
        best_of=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        condition_on_previous_text=True,
    )
    print(f"      语言={info.language} 概率={info.language_probability:.2f} 耗时={time.time()-t0:.1f}s", flush=True)

    segs = []
    for s in segments:
        segs.append({"start": s.start, "end": s.end, "text": s.text.strip(), "zh": ""})
    print(f"      共 {len(segs)} 段", flush=True)

    print("[3/4] Google 翻译 -> 中文 ...", flush=True)
    t0 = time.time()
    pending = []  # 需 agent 兜底
    for i, s in enumerate(segs):
        s["zh"] = translate(s["text"])
        if s["zh"] == AGENT_SENTINEL:
            pending.append({"idx": i, "en": s["text"]})
        if (i + 1) % 20 == 0 or (i + 1) == len(segs):
            print(f"      已翻译 {i+1}/{len(segs)} 段", flush=True)
    print(f"      翻译耗时 {time.time()-t0:.1f}s; 需 agent 兜底 {len(pending)} 段", flush=True)

    # ---------- 写出文件 ----------
    print("[4/4] 生成字幕文件 ...", flush=True)
    base = OUTDIR / "apollo_story"

    with open(f"{base}.bilingual.srt", "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            zh = s["zh"] if s["zh"] != AGENT_SENTINEL else s["text"]
            f.write(f"{i}\n{srt_ts(s['start'])} --> {srt_ts(s['end'])}\n{zh}\n{s['text']}\n\n")

    with open(f"{base}.zh.srt", "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            zh = s["zh"] if s["zh"] != AGENT_SENTINEL else s["text"]
            f.write(f"{i}\n{srt_ts(s['start'])} --> {srt_ts(s['end'])}\n{zh}\n\n")

    with open(f"{base}.en.srt", "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            f.write(f"{i}\n{srt_ts(s['start'])} --> {srt_ts(s['end'])}\n{s['text']}\n\n")

    with open(f"{base}.txt", "w", encoding="utf-8") as f:
        for s in segs:
            zh = s["zh"] if s["zh"] != AGENT_SENTINEL else s["text"]
            f.write(f"[{srt_ts(s['start'])} -> {srt_ts(s['end'])}]\n{zh}\n{s['text']}\n\n")

    with open(f"{base}.segments.json", "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False, indent=2)

    if pending:
        with open(f"{base}.agent_pending.json", "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
        print(f"\n[!] 有 {len(pending)} 段需 agent 兜底, 见 apollo_story.agent_pending.json", flush=True)
    else:
        print("\n[OK] 全部段落已由 Google 翻译完成, 无需 agent 兜底。", flush=True)

    print(f"\n完成! 输出目录: {OUTDIR}", flush=True)
    for p in sorted(OUTDIR.glob("apollo_story*")):
        print(f"  - {p.name}  ({p.stat().st_size//1024} KB)", flush=True)

if __name__ == "__main__":
    main()
