# Changelog

All notable changes to `ace-sidecar` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed
- Ship `data/model_market` in the wheel as package data at `ace/data/model_market`. The catalog sits outside `src/`, so hatchling never picked it up and the published wheel contained code only; `_resolve_catalog_root` then fell through to its cwd-relative last resort, and an installed `ace up` launched from anywhere but the repo root found no rates and recorded every turn as UNPRICED at $0.00. `feeds/` stays out of the wheel — 2.9M of the 3.1M catalog, and nothing in the shipped package reads it.
- Explicit `/dashboard` routing and interactive button CSS, so agent-environment navigation responds to clicks.

---

## [0.1.0] - 2026-08-15

First release on PyPI.

### Added
- Initial open-source release of `ace-sidecar`.
- Standalone FastAPI sidecar server with `/v1/messages` relay, `/dashboard`, `/healthz`, and `/api/stats`.
- Bundled local gateway modules (`messages`, `messages_auth`, `pricing`, `local_store`, `branding`).
- CLI interface (`ace up`, `ace env`).
- Prometheus `/metrics` exposition endpoint (`text/plain; version=0.0.4`) for local telemetry scraping.
- E2E test suite in `scripts/e2e_test.py` covering live server routes, healthz, stats, and Prometheus metrics.
- Automated GitHub Actions PyPI publishing workflow (`.github/workflows/publish.yml`) with Trusted Publisher OIDC support.
- Open-source community issue templates (`bug_report.md`, `feature_request.md`) and pull request template (`PULL_REQUEST_TEMPLATE.md`).
- AGPL-3.0 license, `pyproject.toml` packaging, Homebrew formula (`Formula/ace-sidecar.rb`), and GitHub Actions CI workflow (`.github/workflows/ci.yml`).
- Agent tab filtering across Claude Code, Antigravity, and All Agents; recent telemetry shows 30 turns.

[Unreleased]: https://github.com/ACE-Engineering/ace-sidecar/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ACE-Engineering/ace-sidecar/releases/tag/v0.1.0
