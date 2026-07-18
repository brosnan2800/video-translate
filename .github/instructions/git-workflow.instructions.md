# Git workflow instructions

## Repository
- Private repository. Owner may flip to public later.
- No GitHub Actions CI (by decision); verification is local `make test`.

## Before every commit
1. `make test` is green (49 fast tests).
2. If behavior changed, the matching `docs/specs/` file is updated in the same
   commit. If output format changed, `docs/golden/` is updated deliberately with a
   note in the commit message.
3. No large binaries staged — videos, model caches, and `outputs/` are gitignored.

## Commit style
- Small, focused commits. Imperative subject (e.g. "Add resume test for translate").
- Reference the spec/ADR when relevant (e.g. "per ADR-002").

## Not tracked (see .gitignore)
- `videos/`, `outputs/`, `.cache/`, `.venv/`, `.video-translate.toml`,
  `__pycache__/`, `*.egg-info`.
- `docs/golden/` **is** tracked — it is the regression baseline, not output.

## Tagging
- Tag releases as `vX.Y.Z` (current: `v1.0.0` — faithful migration of the 3-stage
  pipeline; v2 will add segment-merge and LLM translation).
