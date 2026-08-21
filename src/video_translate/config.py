"""Configuration resolution.

Priority (highest wins): CLI args > environment variables > project-level
`.video-translate.toml` > built-in defaults.

Only project-level config is supported (no user-level layer). V5 (ADR-014)
adds ``device``/``compute_type`` (default "auto") — CUDA is resolved when an
NVIDIA GPU is present, otherwise the historical cpu/int8 behaviour is unchanged.

V2: adds engine/persona/merge_* fields; lang defaults to None (auto-detect);
proxy defaults to None (auto-detect via proxy.detect_proxy). The literal
``"auto"`` for lang is normalised to None.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .toolchain import init_toolchain

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

CONFIG_FILENAME = ".video-translate.toml"
DEFAULT_HF_CACHE = os.path.expanduser("~/.cache/huggingface")

DEFAULT_PERSONA = (
    "你是一位资深中英字幕译者。遵循「信达雅」+ 口语感：忠实原意、表达自然、"
    "保留说话人语气与情绪。遇到俚语/文化梗用贴近中文口语的等价表达，不要直译。"
)


@dataclass
class Config:
    """Resolved runtime configuration."""

    model: str = "large-v3"
    chunk: float = 240.0
    lang: str | None = None          # V2: None = auto-detect (Whisper)
    proxy: str | None = None         # V2: None = auto-detect / direct
    src: str = "en"
    tgt: str = "zh-CN"
    hf_cache_dir: str = DEFAULT_HF_CACHE
    # V5 (ADR-014) fields
    device: str = "auto"             # "auto" -> CUDA if available, else cpu
    compute_type: str = "auto"       # "auto" -> int8_float16 on cuda, else int8
    # V2 fields
    engine: str = "agent"
    persona: str = DEFAULT_PERSONA
    merge_enabled: bool = True
    merge_max_dur: float = 8.0
    merge_max_gap: float = 0.5
    merge_max_chars: int = 42
    # V3 fields
    glossary: str | None = None     # path to glossary file (txt/json), injected into persona
    # V6 (B3) fields
    source: str | None = None       # free-text provenance/背景 hint for the translator
    full_transcript: bool = True    # ship whole transcript in the agent task file
    # T2 (ADR-017 / Spec 19): vocal separation preprocessing
    separate_vocals: bool = False   # --separate-vocals / VT_SEPARATE_VOCALS
    demucs_model: str = "htdemucs"  # --demucs-model / VT_DEMUCS_MODEL
    _sources: dict[str, str] = field(default_factory=dict, repr=False)


# TOML sections flattened into Config fields. [hf] cache_dir -> hf_cache_dir.
_TOML_SECTIONS = ("transcribe", "translate", "hf", "llm", "merge")


def load_toml(path: str) -> dict[str, Any]:
    """Load a `.video-translate.toml` into a flat dict, or {} if missing.

    Sections [transcribe]/[translate]/[hf]/[llm]/[merge] are flattened into
    single keys. [hf] cache_dir -> hf_cache_dir.
    """
    if not os.path.exists(path) or tomllib is None:
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    flat: dict[str, Any] = {}
    for section in _TOML_SECTIONS:
        for k, v in (data.get(section) or {}).items():
            key = "hf_cache_dir" if (section == "hf" and k == "cache_dir") else k
            flat[key] = v
    return flat


_FLOAT_ENV = {"chunk", "merge_max_dur", "merge_max_gap"}
_INT_ENV = {"merge_max_chars"}
_BOOL_ENV = {"merge_enabled", "full_transcript", "separate_vocals"}


def _coerce_env(attr: str, raw: str) -> Any:
    if attr in _FLOAT_ENV:
        return float(raw)
    if attr in _INT_ENV:
        return int(raw)
    if attr in _BOOL_ENV:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return raw


def resolve_config(
    cli_overrides: dict[str, Any] | None = None,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Resolve a Config from defaults <- toml <- env (.env / os.environ) <- CLI overrides."""
    cwd = cwd or os.getcwd()
    if env is None:
        init_toolchain(cwd)
        env = dict(os.environ)
    cfg = Config()

    # 1. TOML (project-level)
    toml_vals = load_toml(os.path.join(cwd, CONFIG_FILENAME))
    for k, v in toml_vals.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
            cfg._sources[k] = "toml"

    # 2. Environment
    env_map = {
        "model": "VT_MODEL", "chunk": "VT_CHUNK", "lang": "VT_LANG",
        "proxy": "VT_PROXY", "src": "VT_SRC", "tgt": "VT_TGT",
        "hf_cache_dir": "HF_HOME",
        "device": "VT_DEVICE", "compute_type": "VT_COMPUTE_TYPE",
        "engine": "VT_ENGINE", "persona": "VT_PERSONA",
        "merge_max_dur": "VT_MERGE_MAX_DUR", "merge_max_gap": "VT_MERGE_MAX_GAP",
        "merge_max_chars": "VT_MERGE_MAX_CHARS", "glossary": "VT_GLOSSARY",
        "source": "VT_SOURCE", "full_transcript": "VT_FULL_TRANSCRIPT",
        "separate_vocals": "VT_SEPARATE_VOCALS",
        "demucs_model": "VT_DEMUCS_MODEL",
    }
    for attr, envkey in env_map.items():
        if envkey in env and env[envkey]:
            setattr(cfg, attr, _coerce_env(attr, env[envkey]))
            cfg._sources[attr] = "env"
    # Standard HTTPS_PROXY/HTTP_PROXY fallback for proxy (only if VT_PROXY unset)
    if cfg.proxy is None:
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            if env.get(k):
                cfg.proxy = env[k]
                cfg._sources["proxy"] = "env"
                break

    # 3. CLI overrides (only non-None values)
    for k, v in (cli_overrides or {}).items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
            cfg._sources[k] = "cli"

    # Normalise lang="auto" -> None (auto-detect)
    if cfg.lang == "auto":
        cfg.lang = None

    return cfg