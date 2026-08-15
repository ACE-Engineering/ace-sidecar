# ACE Sidecar

Local developer observability for AI coding agents — see what your Claude Code and Antigravity sessions actually cost, on your own machine.

[![CI](https://github.com/ACE-Engineering/ace-sidecar/actions/workflows/ci.yml/badge.svg)](https://github.com/ACE-Engineering/ace-sidecar/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ace-sidecar.svg)](https://pypi.org/project/ace-sidecar/)
[![Python](https://img.shields.io/pypi/pyversions/ace-sidecar.svg)](https://pypi.org/project/ace-sidecar/)
[![License](https://img.shields.io/badge/License-AGPL_3.0-blue.svg)](LICENSE)

![ACE Sidecar dashboard](docs/assets/dashboard_preview.jpg)

---

## What is ACE Sidecar?

**ACE Sidecar** runs a small proxy on your own machine, in front of the model provider. Every turn your coding agent makes passes through it, and it records what that turn cost — tokens in and out, how much came from cache, how long you waited.

It reads your existing agent transcripts too (`~/.claude/projects`, `~/.gemini/antigravity/brain`), so it has history to show the moment you start it, not after a week of collecting.

**Nothing leaves your machine.** No account, no telemetry upload, no cloud dependency. Metrics live in a local SQLite file you can delete at any time.

ACE Sidecar is built by [ACE Fleet](https://acefleet.dev), a cost-saving proxy for companies scaling AI applications. The sidecar is the coding-agent slice of that work, open-sourced on its own.

---

## Requirements

| | |
|---|---|
| **Python** | **3.12 or newer** — this is the one hard requirement |
| **Operating system** | macOS, Linux, or Windows |
| **Anthropic account** | A Claude subscription *or* an API key — either works |
| **Network** | Only to reach the model provider; the sidecar itself never phones home |
| **Admin rights** | Not needed — everything installs into your user directory |

### Check your Python version first

```bash
python3 --version
```

If that prints **3.12.0 or higher**, you are ready. If it prints 3.9, 3.10, or 3.11 — or "command not found" — install a newer Python:

- **macOS** — `brew install python@3.12`, or download from [python.org](https://www.python.org/downloads/)
- **Ubuntu / Debian** — `sudo apt install python3.12`
- **Fedora / RHEL** — `sudo dnf install python3.12`
- **Windows** — download from [python.org](https://www.python.org/downloads/) and tick *"Add Python to PATH"*
- **Any system** — [`uv`](https://docs.astral.sh/uv/) can fetch a Python for you: `uv python install 3.12`

> **The one error people hit.** If `pip install ace-sidecar` says
> `Could not find a version that satisfies the requirement ace-sidecar (from versions: none)`,
> your Python is older than 3.12. The message is misleading — the package exists; your interpreter is just too old. Older versions of pip report it this way instead of naming the real problem. See [Install troubleshooting](#install-troubleshooting).

---

## Install

ACE Sidecar is a command-line tool, so the cleanest installs give it its own isolated environment. Pick whichever line matches what you already have.

### Recommended — `uv` or `pipx`

```bash
uv tool install ace-sidecar      # https://docs.astral.sh/uv/
```

```bash
pipx install ace-sidecar         # https://pipx.pypa.io/
```

Either one puts an `ace` command on your PATH, keeps its dependencies away from your other projects, and works without admin rights.

### With plain `pip`

Use a virtual environment so the install cannot collide with anything else on your system:

```bash
python3.12 -m venv ~/.venvs/ace
source ~/.venvs/ace/bin/activate        # Windows: ~\.venvs\ace\Scripts\activate
pip install ace-sidecar
```

Installing into your *system* Python with `pip install --user ace-sidecar` also works, provided that Python is 3.12+.

### Verify it worked

```bash
ace --help
```

### Install troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Could not find a version that satisfies the requirement ace-sidecar` | Your Python is older than 3.12 | Install Python 3.12+, then reinstall |
| `ace: command not found` after installing | The install directory is not on your PATH | `uv tool update-shell`, or `pipx ensurepath`, then open a new terminal |
| `ace` runs an old version after upgrading | An earlier copy is shadowing it on PATH | `which -a ace` shows every copy; remove the ones you do not want |
| Permission errors during install | Installing into a system directory | Use `uv`, `pipx`, or a virtual environment instead of `sudo` |

---

## Quickstart

**1. Start the sidecar.**

```bash
ace up --no-key
```

`--no-key` is the right flag for **Claude subscription users** — Claude Code sends its own credential and the sidecar simply relays it. If you pay by API key instead, use `ace up --key sk-ant-...` or set `ANTHROPIC_API_KEY` in your environment.

> Running bare `ace up` with no key configured will stop and explain your options rather than starting.

**2. Point your agent at it**, in the same terminal you will run your agent from:

```bash
eval "$(ace env)"      # exports ANTHROPIC_BASE_URL for the port you are running (default 8787)
```

**3. Open the dashboard.**

```
http://127.0.0.1:8787/dashboard
```

Now use your coding agent as normal. Turns show up live, and your existing transcript history is already there.

To make `--no-key` permanent, put it in `~/.ace/config.json`:

```json
{ "no_key": true }
```

---

## Features

### Unified view across agents

Claude Code and Antigravity land in one place, with per-agent cost, sessions, turns and models. Select a single agent and the whole page scopes to it.

![Agent breakdown and fleet metrics](docs/assets/dashboard_preview.jpg)

### Real spend, against real published prices

Cost is computed per turn from a versioned rate catalog — input, output, cache-read, and the derived cache-write rates — with the source and date it was checked. Cache savings are shown as a counterfactual, so you can see what caching is already earning you.

![Spend and the rate card](docs/assets/spend_and_rate_card.jpg)

### Recommendations tied to a measured threshold

Each recommendation fires off a number from your own transcripts, and carries what it would save, what it would cost, and the risk of doing it.

![Recommendations](docs/assets/recommendations.jpg)

### Workflow skill miner

Repeated command sequences in your transcripts are detected and turned into reusable `SKILL.md` rules, installable into `.agents/skills/<id>/` with one click.

![Workflow skill miner](docs/assets/workflow_skills.jpg)

### Where the time actually goes

Wall clock split into model generating, tool execution, human composing, and idle — including how long you spent parked on approval prompts.

![Session time breakdown](docs/assets/session_time.jpg)

### Prometheus exporter

15 metrics in standard Prometheus text exposition format at `GET /metrics`, ready to scrape into Prometheus, Grafana Alloy, OpenTelemetry Collector, VictoriaMetrics, or Datadog. See [docs/PROMETHEUS_METRICS.md](docs/PROMETHEUS_METRICS.md).

![Prometheus exporter](docs/assets/prometheus_exporter.jpg)

---

## CLI reference

### `ace up`

Runs the sidecar. Defaults to `127.0.0.1:8787`.

| Flag | What it does |
|---|---|
| `--no-key` | Relay whatever credential the caller sends — the normal case for subscription Claude Code |
| `--key sk-ant-...` | Pay with this Anthropic API key |
| `--port 8788` | Bind a different port |
| `--host 127.0.0.1` | Bind a different address (loopback only unless `--allow-remote`) |
| `--capture [DIR]` | Record request bodies locally for offline analysis |
| `--telemetry-db PATH` | Use a different SQLite file |
| `--no-telemetry` | Do not record anything |
| `--log-level LEVEL` | `critical` … `trace` |
| `--help` | Every option, with its config key and environment variable |

### `ace env`

Prints the export line for your current configuration, so the port always matches what you are actually running:

```bash
eval "$(ace env)"
```

---

## Configuration

Settings resolve in this order — the first one that specifies a value wins:

1. **CLI flags** — `--port`, `--no-key`, …
2. **Config file** — `~/.ace/config.json`
3. **Environment variables** — `ANTHROPIC_API_KEY`, `ACE_SIDECAR_PORT`, …
4. **Built-in defaults**

```json
{
  "no_key": true,
  "port": 8787,
  "log_level": "warning"
}
```

### Where your data lives

| Path | What it holds |
|---|---|
| `~/.ace/telemetry.db` | Turn telemetry, local SQLite, never uploaded |
| `~/.ace/config.json` | Your settings |
| `~/.claude/projects` | Claude Code transcripts — **read only** |
| `~/.gemini/antigravity/brain` | Antigravity transcripts — **read only** |

Delete `~/.ace/` to remove everything the sidecar has recorded.

---

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/messages` | The relay your agent talks to |
| `GET /dashboard` | The dashboard shown above |
| `GET /healthz` | Liveness and configuration state, without leaking your key |
| `GET /api/stats` | The dashboard's numbers as JSON |
| `GET /metrics` | Prometheus text exposition |

The sidecar binds loopback and refuses non-local callers. Binding a public address requires `--allow-remote`, and even then unproxied-local-caller checks still apply.

---

## Development

```bash
git clone https://github.com/ACE-Engineering/ace-sidecar.git
cd ace-sidecar

python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

```bash
pytest                        # 40 unit tests
python scripts/e2e_test.py    # live server route verification
```

Running from a checkout picks up `data/model_market/` in the working tree, so catalog edits take effect without reinstalling.

---

## License

Distributed under the [GNU Affero General Public License v3.0](LICENSE).
