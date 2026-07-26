"""Glossary loading for consistent character / proper-noun translation (Spec 14).

A glossary maps source terms to preferred Chinese translations so the same name
is rendered consistently across episodes. It is *soft* guidance injected into the
translation persona — not a forced find/replace (ADR-010).

Supported formats:
  - txt: one mapping per line, ``src => tgt`` or ``src: tgt``; blank / ``#`` ignored.
  - json: ``{"src": "tgt", ...}``.

``load_glossary`` returns a formatted context string (for the persona), or None
when the path is missing / empty / unparseable.
"""
from __future__ import annotations

import json
from typing import Any


def load_glossary(path: str | None) -> str | None:
    """Load a glossary file and return a formatted context string, or None."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    entries = _parse(raw, path)
    if not entries:
        return None
    lines = "\n".join(f"- {src} => {tgt}" for src, tgt in entries)
    return "术语表（翻译时请优先采用以下译名，保持全片一致）：\n" + lines


def _parse(raw: str, path: str) -> list[tuple[str, str]]:
    if path.endswith(".json"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            return [(str(k), str(v)) for k, v in data.items()]
        return []
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            s, t = line.split("=>", 1)
        elif ":" in line:
            s, t = line.split(":", 1)
        else:
            continue
        s, t = s.strip(), t.strip()
        if s and t:
            out.append((s, t))
    return out
