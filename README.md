# ACE Sidecar

Local developer observability for AI coding agents — see what your Claude Code and Antigravity sessions actually cost.

[![CI](https://github.com/ACE-Engineering/ace-sidecar/actions/workflows/ci.yml/badge.svg)](https://github.com/ACE-Engineering/ace-sidecar/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ace-sidecar.svg)](https://pypi.org/project/ace-sidecar/)
[![Python](https://img.shields.io/pypi/pyversions/ace-sidecar.svg)](https://pypi.org/project/ace-sidecar/)
[![License](https://img.shields.io/badge/License-AGPL_3.0-blue.svg)](LICENSE)

![ACE Sidecar dashboard](docs/assets/dashboard_preview.jpg)

**[What it does](#what-it-does)** · **[Requirements](#requirements)** · **[Install](#install)** · **[Quickstart](#quickstart)** · **[Features](#features)** · **[Configuration](#configuration)** · **[Endpoints](#endpoints)** · **[Development](#development)** · **[License](#license)**

---

## What it does

ACE Sidecar runs a proxy on your machine in front of the model provider, recording what each turn costs — tokens in and out, what came from cache, how long you waited. It also reads your existing transcripts (`~/.claude/projects`, `~/.gemini/antigravity/brain`), so there is history to show from the first run.

Nothing leaves your machine: no account, no upload. Metrics live in a local SQLite file you can delete.

Built by [ACE Fleet](https://acefleet.dev), a cost-saving proxy for companies scaling AI applications. This sidecar is the coding-agent slice of that work, open-sourced on its own.

---

## Requirements

- **Python 3.12+** — the one hard requirement. Check with `python3 --version`.
- macOS, Linux, or Windows. No admin rights needed.
- A Claude subscription or an Anthropic API key.

Need a newer Python? `brew install python@3.12` (macOS), `sudo apt install python3.12` (Debian/Ubuntu), `sudo dnf install python3.12` (Fedora), [python.org](https://www.python.org/downloads/) (Windows), or `uv python install 3.12` (anywhere).

---

## Install

```bash
uv tool install ace-sidecar     # or: pipx install ace-sidecar
```

Both give `ace` its own isolated environment and put it on your PATH. With plain pip, use a virtual environment:

```bash
python3.12 -m venv ~/.venvs/ace && source ~/.venvs/ace/bin/activate
pip install ace-sidecar
```

**If install fails with `Could not find a version that satisfies the requirement ace-sidecar`**, your Python is older than 3.12. The message blames the package, but the package is fine — the interpreter is too old.

**If `ace: command not found` after installing**, run `uv tool update-shell` or `pipx ensurepath`, then open a new terminal.

---

## Quickstart

```bash
ace up
```

Then point your agent at it and open the dashboard:

```bash
eval "$(ace env)"                       # exports ANTHROPIC_BASE_URL
open http://127.0.0.1:8787/dashboard
```

Use your coding agent as normal — turns appear live, with your transcript history already loaded.

On a Claude subscription, start with `ace up --no-key` (Claude Code sends its own credential and the sidecar relays it), or put `{"no_key": true}` in `~/.ace/config.json`. `ace up --help` lists every flag.

---

## Features

**Unified view across agents.** Claude Code and Antigravity in one place, with per-agent cost, sessions, turns and models. Pick one agent and the page scopes to it.

**Real spend against published prices.** Per-turn cost from a versioned rate catalog — input, output, cache-read, and derived cache-write rates — with the source and the date it was checked. Cache savings shown as a counterfactual.

![Spend and rate card](docs/assets/spend_and_rate_card.jpg)

**Recommendations off a measured threshold.** Each one fires on a number from your own transcripts and carries its saving, its cost, and its risk.

![Recommendations](docs/assets/recommendations.jpg)

**Workflow skill miner.** Repeated command sequences become reusable `SKILL.md` rules, installable into `.agents/skills/<id>/` in one click.

![Workflow skill miner](docs/assets/workflow_skills.jpg)

**Where the time goes.** Wall clock split across model generating, tool execution, human composing, and idle — including time parked on approval prompts.

![Session time](docs/assets/session_time.jpg)

**Prometheus exporter.** 15 metrics in standard text exposition format at `GET /metrics`, for Prometheus, Grafana Alloy, OpenTelemetry Collector, VictoriaMetrics, or Datadog. See [docs/PROMETHEUS_METRICS.md](docs/PROMETHEUS_METRICS.md).

![Prometheus exporter](docs/assets/prometheus_exporter.jpg)

---

## Configuration

Settings resolve in order: **CLI flags** → **`~/.ace/config.json`** → **environment variables** → **defaults**.

```json
{ "no_key": true, "port": 8787, "log_level": "warning" }
```

| Path | Holds |
|---|---|
| `~/.ace/telemetry.db` | Turn telemetry — local SQLite, never uploaded |
| `~/.ace/config.json` | Your settings |
| `~/.claude/projects`, `~/.gemini/antigravity/brain` | Agent transcripts — read only |

Delete `~/.ace/` to remove everything recorded.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/messages` | The relay your agent talks to |
| `GET /dashboard` | The dashboard above |
| `GET /healthz` | Liveness and config state, without leaking your key |
| `GET /api/stats` | The dashboard's numbers as JSON |
| `GET /metrics` | Prometheus exposition |

Binds loopback and refuses non-local callers; a public bind needs `--allow-remote`.

---

## Development

```bash
git clone https://github.com/ACE-Engineering/ace-sidecar.git && cd ace-sidecar
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"

pytest                        # 40 unit tests
python scripts/e2e_test.py    # live route verification
```

---

## License

[GNU Affero General Public License v3.0](LICENSE).
