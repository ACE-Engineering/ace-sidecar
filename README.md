# ACE Sidecar

Local developer observability sidecar, transcript mining, and workflow skill miner for Claude Code & Antigravity.

[![CI](https://github.com/ACE-Engineering/ace-sidecar/actions/workflows/ci.yml/badge.svg)](https://github.com/ACE-Engineering/ace-sidecar/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ace-sidecar.svg)](https://pypi.org/project/ace-sidecar/)
[![License](https://img.shields.io/badge/License-AGPL_3.0-blue.svg)](LICENSE)

---

## Quickstart

```bash
pip install ace-sidecar
ace up
```

Once running, point your developer tools to the local ACE sidecar:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
```

---

## Features

- **Local Proxy & Observability**: Intercepts local Claude Code and agent requests on loopback (`127.0.0.1:8787`) with zero cloud overhead.
- **Privacy-First Zero-Trust Architecture**: Your API keys and full request bodies remain on your machine by default.
- **Transcript Mining**: Analyzes Antigravity and Claude Code transcript logs to calculate real-world context utilization, cache savings, and token efficiency.
- **Workflow Skill Miner**: Automatically detects repetitive multi-turn command patterns and proposes reusable agent skills.
- **Interactive Developer Dashboard**: Access local metrics and reports directly at `http://127.0.0.1:8787/dashboard`.
- **Prometheus Metrics Exporter**: Native `/metrics` endpoint serving standard Prometheus text exposition format for scraping into Prometheus, Grafana, OTel, and Datadog (see [docs/PROMETHEUS_METRICS.md](docs/PROMETHEUS_METRICS.md)).


---

## Usage & CLI Reference

### `ace up`
Launches the local proxy sidecar service.

```bash
# Start proxy with default settings (port 8787, 127.0.0.1)
ace up

# Specify Anthropic API key explicitly
ace up --key sk-ant-...

# Run without a stored key (relaying caller's credentials)
ace up --no-key

# Specify custom port or bind address
ace up --port 8788 --host 127.0.0.1

# Enable local request capture for offline inspection
ace up --capture
```

### `ace env`
Prints shell export configuration for easy terminal setup:

```bash
eval "$(ace env)"
```

---

## Configuration

Configuration values are resolved in the following precedence order:
1. **CLI Flags** (e.g. `--port`, `--no-key`)
2. **Configuration File** (`~/.ace/config.json`)
3. **Environment Variables** (`ANTHROPIC_API_KEY`, `ACE_SIDECAR_PORT`, etc.)
4. **Built-in Defaults**

Sample `~/.ace/config.json`:
```json
{
  "port": 8787,
  "no_key": true,
  "log_level": "info",
  "antigravity_dir": "~/.gemini/antigravity/brain"
}
```

---

## Development & Testing

### Installation from Source

```bash
# Clone the repository
git clone https://github.com/ACE-Engineering/ace-sidecar.git
cd ace-sidecar

# Create local virtual environment and install editable package
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

### Running Tests

```bash
# Run 33 unit tests
pytest

# Run E2E server route verification
python scripts/e2e_test.py
```

---

## License

Distributed under the terms of the [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE).
