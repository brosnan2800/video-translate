import os, sys, time, json

PROXY = "http://127.0.0.1:7890"
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ[k] = PROXY
os.environ.pop("all_proxy", None)
os.environ.pop("ALL_PROXY", None)

from deep_translator import GoogleTranslator

SRC = "/Users/yanglei/Movies/翻译/subtitles/apollo_story.segments_en.json"
ZH_OUT = "/Users/yanglei/Movies/翻译/subtitles/zh_segments.json"
PENDING = "/Users/yanglei/Movies/翻译/subtitles/agent_pending.json"

segs = json.load(open(SRC))
n = len(segs)

# 续传：已翻过的按 index 跳过
done = {}
if os.path.exists(ZH_OUT):
    try:
        done = {int(k): v for k, v in json.load(open(ZH_OUT)).items()}
    except Exception:
        done = {}
print(f"[翻译] 总段数={n}, 已完成={len(done)}", flush=True)

tr = GoogleTranslator(source="en", target="zh-CN")
pending = []

def translate_one(text):
    t = text.strip()
    if not t:
        return ""
    last = None
    for attempt in range(3):  # 1 次 + 重试 2 次
        try:
            return tr.translate(t)
        except Exception as e:
            last = e
            time.sleep(1.0 + attempt)
    raise last

start = time.time()
for i, s in enumerate(segs):
    if i in done:
        continue
    text = s.get("text", "")
    try:
        zh = translate_one(text)
        if zh is None:
            raise ValueError("empty result")
        done[i] = zh
    except Exception as e:
        print(f"  [FAIL] 段 {i}: {str(e)[:80]}", flush=True)
        pending.append({"index": i, "start": s.get("start"), "end": s.get("end"), "text": text})
    # 增量落盘
    if (i + 1) % 10 == 0 or (i + 1) == n:
        json.dump({str(k): v for k, v in done.items()}, open(ZH_OUT, "w"), ensure_ascii=False, indent=0)
        print(f"  [进度] {i+1}/{n}  已翻{len(done)} 耗时{time.time()-start:.0f}s", flush=True)

# 最终落盘
json.dump({str(k): v for k, v in done.items()}, open(ZH_OUT, "w"), ensure_ascii=False, indent=0)
json.dump(pending, open(PENDING, "w"), ensure_ascii=False, indent=2)
print(f"[完成] 翻译 {len(done)}/{n} 段, 失败 {len(pending)} 段 -> agent_pending.json", flush=True)
