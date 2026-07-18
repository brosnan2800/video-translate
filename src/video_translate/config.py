"""Configuration resolution.

Priority (highest wins): CLI args > environment variables > project-level
`.video-translate.toml` > built-in defaults.

Only project-level config is supported (no user-level layer). device/compute_type
are intentionally NOT configurable — they are forced to cpu/int8 (CTranslate2 has
no AMD/Metal GPU support on this class of machine).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

from .proxy import DEFAULT_PROXY

CONFIG_FILENAME = ".video-translate.toml"
DEFAULT_HF_CACHE = os.path.expanduser("~/.cache/huggingface")


@dataclass
class Config:
    """Resolved runtime configuration."""

    model: str = "large-v3"
    chunk: float = 240.0
    lang: str = "en"
    proxy: str = DEFAULT_PROXY
    src: str = "en"
    tgt: str = "zh-CN"
    hf_cache_dir: str = DEFAULT_HF_CACHE
    _sources: dict[str, str] = field(default_factory=dict, repr=False)


def load_toml(path: str) -> dict[str, Any]:
    """Load a `.video-translate.toml` into a flat dict, or {} if missing.

    Sections [transcribe]/[translate]/[hf] are flattened into single keys.
    """
    if not os.path.exists(path) or tomllib is None:
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    flat: dict[str, Any] = {}
    for section in ("transcribe", "translate", "hf"):
        for k, v in (data.get(section) or {}).items():
            key = "hf_cache_dir" if (section == "hf" and k == "cache_dir") else k
            flat[key] = v
    return flat


def resolve_config(
    cli_overrides: dict[str, Any] | None = None,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Resolve a Config from defaults <- toml <- env <- CLI overrides."""
    cwd = cwd or os.getcwd()
    env = env if env is not None else dict(os.environ)
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
    }
    for attr, envkey in env_map.items():
        if envkey in env and env[envkey]:
            raw = env[envkey]
            setattr(cfg, attr, float(raw) if attr == "chunk" else raw)
            cfg._sources[attr] = "env"

    # 3. CLI overrides (only non-None values)
    for k, v in (cli_overrides or {}).items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
            cfg._sources[k] = "cli"

    return cfg
