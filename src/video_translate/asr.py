"""ADR-038 — ASR 层：可插拔 ASRProvider 接口 + ① 层门面 ``run_asr``。

**第一步（已落地，行为零变化）**：定义 ``ASRProvider`` Protocol / ``TranscriberConfig``
/ ``TranscribeResult``，并给出第一个实现 ``FasterWhisperProvider``（薄包装
``transcribe.transcribe_video``）；当时旁挂不接线。

**第二步 A 块（本模块当前形态）**：新增 ① 层门面 ``run_asr()``，把原先散落在
``cli.cmd_transcribe`` 的编排（人声分离 → 转写 → 幻觉过滤+合并+切分 → 漏音补洞+review）
搬进本模块；``cli.cmd_transcribe`` 退化为薄壳（前置检查 + 调门面 + 落 state + 异常映射）。
**门面不做控制面记账**（``vt_state`` 读写 / 退出码）——那是 cli 层的事（ADR-038 D2/D4）。

分层目标（ADR-038 D1/D3）：

    ① ASR 方案层（本模块是其**引擎接口**边界）
        人声分离 → 转写 → 对齐 → 幻觉过滤 → 断句合并 → 漏音补洞 → review
        ↑ Provider 只管「音频 → 原始段」这一段
    ② 字幕交付层
        42 字符切分 → 表现层

Provider 边界 =「音频 → 原始段」，**不含**合并 / 补洞等方案后处理：那些是
「围绕引擎的方案逻辑」，换引擎时复用（仅阈值需重标定）；若塞进引擎接口，
每个新 Provider 都得重写一遍整理逻辑。

契约：``RawSegment`` 必须满足 ``artifacts`` 的 ``segments_raw`` required
（start/end/text）；``words`` 与置信度字段为**引擎可选产出**——缺失时下游信号
自动 inert（ADR-020 向后兼容口径）。产物契约零变化（ADR-038 D4）。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .capabilities import GateFail
from .merge import DEFAULT_MAX_CHARS, DEFAULT_MAX_DUR, DEFAULT_MAX_GAP

# 引擎产出的原始段。必须满足 artifacts "segments_raw" 的 required 契约
# （start/end/text）；words 与置信度字段为引擎可选产出。
RawSegment = dict[str, Any]


@dataclass(frozen=True)
class TranscriberConfig:
    """引擎无关的通用转写参数（ADR-038 D3 / Spec 27）。

    默认值**必须与 ``transcribe.transcribe_video`` 的形参默认值逐字段等价**
    ——``tests/test_asr_provider.py`` 用 ``inspect.signature`` 锁定，防两处漂移。
    """

    model: str = "large-v3"
    chunk: float = 240.0
    threads: int | None = None
    lang: str | None = None
    vad_threshold: float | None = None
    use_vad: bool = False
    no_speech_threshold: float = 0.0
    temperature: list[float] | None = None
    device: str | None = None
    compute_type: str | None = None
    adaptive_vad: bool = False
    # T2 (ADR-017) 人声分离：audio_source 为 None 时用原输入
    audio_source: str | None = None
    separate_vocals: bool = False
    vocal_sep_backend: str = "demucs"
    vocal_sep_model: str = "htdemucs"
    vocal_sep_input_hash: str | None = None
    # T4 (ADR-028 / Spec 22) 强制声学对齐
    align_backend: str = "auto"
    align_allow_degrade: bool = False


@dataclass(frozen=True)
class TranscribeResult:
    """引擎转写结果（Spec 27）。"""

    segments_path: str            # 落盘的 raw segments 路径
    segments: list[RawSegment]    # 段数据（编排层继续后处理用，避免调用方再读盘）
    detected_lang: str | None = None


@runtime_checkable
class ASRProvider(Protocol):
    """ASR 引擎接口（ADR-038 D3/D7）：音频 → 原始段。

    - ``name``            引擎稳定标识（日志 / state 用）。
    - ``prerequisites()`` 本引擎的就绪要求（能力 id 元组），供 doctor / 闸门做
                          「通用体检 + Provider 自报体检」（ADR-038 D7）。
    - ``transcribe()``    唯一必实现方法。
    """

    name: str

    def prerequisites(self) -> tuple[str, ...]: ...

    def transcribe(
        self,
        input_path: str,
        outdir: str,
        *,
        base: str,
        config: TranscriberConfig,
        progress: Callable[..., None] = print,
    ) -> TranscribeResult: ...


class FasterWhisperProvider:
    """第一个实现：薄包装 ``transcribe.transcribe_video``（行为零变化）。

    本类**不复制任何逻辑**，只做两件事：

    1. 把 ``TranscriberConfig`` 逐字段展开成 ``transcribe_video`` 的关键字参数；
    2. 回读落盘的段数据与 ``detected_lang`` sidecar，填充 ``TranscribeResult``。

    读取 sidecar 的原因：``transcribe_video`` 只返回段文件路径，语言落在
    ``{base}.{fp}.detected_lang.json``（fp 是其内部指纹、调用方不可见），
    故按 glob 取最新的同 base sidecar。
    """

    name = "faster-whisper"

    def prerequisites(self) -> tuple[str, ...]:
        """与 ``capabilities.CAPS`` 现有命名对齐（ADR-038 D7）。"""
        return ("model:large-v3", "cuda", "whisperx", "demucs")

    def transcribe(
        self,
        input_path: str,
        outdir: str,
        *,
        base: str,
        config: TranscriberConfig,
        progress: Callable[..., None] = print,
    ) -> TranscribeResult:
        from .io_utils import load_json
        from .transcribe import transcribe_video

        segments_path = transcribe_video(
            input_path, outdir, base=base,
            model_name=config.model,
            chunk=config.chunk,
            threads=config.threads,
            lang=config.lang,
            vad_threshold=config.vad_threshold,
            use_vad=config.use_vad,
            no_speech_threshold=config.no_speech_threshold,
            temperature=config.temperature,
            device=config.device,
            compute_type=config.compute_type,
            adaptive_vad=config.adaptive_vad,
            audio_source=config.audio_source,
            separate_vocals=config.separate_vocals,
            vocal_sep_backend=config.vocal_sep_backend,
            vocal_sep_model=config.vocal_sep_model,
            vocal_sep_input_hash=config.vocal_sep_input_hash,
            align_backend=config.align_backend,
            align_allow_degrade=config.align_allow_degrade,
            progress=progress,
        )
        segments = load_json(segments_path) or []
        return TranscribeResult(
            segments_path=segments_path,
            segments=segments,
            detected_lang=_read_detected_lang(outdir, base),
        )


def _read_detected_lang(outdir: str, base: str) -> str | None:
    """读回 ``{base}.{fp}.detected_lang.json``（取最新的同 base sidecar）。

    文件由 ``transcribe_video`` 落盘，格式 ``{"language": "<lang>"}``。
    不存在 / 损坏 / 解析失败一律返回 ``None``——语言只是元信息，绝不阻断。
    """
    from .artifacts import workdir
    from .io_utils import load_json

    try:
        d = Path(workdir(outdir, base))
        candidates = sorted(d.glob(f"{base}.*.detected_lang.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for p in candidates:
        try:
            data = load_json(str(p))
        except Exception:  # noqa: BLE001 - sidecar 损坏不该阻断转写结果
            continue
        if isinstance(data, dict) and data.get("language"):
            return str(data["language"])
    return None


# ---------------------------------------------------------------------------
# ① 层门面（ADR-038 D5 第二步 A 块）：请求 / 产出 / 编排
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AsrRequest:
    """① ASR 层门面 ``run_asr`` 的完整输入（ADR-038 D5）。

    刻意**不接收 ``argparse.Namespace``** —— 那会让 ``asr.py`` 反向耦合 cli
    入口壳。cli 层负责把 Namespace 归一化成这个请求对象。
    """

    input_path: str
    outdir: str
    base: str
    config: TranscriberConfig
    threads: int | None = None
    # 后处理参数（来自 ``Config``；``TranscriberConfig`` 只管引擎参数，不含合并阈值，
    # 故这里显式承载 —— 默认值直接引用 ``merge`` 的模块常量，避免第二份字面量）
    merge_max_dur: float = DEFAULT_MAX_DUR
    merge_max_gap: float = DEFAULT_MAX_GAP
    merge_max_chars: int = DEFAULT_MAX_CHARS
    # 后处理开关（对应搬迁前 ``cmd_transcribe`` 读的 ``args.*``）
    merge: bool = True
    split: bool = True
    snap_drift: bool = True
    audit: bool = True
    review: bool = True
    g3: bool = True
    # 显式意图的逃生门（``--allow-degrade``）：同时管 T2 分离与 T4 对齐
    allow_degrade: bool = False


@dataclass(frozen=True)
class AsrOutcome:
    """① ASR 层门面的产出（供 cli 层记账与后续阶段消费）。"""

    segments_path: str
    segments: list[RawSegment]
    detected_lang: str | None = None
    # T2 人声分离产物（``audio_source`` 为 None = 本次未分离，用原输入）
    audio_source: str | None = None
    vsep_backend: str = "demucs"
    vsep_model: str = "htdemucs"
    vsep_input_hash: str | None = None


def _gate_vsep(allow_degrade: bool, message: str, guidance: str) -> bool:
    """显式 vsep 请求无法满足时的控制面硬停（裁决一）。

    ``--separate-vocals`` 是 EXPLICIT：demucs 不可用时该请求无法被满足 →
    ``GateFail``（cli.main → exit 8）+ 修复指引；只有 ``--allow-degrade`` 显式
    弃权才降级（打 warning，原因记入 state）。

    Returns:
        True 表示调用方应**降级**（继续用原音频）。硬停时直接抛 ``GateFail``，
        不会返回。
    """
    if allow_degrade:
        print(
            f"[warn] {message} --allow-degrade set; proceeding on the original "
            f"audio (reason recorded in state)",
            file=sys.stderr,
        )
        return True
    raise GateFail(message, guidance)


def _vocal_sep_step(
    request: AsrRequest,
) -> tuple[bool, str | None, str, str, str | None]:
    """T2 人声分离 —— 必须先于任何 Whisper 加载（8GB 显存安全）。

    Spec 19 / ADR-017 §5: 必须先跑 demucs、**释放全部 demucs 显存**，再启动
    Whisper —— 否则两个大模型同驻 8GB 卡必 OOM。

    Control plane 裁决一: ``--separate-vocals`` 是 EXPLICIT 请求；demucs 未安装时
    该请求无法被满足 → 硬停（GateFail → exit 8）+ 修复指引，除非
    ``--allow-degrade`` 显式弃权（则告警 + 回退，原因记入 state）。

    Returns:
        (是否实际分离, audio_source 路径或 None, vsep_backend, vsep_model,
         vsep_input_hash 或 None)
    """
    cfg = request.config
    sep = bool(cfg.separate_vocals)
    dm_model = cfg.vocal_sep_model or "htdemucs"
    backend = "demucs"
    if not sep:
        return False, None, backend, dm_model, None
    # 用户显式要求分离 —— 探测并尝试
    from .vocal_sep import (
        _input_fingerprint,
        demucs_available,
        separate_vocals,
    )
    if not demucs_available():
        # _gate_vsep raises GateFail (→ exit 8) unless --allow-degrade set.
        _gate_vsep(
            request.allow_degrade,
            "--separate-vocals requires the demucs package, which is not "
            "installed. Explicit requests are never silently degraded — the "
            "whole point of vsep is preventing strong-BGM hallucinations.",
            "Run `uv sync` (`pip install -e .` fallback). To skip separation "
            "and proceed on the original audio, drop --separate-vocals or "
            "explicitly bypass with --allow-degrade.",
        )
        return False, None, backend, dm_model, None
    try:
        audio_source = separate_vocals(
            request.input_path, request.outdir, base=request.base,
            backend=backend, model_name=dm_model,
        )
    except Exception as exc:  # noqa: BLE001 - 分离失败按"未分离"继续（同搬迁前）
        print(f"[warn] demucs separation failed ({exc}); falling back to original audio",
              file=sys.stderr)
        audio_source = None
    if audio_source is None:
        # 分离失败，或 demucs 实际未跑（如 backend 不匹配）
        return False, None, backend, dm_model, None
    vsep_input_hash = _input_fingerprint(request.input_path)
    # 强制断言时长不变量（ADR-017 §2 的承重守卫）
    try:
        from .ffmpeg_utils import probe_duration
        din = probe_duration(request.input_path)
        dout = probe_duration(audio_source)
        if abs(din - dout) >= 0.05:
            print(f"[warn] demucs output duration {dout:.3f}s ≠ input {din:.3f}s;\n"
                  f"       refusing to shift timestamps — falling back to original audio")
            return False, None, backend, dm_model, None
    except Exception:  # noqa: BLE001 - 探测失败则信任 separate_vocals 的内部断言
        pass
    # Spec 19 / ADR-017 §5 —— 在构造任何 WhisperModel 之前显式释放 demucs/torch 显存
    try:
        import gc
        gc.collect()
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - 无 torch 即无 CUDA 可清
            pass
    except Exception:  # noqa: BLE001
        pass
    return True, audio_source, backend, dm_model, vsep_input_hash


def run_asr(
    request: AsrRequest,
    *,
    provider: ASRProvider | None = None,
    silence_intervals: list[tuple[float, float]] | None = None,
    progress: Callable[..., None] = print,
) -> AsrOutcome:
    """① ASR 层唯一门面（ADR-038 D5 第二步 A 块）。

    编排（与搬迁前 ``cli.cmd_transcribe`` 逐项等价）：

      1. 人声分离（T2）—— 必须先于任何 Whisper import（8GB 显存串行调度）
      2. 引擎转写（Provider；含 T4 强制对齐）
      3. 幻觉过滤 + 断句合并 + 切分 + 短句合并 + 孤儿并右（``apply_merge``）
      4. 漏音补洞 + review + G1/G2/G3（``fill_gaps``）

    Args:
        request: ① 层的完整输入（见 :class:`AsrRequest`）。
        provider: 引擎实现；默认 :class:`FasterWhisperProvider`（换引擎只换它）。
        silence_intervals: 独立静音参照（``ffmpeg silencedetect``）。**由调用方供给**
            —— state 的读取/回写属控制面，留在 cli 层（ADR-035 M2）；为 ``None``
            时门面兜底重算一次，保证直调 ``run_asr`` 也拿得到参照。
        progress: 进度回调（默认 print）。

    Returns:
        :class:`AsrOutcome`：段文件路径 / 段数据 / 检测语言 / 分离产物。

    **不做**控制面记账（``vt_state`` 落盘、退出码）——那是 cli 层的事。
    """
    from .artifacts import artifact_path
    from .io_utils import load_json, save_json

    cfg = request.config
    engine = provider if provider is not None else FasterWhisperProvider()

    # ---- 1) T2 人声分离（必须最先；返回时 demucs 显存已释放） ----
    sep_on, audio_source, vsep_backend, vsep_model, vsep_hash = _vocal_sep_step(request)

    # ---- 2) 引擎转写（T1 转写 + T4 对齐） ----
    engine_cfg = replace(
        cfg,
        audio_source=audio_source,
        separate_vocals=sep_on,
        vocal_sep_backend=vsep_backend,
        vocal_sep_model=vsep_model,
        vocal_sep_input_hash=vsep_hash,
    )
    result = engine.transcribe(
        request.input_path, request.outdir, base=request.base,
        config=engine_cfg, progress=progress,
    )
    segs_path = result.segments_path

    # ---- 3) 独立静音参照（ADR-012：同一份参照喂 merge 与 fill_gaps） ----
    # Spec 19 Invariant #4: 参照永远取自原始输入，绝不用清洗后的 audio_source。
    if silence_intervals is None:
        try:
            from .audio_profile import analyze_audio
            prof = analyze_audio(request.input_path)
            silence_intervals = prof.silence_intervals if prof.ok else None
        except Exception:  # noqa: BLE001 - 参照缺失不该阻断转写（同搬迁前）
            silence_intervals = None

    # ---- 4) 幻觉过滤 + 断句合并 + 切分（apply_merge） ----
    if request.merge:
        from .merge import apply_merge
        raw_path = artifact_path("segments_raw", request.outdir, request.base)
        apply_merge(
            segs_path, raw_path=raw_path,
            max_dur=request.merge_max_dur, max_gap=request.merge_max_gap,
            split_enabled=request.split,
            split_max_chars=request.merge_max_chars,
            snap_drift=request.snap_drift,
            silence_intervals=silence_intervals,
            progress=progress,
        )
        progress(f"[merge] merged + split -> {segs_path} (raw kept at {raw_path})")

    # ---- 5) 漏音补洞 + review + G1/G2/G3（fill_gaps） ----
    if request.audit:
        from .fill_gaps import fill_gaps
        from .io_utils import tee_progress
        segs = load_json(segs_path)
        # ADR-035 M3（Z2）: review 按 _raw_indices 回查 raw 段置信度（G1 复明）。
        raw_segs = None
        try:
            _rp = artifact_path("segments_raw", request.outdir, request.base)
            if os.path.isfile(_rp):
                raw_segs = load_json(_rp)
        except Exception:  # noqa: BLE001 - raw 缺失/损坏降级为旧行为，不阻断
            raw_segs = None
        recovered = fill_gaps(
            request.input_path, segs, lang=cfg.lang,
            model_name=cfg.model,
            use_vad=False,  # ADR-016 (T2a): recovery is always bare
            silence_intervals=silence_intervals,
            device=cfg.device, compute_type=cfg.compute_type,
            # T2 / Spec 19 §(B): recovery 与主 pass 同源（vocals.wav 或原视频）
            audio_source=audio_source,
            # ADR-034 §6.2: 双信号 review + G1/G2 重处理
            review=request.review,
            # ADR-034 §6.3: G3 局部 separate-vocals（强 BGM 兜底）
            g3=request.g3,
            # ADR-034 §5.2: 独立缓存层（重跑只处理可疑窗）
            outdir=request.outdir, base=request.base,
            # ADR-035 M3（Z2）: 信号 A 经 _raw_indices 回查 raw 源段
            raw_segments=raw_segs,
            # ADR-035 可观测性: [audit] 审计行同时落盘 <base>.review.log
            progress=tee_progress(
                artifact_path("review_log", request.outdir, request.base)),
        )
        if recovered is not segs:
            save_json(segs_path, recovered, indent=0)

    return AsrOutcome(
        segments_path=segs_path,
        segments=load_json(segs_path) or [],
        detected_lang=result.detected_lang,
        audio_source=audio_source,
        vsep_backend=vsep_backend,
        vsep_model=vsep_model,
        vsep_input_hash=vsep_hash,
    )
