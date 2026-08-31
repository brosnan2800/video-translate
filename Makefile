# Deterministic command entry (ADR-029 / Spec 23): every target runs through
# `uv run`, which always resolves the project .venv regardless of PATH. Never
# call bare `python` / `video-translate` — a stray global interpreter (e.g.
# F:\Python311) may shadow the venv on PATH and silently lose deps (whisperx).
PY  := uv run python
PIP := uv run python -m pip

.PHONY: help venv install install-dev setup test test-all doctor clean

help:
	@echo "Targets:"
	@echo "  venv         Create .venv (Python 3.13)"
	@echo "  install      Install runtime deps into .venv (pip fallback)"
	@echo "  install-dev  Install runtime + dev deps into .venv (pip fallback)"
	@echo "  setup        ONE-SHOT (recommended): uv sync deps + pre-download Whisper model"
	@echo "  test         Run unit + contract tests (skips @slow)"
	@echo "  test-all     Run ALL tests including @slow e2e"
	@echo "  doctor       Run environment self-check"
	@echo "  clean        Remove caches and build artifacts"

venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip

# pip fallback path (only if `uv` is unavailable). NOT the canonical path.
install: venv
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

install-dev: venv
	$(PIP) install -r requirements-dev.txt
	$(PIP) install -e .

# Canonical one-shot bootstrap: uv sync (reproducible via uv.lock) + model
# weights. Falls back to pip only if uv is not installed. Deterministic, no
# scattered caches.
setup:
	@if command -v uv >/dev/null 2>&1; then \
		echo "[setup] uv detected — using uv sync (reproducible, uv.lock pinned)"; \
		uv sync --extra dev || uv sync; \
		uv run python -m video_translate.cli setup; \
	else \
		echo "[setup] uv not found — run setup manually with the venv python (see TOOLCHAIN.md §2.5):"; \
		echo "        .venv/Scripts/python.exe -m video_translate.cli setup"; \
	fi

test:
	$(PY) -m pytest -q

test-all:
	$(PY) -m pytest -q -m ""

doctor:
	$(PY) -m video_translate.cli doctor

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info build dist

# ----------------------- gate-kit pipeline (Gap B) -----------------------
# 用法见 docs/PIPELINE.md。所有命令经 uv run，绝不裸 python/video-translate。
# 注意：本地 PY := uv run python 保留给单测/脚本；gate-kit 用到的
# `uv run video-translate` 入口单独定义为 VT，避免覆盖。
VT   := uv run video-translate
DECIDE := $(PY) gates/preflight_decision.py
GATE  := $(PY) gates/gate.py
CKPT  := $(PY) gates/checkpoint.py
VGT   := $(PY) gates/verify_gate.py

.PHONY: preflight decide-show decide confirm transcribe check-translate generate verify finish gate verify-fix verify-approve ci

preflight:
	$(VT) doctor
	$(DECIDE) propose --base "$(BASE)" --video "$(VIDEO)"

decide-show:
	$(DECIDE) show --base "$(BASE)"

decide:
	$(DECIDE) set --base "$(BASE)" --item "$(ITEM)" --value "$(VALUE)"

confirm:
	$(DECIDE) confirm --base "$(BASE)"
	$(CKPT) complete preflight --base "$(BASE)"

transcribe:
	$(DECIDE) assert --base "$(BASE)" --video "$(VIDEO)"
	$(VT) run "$(VIDEO)" $$($(DECIDE) render-flags --base "$(BASE)")

check-translate:
	$(GATE) content --base "$(BASE)"

generate:
	$(VT) generate --segments "videos/$(BASE).segments_en.json" \
	                 --zh "videos/$(BASE).zh_segments.json" \
	                 --outdir videos --base "$(BASE)" --video "$(VIDEO)"

verify:
	$(GATE) all --base "$(BASE)" --video "$(VIDEO)"

finish:
	$(DECIDE) assert --base "$(BASE)" --video "$(VIDEO)"
	$(CKPT) complete transcribe --base "$(BASE)"
	$(GATE) content --base "$(BASE)"
	$(VT) generate --segments "videos/$(BASE).segments_en.json" \
	               --zh "videos/$(BASE).zh_segments.json" \
	               --outdir videos --base "$(BASE)" --video "$(VIDEO)"
	$(VGT) run --base "$(BASE)" --video "$(VIDEO)"
	$(CKPT) complete verify --base "$(BASE)"

gate:
	$(GATE) all --base "$(BASE)" --video "$(VIDEO)"

verify-fix:
	$(VGT) run --base "$(BASE)" --video "$(VIDEO)" --auto-loop

verify-approve:
	$(CKPT) approve verify --base "$(BASE)"

ci:
	$(PY) -m pytest -q
