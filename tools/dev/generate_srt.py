import json, os

BASE = os.path.expanduser("~/Movies/翻译/subtitles")
segs = json.load(open(f"{BASE}/apollo_story.segments_en.json"))
zh = {int(k): v for k, v in json.load(open(f"{BASE}/zh_segments.json")).items()}

def srt_time(t):
    t = max(0.0, float(t))
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        t += 1.0; ms = 0
    total = int(t)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def block(i, start, end, lines):
    return f"{i}\n{srt_time(start)} --> {srt_time(end)}\n" + "\n".join(lines) + "\n"

bi_lines, zh_lines, en_lines, txt_lines = [], [], [], []
for i, s in enumerate(segs, 1):
    start, end = s["start"], s["end"]
    en = (s.get("text") or "").strip()
    cn = (zh.get(i - 1) or "").strip()
    # 双语：中文在上，英文在下
    bi = [l for l in [cn, en] if l]
    bi_lines.append(block(i, start, end, bi))
    if cn:
        zh_lines.append(block(i, start, end, [cn]))
    if en:
        en_lines.append(block(i, start, end, [en]))
    ts = f"[{srt_time(start)} -> {srt_time(end)}]"
    txt_lines.append(f"{ts}\n中文: {cn}\n英文: {en}\n")

open(f"{BASE}/apollo_story.bilingual.srt", "w").write("\n".join(bi_lines).rstrip() + "\n")
open(f"{BASE}/apollo_story.zh.srt", "w").write("\n".join(zh_lines).rstrip() + "\n")
open(f"{BASE}/apollo_story.en.srt", "w").write("\n".join(en_lines).rstrip() + "\n")
open(f"{BASE}/apollo_story.txt", "w").write("\n".join(txt_lines).rstrip() + "\n")

print(f"[生成] 双语={len(bi_lines)}块 中文={len(zh_lines)}块 英文={len(en_lines)}块")
print(f"[文件] bilingual.srt / zh.srt / en.srt / txt 已写出")
