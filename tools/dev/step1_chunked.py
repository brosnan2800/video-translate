#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step1 分块转写 large-v3, 断点续传。每块独立存盘, 被杀后可重跑跳过已完成块。"""
import os, time, json, subprocess
from pathlib import Path

VIDEO = os.path.expanduser("~/Movies/翻译/steveharvy-the apollo story.mp4")
OUTDIR = Path(os.path.expanduser("~/Movies/翻译/subtitles"))
OUTDIR.mkdir(parents=True, exist_ok=True)
PROXY = "http://127.0.0.1:7890"
for k in ("http_proxy","https_proxy","HTTP_PROXY","HTTPS_PROXY"): os.environ[k]=PROXY
os.environ.pop("all_proxy",None); os.environ.pop("ALL_PROXY",None)

TOTAL = 718.0          # 视频时长(秒)
CHUNK = 280.0          # 每块时长, 保证单块转写 < 时长上限
NT = max(1, os.cpu_count() or 4)

def chunk_plan():
    starts=[]; s=0.0
    while s < TOTAL:
        starts.append(s); s += CHUNK
    return starts

def srt_ts(x):
    x=max(0.0,x); ms=int(round(x*1000)); h,ms=divmod(ms,3600_000); m,ms=divmod(ms,60_000); s,ms=divmod(ms,1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def main():
    from faster_whisper import WhisperModel
    starts = chunk_plan()
    print(f"[计划] 共 {len(starts)} 块, 块长 {CHUNK}s, 线程 {NT}", flush=True)
    model = WhisperModel("large-v3", device="cpu", compute_type="int8", cpu_threads=NT)

    done = 0
    for i, st in enumerate(starts):
        cj = OUTDIR / f"chunk_{i}.json"
        if cj.exists():
            print(f"[跳过] 块 {i} 已完成", flush=True); done += 1; continue
        dur = min(CHUNK, TOTAL - st)
        wav = f"/tmp/chunk_{i}.wav"
        t0=time.time()
        subprocess.run(["ffmpeg","-y","-ss",str(st),"-t",str(dur),"-i",VIDEO,
                        "-ar","16000","-ac","1","-c:a","pcm_s16le",wav],
                       capture_output=True)
        segs, info = model.transcribe(wav, language="en", task="transcribe", beam_size=1, best_of=1,
            vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200))
        out=[]
        for s in segs:
            out.append({"start": round(st+s.start,3), "end": round(st+s.end,3), "text": s.text.strip()})
        with open(cj,"w",encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"[完成] 块 {i} start={st:.0f}s 段数={len(out)} 耗时={time.time()-t0:.1f}s", flush=True)
        done += 1

    # 合并
    if done == len(starts):
        all_segs=[]
        for i in range(len(starts)):
            with open(OUTDIR/f"chunk_{i}.json", encoding="utf-8") as f:
                all_segs += json.load(f)
        all_segs.sort(key=lambda x:x["start"])
        base = OUTDIR/"apollo_story"
        with open(f"{base}.segments_en.json","w",encoding="utf-8") as f:
            json.dump(all_segs, f, ensure_ascii=False, indent=2)
        with open(f"{base}.en.srt","w",encoding="utf-8") as f:
            for i,s in enumerate(all_segs,1):
                f.write(f"{i}\n{srt_ts(s['start'])} --> {srt_ts(s['end'])}\n{s['text']}\n\n")
        print(f"[合并] 全部 {len(all_segs)} 段 -> apollo_story.en.srt / segments_en.json", flush=True)
    else:
        print(f"[续传] 已完成 {done}/{len(starts)} 块, 重跑本脚本可继续", flush=True)

if __name__=="__main__":
    main()
