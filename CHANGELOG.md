# Changelog

All notable changes to `ace-sidecar` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.1] - 2026-08-11

### Added
- Prometheus `/metrics` exposition format endpoint (`text/plain; version=0.0.4`) for local telemetry scraping.
- E2E test suite in `scripts/e2e_test.py` covering live server routes, healthz, stats, and Prometheus metrics.
- Automated GitHub Actions PyPI publishing workflow (`.github/workflows/publish.yml`) with Trusted Publisher OIDC support.
- Open-source community issue templates (`bug_report.md`, `feature_request.md`) and Pull Request template (`PULL_REQUEST_TEMPLATE.md`).

### Fixed
- Passed `agent` parameter to `build()` in `dashboard` and `report` endpoints to fix agent tab filtering (Claude Code vs. Antigravity vs. All Agents).
- Updated recent telemetry turn display limit to 30.
- Enhanced Section 10 (ACE About & Contact section) UI with modern glassmorphism cards, badges, and hover animations.

---

## [0.1.0] - 2026-08-07

### Added
- Initial open-source release of `ace-sidecar`.
- Standalone FastAPI sidecar server with `/v1/messages` relay, `/dashboard`, `/healthz`, and `/api/stats`.
- Bundled local gateway modules (`messages`, `messages_auth`, `pricing`, `local_store`, `branding`).
- CLI interface (`ace up`, `ace env`).
- AGPL-3.0 License, `pyproject.toml` packaging, Homebrew formula (`Formula/ace-sidecar.rb`), and GitHub Actions CI workflow (`.github/workflows/ci.yml`).
