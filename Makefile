.PHONY: help venv install install-dev setup test test-all lint doctor clean

PY := python
PIP := python -m pip

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
	else \
		echo "[setup] uv not found — falling back to pip (see TOOLCHAIN.md §3.1)"; \
		$(MAKE) install; \
	fi
	$(PY) -m video_translate.cli setup

test:
	$(PY) -m pytest -q

test-all:
	$(PY) -m pytest -q -m ""

doctor:
	$(PY) -m video_translate.cli doctor

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info build dist
