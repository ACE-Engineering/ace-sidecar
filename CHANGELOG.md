# Changelog

All notable changes to `ace-sidecar` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [0.1.1] - 2026-08-15

Fixes the packaging bug that made 0.1.0 report every turn as free.

### Fixed
- **Ship `data/model_market` in the wheel**, as package data at `ace/data/model_market`. The catalog sits outside `src/`, so hatchling never picked it up and the published wheel contained code only; `_resolve_catalog_root` then fell through to its cwd-relative last resort, and an installed `ace up` launched from anywhere but the repo root found no rates and recorded **every turn as UNPRICED at $0.00**. Anyone who installed 0.1.0 from PyPI was affected. `feeds/` stays out of the wheel — 2.9M of the 3.1M catalog, and nothing in the shipped package reads it.
- **Mask the home directory everywhere a path is rendered.** The dashboard printed absolute paths in the installed-skills locations, the session list (transcript paths, working directories and opening prompts) and the footer, each of which publishes the reader's account name — and, in the session list, the names of their private repositories. Masking existed but was inlined at the one call site that had been noticed, so the page was half-masked. Also covers the dash-encoded form Claude Code uses for transcript filenames, where `/Users/alex` appears as `-Users-alex`.
- **Selecting an agent now scopes the whole page.** Section 00 rendered a card for every agent regardless of the filter, so a non-selected agent appeared with its numbers zeroed rather than being removed. Session file listings are scoped the same way.
- **Rail navigation lands on the right sections.** "Common questions" pointed at `#s11` and arrived at Live Stream, because the questions section was itself numbered 10 and collided with Session Time, leaving two `id='s10'` on one page. The tail is renumbered to follow document order — Prometheus 13→12, Common questions 10→13, About 12→14 — and the rail, the About CTA and three stale docstrings updated to match.
- Explicit `/dashboard` routing and interactive button CSS, so agent-environment navigation responds to clicks.
- Removed two citations of internal analysis documents that do not exist in this repository, one in the § 10 prose and one in the § 01 subtitle.

### Performance
- **Agent toggles no longer rebuild the page.** `insights.build` splits into a cacheable transcript-derived half, memoised on a key carrying the transcript fingerprint, so appending a turn retires the entries derived from older transcripts and a stale payload cannot outlive its data. Live telemetry counters stay outside the cache and are re-read per request. Measured over HTTP against a real transcript directory, a repeat toggle goes from **4.0s to 1.2ms**.
- `sessions()` no longer re-walks and stats every transcript on cache hits (~0.35s); a short freshness window lets a burst of toggles reuse the fingerprint.
- `skill_miner._scan_transcript_logs` is memoised. It parses every line of every transcript — six figures of `json.loads` — and ignores the sessions it is handed, so it returned an identical answer for each agent tab and was rerun for each. First visit to the Antigravity tab: **1.45s to 0.35s**.

### Added
- The dashboard's About section introduces **ACE Fleet** and places the sidecar as one open-sourced vertical of it, rather than describing only itself.
- README: requirements that state the Python 3.12+ floor and the error older interpreters produce, a working quickstart, a table of contents, per-capability screenshots, and a section contrasting ACE Sidecar with ACE Fleet.
- Regression tests for the build cache and for path masking (42 tests, up from 33).

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

[Unreleased]: https://github.com/ACE-Engineering/ace-sidecar/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/ACE-Engineering/ace-sidecar/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ACE-Engineering/ace-sidecar/releases/tag/v0.1.0
