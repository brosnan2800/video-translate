"""ADR-042 / Spec 29 — 接口型 ASR：YouTube 字幕的抓取与句子化。

两件事，刻意分层：

  1. **取**（`fetch_transcript` / `list_transcripts` / `preflight_network`）
     —— 需要网络；通路预检失败即 `GateFail`（ADR-042 D8）。
  2. **洗**（`parse_video_id` / `snippets_to_segments` 及内部纯函数）
     —— **纯函数**，无网络、无 I/O，可离线全量单测。

第 2 层是本模块的错误高发区（时间轴不单调、句子被切碎、滚动重复没去干净），
所以它被刻意做成无副作用、可逐条断言的形式。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .merge import DEFAULT_MAX_CHARS, DEFAULT_MAX_DUR
from .text_utils import to_single_line

# 无标点兜底（Spec 29）：一整段没有句末标点时，按软上限强制切分，
# 防止"整篇一句"。阈值**复用 merge 的既有常量**，不引入第二套数字。
_NO_PUNCT_MAX_DUR_FRAC = 0.6
_NO_PUNCT_MAX_CHARS_MULT = 2

# 句末标点：全角一律是句子边界；ASCII 需要额外判断（缩写 / 小数）。
_CJK_SENT_END = "。！？…；"
_ASCII_SENT_END = ".!?"

# 归一化：比较滚动重叠时忽略大小写、空白与标点（**只用于比较**，输出保留原文）。
_NORM_DROP = re.compile(r"[\s\u3000]+")
_NORM_PUNCT = re.compile(r"[,，.。!！?？;；:：'\"“”‘’()（）\[\]【】…—\-~]")

# 滚动重叠的最长扫描窗口（字符）。超过这个长度还不匹配就不必再找 ——
# YouTube 的滚动重复是"上一条的尾巴"，量级是几个词，120 字符已很宽裕。
_OVERLAP_SCAN_MAX = 120

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# --------------------------------------------------------------------------- #
# URL / id 解析（纯函数）
# --------------------------------------------------------------------------- #

def parse_video_id(url_or_id: str) -> str:
    """从 URL 或裸 id 中解析出 11 位 YouTube video id。

    支持：完整 `watch?v=`、`youtu.be/` 短链、`/embed/`、`/shorts/`、`/v/`、
    裸 id。解析不出即 `ValueError`（调用方据此报 usage error）。
    """
    s = (url_or_id or "").strip()
    if not s:
        raise ValueError("empty YouTube url/id")
    if _VIDEO_ID_RE.match(s):
        return s
    # 去掉 query / fragment 后再取路径末段
    body = s.split("#", 1)[0]
    if "?" in body:
        head, _, query = body.partition("?")
        for part in query.split("&"):
            key, _, val = part.partition("=")
            if key == "v" and _VIDEO_ID_RE.match(val.strip()):
                return val.strip()
        body = head
    for seg in reversed([p for p in body.split("/") if p]):
        if _VIDEO_ID_RE.match(seg):
            return seg
    raise ValueError(f"cannot parse a YouTube video id from {url_or_id!r}")


# --------------------------------------------------------------------------- #
# 句子化（纯函数）
# --------------------------------------------------------------------------- #

def _norm(s: str) -> str:
    """比较用的归一化形式：去空白 + 去标点 + 小写。"""
    return _NORM_PUNCT.sub("", _NORM_DROP.sub("", s)).lower()


def _overlap_len(acc: str, new: str) -> int:
    """`acc` 的后缀与 `new` 的前缀的最长（归一化后）公共长度。

    滚动字幕的相邻片段会重复上一段的尾巴（`["...quick brown", "brown fox..."]`），
    这里找出该重复的长度，调用方据此只追加新增部分。
    """
    limit = min(len(acc), len(new), _OVERLAP_SCAN_MAX)
    for n in range(limit, 0, -1):
        if _norm(acc[-n:]) == _norm(new[:n]):
            return n
    return 0


def accumulate(snippets: list[dict[str, Any]]) -> tuple[str, list[int]]:
    """把滚动片段拼成连续文本，并给出「每个字符属于第几条 snippet」的索引表。

    返回 ``(text, owners)``：``owners[i]`` 是 ``text[i]`` 的来源 snippet 下标。
    时间戳插值依赖这张表 —— 没有它就无法知道某个句子落在哪几条片段上。
    """
    parts: list[str] = []
    owners: list[int] = []
    for i, sn in enumerate(snippets):
        piece = (sn.get("text") or "").replace("\n", " ")
        if not piece.strip():
            continue
        if parts:
            cut = _overlap_len("".join(parts), piece)
            piece = piece[cut:]
        if not piece:
            continue
        if parts and not parts[-1].endswith(" ") and not piece.startswith(" "):
            parts.append(" ")           # 片段之间补一个空格，避免词被粘死
            owners.append(owners[-1] if owners else i)
        parts.append(piece)
        owners.extend([i] * len(piece))
    return "".join(parts), owners


def _prev_word(text: str, pos: int) -> str:
    """``text[pos-1]`` 往前回溯出的一个"词"（字母数字）。"""
    j = pos - 1
    while j >= 0 and (text[j].isalnum() or text[j] in "&'"):
        j -= 1
    return text[j + 1:pos]


def _is_abbrev(text: str, pos: int) -> bool:
    """``text[pos]`` 是 ``.`` 且疑似缩写点（``Mr.`` / ``Dr.`` / ``e.g.``）。"""
    word = _prev_word(text, pos)
    return 1 <= len(word) <= 2 and word[:1].isupper()


def split_points(text: str) -> list[int]:
    """句末切点（返回每句**结束**的下标，不含该下标本身）。

    全角标点一律是边界；ASCII `.`/`!`/`?` 需要额外判断：
      - 后面必须是空白 / 引号 / 结尾（`3.14`、`Mr.X` 不切）；
      - 前面不能是数字（小数）；
      - 前面不能是短缩写（`Mr.` / `Dr.` —— 否则一句被腰斩）。
    连续标点（`...`、`?!`）合并为一个边界。
    """
    out: list[int] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        is_end = ch in _CJK_SENT_END
        if not is_end and ch in _ASCII_SENT_END:
            nxt = text[i + 1] if i + 1 < n else ""
            prev = text[i - 1] if i > 0 else ""
            is_end = (nxt == "" or nxt.isspace() or nxt in "\"')]}") \
                and not prev.isdigit() \
                and not (ch == "." and _is_abbrev(text, i))
        if is_end:
            j = i + 1
            while j < n and text[j] in _CJK_SENT_END + _ASCII_SENT_END + "」』”’)]】":
                j += 1
            # 连带吃掉结尾的空白，避免下一句以空格开头
            while j < n and text[j].isspace():
                j += 1
            out.append(j)
            i = j
            continue
        i += 1
    return out


def effective_windows(snippets: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """每条片段的**有效时间窗**（真实换行时刻，而非 ``start + duration``）。

    ⚠️ 实测结论（YouTube 自动轨）：``duration`` 是**显示时长**，会越过下一条片段的
    ``start`` —— 片段是**滚动窗口**。某真实 Shorts 的前两条：

        0.00 +3.36  "Just forgive. [music] And don't worry."
        2.56 +2.68  '>> Huh.'      ← 2.56 就把上一条**替换**掉了（而非 3.36）

    若按 ``[start, start+duration]`` 取窗，同一条片段里的**第二句**会被前一句占满
    窗口、再被"互不重叠"钳制挤成 0.01s 的不可见微段（实测复现）。真实锚点是
    **下一条的 start**：片段 k 的有效窗口是 ``[s_k, s_{k+1})``，末条用自身 end。

    非滚动数据（``s_{k+1} >= e_k``）保持原窗口，不受影响。
    """
    n = len(snippets)
    out: list[tuple[float, float]] = []
    for k, sn in enumerate(snippets):
        start = float(sn.get("start") or 0.0)
        end = start + float(sn.get("duration") or 0.0)
        if k + 1 < n:
            nxt = float(snippets[k + 1].get("start") or 0.0)
            if start < nxt < end:
                end = nxt
        out.append((start, max(end, start)))
    return out


def char_times(owners: list[int],
               windows: list[tuple[float, float]]) -> list[float]:
    """每个字符的**起始时刻**；长度 = ``len(owners) + 1``（末尾为整体结束哨兵）。

    片段窗口内按**字符长度比例**线性插值 —— 这正是 ADR-042 D2 要求的「成比例」：
    一条片段里若含多个句子，它们按各自字符数**分摊**该窗口，而不是「先到先得」
    （后者会把后续句子挤成 0.01s）。最后强制单调不减，抵御异常数据回跳。
    """
    if not owners:
        return [0.0]
    span_of: dict[int, list[int]] = {}
    for i, own in enumerate(owners):
        if own in span_of:
            span_of[own][1] = i + 1
        else:
            span_of[own] = [i, i + 1]
    times: list[float] = []
    for i, own in enumerate(owners):
        start, end = windows[own]
        a, b = span_of[own]
        times.append(start + (end - start) * ((i - a) / max(1, b - a)))
    times.append(windows[owners[-1]][1])
    for i in range(1, len(times)):
        if times[i] < times[i - 1]:
            times[i] = times[i - 1]
    return times


def snippets_to_segments(
    snippets: list[dict[str, Any]],
    *,
    max_dur: float = DEFAULT_MAX_DUR,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """滚动片段 → 句子化的 `RawSegment[]`（Spec 29 的核心算法）。

    三步：去重叠拼接 → 按句末标点切句（无标点则按软上限兜底）→ 时间戳插值。

    产出保证（load-bearing）：
      - ``start < end``；
      - 段之间**严格非递减**、**互不重叠**（重叠部分归前一段）；
      - 文本经 ``to_single_line``（ADR-040 单行不变量）。

    > 这些时间戳**不是声学真值**（ADR-042 D3 已接受），但必须**单调、连续、
    > 与文本长度成比例** —— 表现层（tail / min-dur / 折行）据此才有合理依据。
    """
    text, owners = accumulate(snippets)
    if not text.strip():
        return []
    windows = effective_windows(snippets)
    times = char_times(owners, windows)

    # 切点 → 区间
    bounds = [0] + split_points(text) + [len(text)]
    spans: list[tuple[int, int]] = [(a, b) for a, b in zip(bounds, bounds[1:])
                                    if b > a]

    # 软上限兜底：把超限的长块再切（理由见 _enforce_fallbacks docstring）
    max_chars_fallback = max(1, int(max_chars * _NO_PUNCT_MAX_CHARS_MULT))
    max_dur_fallback = max(0.1, float(max_dur) * _NO_PUNCT_MAX_DUR_FRAC)
    spans = _enforce_fallbacks(spans, owners, times,
                               max_chars_fallback, max_dur_fallback)

    out: list[dict[str, Any]] = []
    prev_end = 0.0
    for a, b in spans:
        chunk = to_single_line(text[a:b]).strip()
        if not chunk:
            continue
        start, end = times[a], times[b]
        if start < prev_end:                 # 互不重叠：重叠归前一段
            start = prev_end
        if end <= start:
            end = start + 0.01              # start < end 必须成立
        out.append({"start": round(start, 2), "end": round(end, 2),
                    "text": chunk})
        prev_end = end
    return out


def _enforce_fallbacks(
    spans: list[tuple[int, int]],
    owners: list[int],
    times: list[float],
    max_chars: int,
    max_dur: float,
) -> list[tuple[int, int]]:
    """把超限块切开（优先落在片段边界，找不到就按软上限硬切）。

    两个软上限**同时**生效、取先到者：字符数（``2 × 剪映单行上限`` ≈ 84）与时长
    （``0.6 × merge 的 cue 上限`` ≈ 4.8s）。

    **为什么本方案必须自带这道切**：接口型 ASR 的段**没有 ``words[]``**，`merge`
    的词级切分对它无效 —— 若不在这里切，一条 90+ 字符的长句会原样进入最终字幕
    （超出剪映单行上限）。切点优先选**片段边界**，避免把一条滚动片段从中间劈开。
    """
    out: list[tuple[int, int]] = []
    for a, b in spans:
        cur = a
        while True:
            if b - cur <= max_chars and times[b] - times[cur] <= max_dur:
                out.append((cur, b))           # 未超限 → 整段保留（勿漏！）
                break
            # 硬上限：字符与时长谁先到听谁的
            limit = min(b - 1, max(cur + 1, cur + max_chars))
            t_limit = times[cur] + max_dur
            while limit > cur + 1 and times[limit] > t_limit:
                limit -= 1
            cut = limit
            for i in range(limit, cur, -1):    # 回退到最近的片段边界
                if owners[i] != owners[cur]:
                    cut = i
                    break
            if cut <= cur:                     # 保证前进，避免死循环
                cut = cur + 1
            out.append((cur, cut))
            cur = cut
    return out


# --------------------------------------------------------------------------- #
# 网络层：通路预检与取字幕（ADR-042 D8 / D9）
# --------------------------------------------------------------------------- #

class CaptionUnavailable(RuntimeError):
    """取字幕失败，且**不是通路问题**（视频本就无字幕 / 缺该语言 / 视频不可用）。

    与 :class:`~video_translate.capabilities.GateFail` 的区别是刻意的：前者是
    「换个时间/出口就能好」（exit 8，环境前提不成立），后者是「这个视频就是没有」
    （exit 1，与网络无关）。把两者混在一起会让诊断失去方向。
    """

    def __init__(self, message: str, guidance: str = ""):
        super().__init__(message)
        self.message = message
        self.guidance = guidance


def split_host_port(proxy: str) -> tuple[str, int]:
    """从 ``http://host:port`` 解析 ``(host, port)``；解析不出返回 ``("", 0)``。"""
    from urllib.parse import urlparse

    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    try:
        return (parsed.hostname or ""), int(parsed.port or 80)
    except (TypeError, ValueError):
        return "", 0


def _exc_groups() -> tuple[tuple[type, ...], tuple[type, ...]]:
    """``(通路类异常, 内容类异常)`` —— 延迟 import 第三方包，避免拖慢 CLI 启动。

    分组依据是**归因**（该怪环境还是该怪视频），不是异常在库里的层级。
    """
    from youtube_transcript_api import _errors as E

    passage = (E.IpBlocked, E.RequestBlocked, E.PoTokenRequired,
               E.YouTubeRequestFailed, E.YouTubeDataUnparsable)
    missing = (E.TranscriptsDisabled, E.NoTranscriptFound, E.VideoUnavailable,
               E.VideoUnplayable, E.AgeRestricted, E.InvalidVideoId)
    return passage, missing


def _guarded(fn: Callable[[], Any]) -> Any:
    """执行库调用，把上游异常映射为本项目的两种语义（GateFail / CaptionUnavailable）。"""
    from .capabilities import GateFail

    passage, missing = _exc_groups()
    try:
        return fn()
    except passage as e:
        raise GateFail(
            f"YouTube blocked the request ({type(e).__name__})",
            "通路被拒，与视频内容无关。依次排查：① 代理是否真的生效（--proxy 指定，"
            "或设 VT_PROXY；代理监听在非默认端口时还要设 VT_PROXY_PORT）；"
            "② 出口 IP 是否被 YouTube 标记 —— 云厂商（AWS/GCP/Azure）出口常被拒，"
            "本地家宽通常可用；③ 稍后重试（限流是暂时的）。",
        ) from e
    except missing as e:
        raise CaptionUnavailable(
            f"no usable transcript for this video ({type(e).__name__})",
            "该视频没有可用字幕轨（或没有你请求的语言）。用 --list 查看可用轨道；"
            "若确实无字幕，只能走本地转写：`uv run video-translate run <video>`。",
        ) from e


def _make_api(proxy: str | None):
    """构造 API 实例；代理经 ``GenericProxyConfig`` **显式注入**。

    该库用构造参数（而非环境变量）配置代理，所以不能只依赖 ``setup_http_proxy``
    的副作用 —— 必须把解析结果显式传进来（ADR-042 D5 的核实结论）。
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    if not proxy:
        return YouTubeTranscriptApi()
    from youtube_transcript_api.proxies import GenericProxyConfig

    return YouTubeTranscriptApi(
        proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy))


def preflight_network(proxy: str | None, *, timeout: float = 5.0) -> None:
    """通路预检：代理（若配置）连得上 **且** YouTube 达得到；否则 ``GateFail``（exit 8）。

    **为什么是硬停而不是降级**：本命令的**唯一**功能就是取字幕，网络不通即功能不可
    执行；而这里"降级"唯一的落点就是"偷偷改跑本地 Whisper"—— 那正是 ADR-042 D2 明令
    禁止的（用户没让它转写）。所以它是一条 gate：前提不成立就停，并给出确定性指引。

    判定口径为**端到端**（ADR-042 用户决策）：没配代理也允许直连尝试，只有
    「配了代理但连不通」或「直连也不通」才停 —— 海外机器不会被无谓拦住。
    """
    from .capabilities import GateFail
    from .proxy import DEFAULT_PROXY_PORT, _probe, _probe_youtube_endpoint

    if proxy:
        host, port = split_host_port(proxy)
        if host and not _probe(host, port, timeout):
            raise GateFail(
                f"HTTP proxy {proxy} is not accepting connections",
                f"代理端口不通。确认代理软件已启动、端口与 --proxy/VT_PROXY 一致；"
                f"自动探测的默认端口是 {DEFAULT_PROXY_PORT}，监听在别处时设 "
                f"VT_PROXY_PORT。",
            )
    if not _probe_youtube_endpoint(proxy, timeout=timeout):
        if proxy:
            detail = f"已配置代理 {proxy}，但经它仍无法到达 YouTube。"
        else:
            detail = ("未检测到可用代理（自动探测未命中，VT_PROXY/HTTPS_PROXY 也未设），"
                      "直连 YouTube 不通。")
        raise GateFail(
            "YouTube is unreachable",
            f"{detail}\n请配置 HTTP 代理：`--proxy http://127.0.0.1:<port>` 或设 "
            f"VT_PROXY；代理监听在非默认端口时同时设 VT_PROXY_PORT（默认 "
            f"{DEFAULT_PROXY_PORT}）。注意 YouTube 会大面积拒绝云厂商出口 IP，"
            f"本地家宽通常可用。",
        )


def list_transcripts(video_id: str, *, proxy: str | None = None) -> list[dict[str, Any]]:
    """列出可用字幕轨（供 ``--list``；**不落盘**）。人工轨排在自动轨之前。"""
    tl = _guarded(lambda: _make_api(proxy).list(video_id))
    out: list[dict[str, Any]] = []
    for t in tl:
        out.append({
            "lang": getattr(t, "language_code", "") or "",
            "language": getattr(t, "language", "") or "",
            "track": "auto" if getattr(t, "is_generated", False) else "manual",
        })
    out.sort(key=lambda x: (x["track"] != "manual", x["lang"]))
    return out


def _pick_transcript(tl: Any, langs: list[str], *, allow_auto: bool) -> Any:
    """人工 CC 优先于自动 ASR（ADR-042 D2）；同优先内按 ``langs`` 顺序。

    ⚠️ **库的语义陷阱**（实测踩到）：``find_manually_created_transcript`` /
    ``find_generated_transcript`` 在**没有匹配时抛 ``NoTranscriptFound``，而不是
    返回 ``None``**（见 ``TranscriptList._find_transcript``）。若按「返回 None」写，
    「只有自动轨、没有人工轨」这一**最常见**情形会在第一类轨道上直接抛出去，被
    误判成「整个视频没有字幕」——而 ``--list`` 明明看得到轨道。故必须逐类接住再试。
    """
    from youtube_transcript_api import NoTranscriptFound

    for finder, enabled in ((tl.find_manually_created_transcript, True),
                            (tl.find_generated_transcript, allow_auto)):
        if not enabled:
            continue
        try:
            return finder(langs)          # 语言优先级由库按 langs 顺序自行处理
        except NoTranscriptFound:
            continue
    raise CaptionUnavailable(
        f"no transcript in {langs!r}"
        + ("" if allow_auto else " (auto-generated excluded by --no-auto)"),
        "用 --list 查看该视频实际可用的语言与轨道类型，再据此调整 --lang"
        + ("，或去掉 --no-auto。" if not allow_auto else "。"),
    )


def fetch_transcript(
    video_id: str,
    langs: list[str],
    *,
    proxy: str | None = None,
    allow_auto: bool = True,
) -> dict[str, Any]:
    """取字幕。返回 ``{"snippets": [...], "track": "manual|auto", "lang": "..."}``。

    只调用一次（不做重试）：核实结论是本库内部**只有一套硬编码 client**
    （``INNERTUBE_CONTEXT`` = ANDROID），不存在可轮换的 web/ios 身份，因此
    「换 client 重试」在此不可实现；同 client 盲目重试对 IP 封禁型失败也无意义
    （ADR-042 D8 已据实修正）。
    """
    api = _make_api(proxy)
    tl = _guarded(lambda: api.list(video_id))
    # `_pick_transcript` 自带错误语义（逐类轨道接住 NoTranscriptFound），
    # 不经 `_guarded` —— 否则中间步骤的 NoTranscriptFound 会被误映射成
    # 「整个视频没有字幕」。
    transcript = _pick_transcript(tl, langs, allow_auto=allow_auto)
    fetched = _guarded(transcript.fetch)
    snippets: list[dict[str, Any]] = []
    for s in fetched:
        snippets.append({
            "text": getattr(s, "text", "") or "",
            "start": float(getattr(s, "start", 0.0) or 0.0),
            "duration": float(getattr(s, "duration", 0.0) or 0.0),
        })
    return {
        "snippets": snippets,
        "track": "auto" if getattr(transcript, "is_generated", False) else "manual",
        "lang": getattr(transcript, "language_code", "") or "",
    }


# --------------------------------------------------------------------------- #
# 缓存（ADR-042 D9）
# --------------------------------------------------------------------------- #

def load_cache(path: str, video_id: str,
               langs: list[str]) -> dict[str, Any] | None:
    """读缓存；视频或语言不匹配即视为未命中（返回 None）。"""
    import os
    if not os.path.isfile(path):
        return None
    try:
        from .io_utils import load_json
        data = load_json(path) or {}
    except Exception:  # noqa: BLE001 - 缓存损坏不该阻断，重取即可
        return None
    if not isinstance(data, dict):
        return None
    if data.get("video_id") != video_id:
        return None
    if str(data.get("lang") or "") != str(langs[0] if langs else ""):
        return None
    if not isinstance(data.get("snippets"), list) or not data["snippets"]:
        return None
    return data


def save_cache(path: str, video_id: str, got: dict[str, Any]) -> None:
    """落缓存（best-effort：写失败不影响本次结果）。"""
    try:
        from .io_utils import save_json
        save_json(path, {
            "kind": "youtube-captions",
            "video_id": video_id,
            "lang": got.get("lang") or "",
            "track": got.get("track") or "",
            "snippets": got.get("snippets") or [],
        }, indent=0)
    except Exception:  # noqa: BLE001 - 缓存是增强，永不阻断
        pass


# --------------------------------------------------------------------------- #
# Provider（ADR-038 的第二个上游）
# --------------------------------------------------------------------------- #

class YtCaptionProvider:
    """接口型 ASR 的 Provider 实现（ADR-042 D1）。

    与 ``FasterWhisperProvider`` **并列**：同样只负责「输入 → 原始段」，同样把
    合并 / 补洞 / 归一化留给 ① 层编排（``asr.run_asr``）。区别是它**取**而不**算**。

    产出的是**未合并**的句子化段（写到 ``segments_en.json``），随后由 ``run_asr``
    的 ``apply_merge`` 统一切分/合并 —— 与 Whisper 路径走**同一段编排代码**，
    这正是「两套方案共用 ② 层」在 ① 层内部的对应物。
    """

    name = "youtube-captions"

    def __init__(self, *, proxy: str | None = None, langs: tuple[str, ...] = ("en",),
                 allow_auto: bool = True, cached: dict[str, Any] | None = None):
        self.proxy = proxy
        self.langs = list(langs)
        self.allow_auto = allow_auto
        self.cached = cached
        self.fetched: dict[str, Any] | None = None   # 本次实际使用的字幕数据
        self.track = ""
        self.video_id = ""

    def prerequisites(self) -> tuple[str, ...]:
        """无本地模型要求 —— 通路可达性由 :func:`preflight_network` 在取之前硬闸。"""
        return ()

    def transcribe(self, input_path: str, outdir: str, *,
                   base: str, config: Any = None,
                   progress: Callable[..., None] = print) -> Any:
        from .artifacts import artifact_path
        from .asr import TranscribeResult
        from .io_utils import save_json

        self.video_id = parse_video_id(input_path)
        got = self.cached
        if got is None:
            preflight_network(self.proxy)      # 通路不通 → GateFail(exit 8)
            progress(f"[captions] fetching transcript for {self.video_id} "
                     f"(langs={self.langs}, proxy={self.proxy or 'direct'})")
            got = fetch_transcript(self.video_id, self.langs,
                                   proxy=self.proxy,
                                   allow_auto=self.allow_auto)
        else:
            progress(f"[captions] cache hit for {self.video_id} — no network")
        self.fetched = got
        self.track = str(got.get("track") or "")
        segments = snippets_to_segments(got.get("snippets") or [])
        if not segments:
            raise CaptionUnavailable(
                "the selected transcript is empty",
                "该字幕轨没有任何可用片段（可能只含音效标注）。用 --list 换一条轨道，"
                "或改用本地转写：`uv run video-translate run <video>`。",
            )
        path = artifact_path("segments", outdir, base)
        save_json(path, segments, indent=0)
        progress(f"[captions] {len(segments)} sentenceized segments "
                 f"({self.track}) -> {path}")
        return TranscribeResult(
            segments_path=path, segments=segments,
            detected_lang=str(got.get("lang") or "") or None,
        )


