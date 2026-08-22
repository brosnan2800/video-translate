.PHONY: help venv install install-dev setup test test-all lint doctor clean

PY := python
PIP := python -m pip

help:
	@echo "Targets:"
	@echo "  venv         Create .venv (Python 3.13)"
	@echo "  install      Install runtime deps into .venv"
	@echo "  install-dev  Install runtime + dev deps into .venv"
	@echo "  setup        ONE-SHOT: install deps + pre-download Whisper model (recommended first step)"
	@echo "  test         Run unit + contract tests (skips @slow)"
	@echo "  test-all     Run ALL tests including @slow e2e"
	@echo "  doctor       Run environment self-check"
	@echo "  clean        Remove caches and build artifacts"

venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip

install: venv
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

install-dev: venv
	$(PIP) install -r requirements-dev.txt
	$(PIP) install -e .

# One-shot bootstrap: deps + model weights. Deterministic, no scattered caches.
setup: install
	$(PY) -m video_translate.cli setup

test:
	$(PY) -m pytest -q

test-all:
	$(PY) -m pytest -q -m ""

doctor:
	$(PY) -m video_translate.cli doctor

clean:
	rm -rf .pytest_cache **/__pycache__ *.egg-info build dist
