# ACE Sidecar

**Local coding agent cost optimization & observability** — cut your Claude Code, Antigravity, and OpenAI Codex spend by **up to 50%** via real-time trajectory context compression and smart local model routing.

[![CI](https://github.com/ACE-Engineering/ace-sidecar/actions/workflows/ci.yml/badge.svg)](https://github.com/ACE-Engineering/ace-sidecar/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ace-sidecar.svg)](https://pypi.org/project/ace-sidecar/)
[![Python](https://img.shields.io/pypi/pyversions/ace-sidecar.svg)](https://pypi.org/project/ace-sidecar/)
[![License](https://img.shields.io/badge/License-AGPL_3.0-blue.svg)](LICENSE)

![ACE Sidecar dashboard](docs/assets/dashboard_preview.jpg)

**[How It Cuts Costs](#how-it-cuts-costs)** · **[Quickstart](#quickstart)** · **[Trajectory Compression](#1-trajectory-context-compression)** · **[Local Model Routing](#2-local-model-routing)** · **[Install](#install)** · **[Configuration](#configuration)** · **[Dashboard & Observability](#dashboard--observability)** · **[Who Builds This](#who-builds-this)** · **[License](#license)**

---

## How It Cuts Costs

Coding agents like Claude Code are token-heavy by design: every turn sends massive file reads, repetitive tool results, and bloated conversation histories. 

ACE Sidecar runs a **100% local proxy on loopback (`127.0.0.1`)** that actively reduces your API bill by **30% to 50%+** before requests ever hit the provider:

```
┌────────────────────────────────────────────────────────┐
│                    Claude Code CLI                     │
│                (or VS Code Extension)                  │
└───────────────────────────┬────────────────────────────┘
                            │ ANTHROPIC_BASE_URL=http://127.0.0.1:8787
                            ▼
┌────────────────────────────────────────────────────────┐
│                   ACE Local Sidecar                    │
│                 (`ace up` on 127.0.0.1)                │
├────────────────────────────────────────────────────────┤
│ 1. Trajectory Compression                              │
│    • Tool Output Truncation: Head + tail (2KB cap)     │
│    • Read Deduplication: SHA-256 digest pointers       │
│    • Prose Compaction: Strips conversational fluff     │
│    • Idle Optimizer: 300s TTL-aware cache compaction   │
│                                                        │
│ 2. Local Model Routing (Claude Family)                 │
│    • Exploration / Grep / Search ──> Claude 3.5 Haiku  │
│    • Code Authoring / Editing    ──> Claude 3.7 Sonnet │
│    • Sticky Session Anchoring    ──> Retains 90% cache │
│    • Data Sensitivity Gate       ──> Quarantines data  │
└───────────────────────────┬────────────────────────────┘
                            │ Relays optimized payload & adapts params
                            ▼
┌────────────────────────────────────────────────────────┐
│                Anthropic Upstream API                  │
│               (https://api.anthropic.com)              │
└────────────────────────────────────────────────────────┘
```

**100% locally hosted**: Zero telemetry phone-home, zero remote data upload, zero external cloud accounts. Everything executes on your workstation.

---

## Quickstart

### 1. Install ACE Sidecar
```bash
# Recommended via uv
uv tool install ace-sidecar

# Or via pip
pip install ace-sidecar
```

### 2. Launch the Sidecar
```bash
ace up
```
*Proxy listening on `http://127.0.0.1:8787` with real-time compression and routing enabled.*

### 3. Point Claude Code at the Sidecar
```bash
# In your terminal or ~/.bashrc / ~/.zshrc:
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787

# Configure optimized MCP tools for Claude Code:
ace setup-claude

# Start coding as normal!
claude
```

Open the local dashboard at **`http://127.0.0.1:8787/dashboard`** to see live token savings, cache hit ratios, and dollar reductions.

---

## Core Optimization Engines

### 1. Trajectory Context Compression
- **Tool Result Truncation**: Automatically caps sprawling terminal outputs and file dumps at 2KB, preserving the diagnostic head lines and summary tail lines while eliding redundant middle output.
- **Read Deduplication (SHA-256)**: When an agent re-reads an unchanged file across turns, the sidecar replaces the redundant content with a 120-byte digest pointer, saving tens of thousands of duplicate tokens.
- **Fact-Preserving Prose Compression**: Compresses assistant conversational chatter while preserving fenced code blocks, file paths, line numbers, and bash commands verbatim.
- **Prompt Cache TTL Compactor**: Anthropic prompt caches expire after 300 seconds of inactivity. When idle gap $> 300\text{s}$, the sidecar safely compacts older history into structured working state without breaking live turns.
- **Fast Stdio MCP Tools (`Search` & `Edit`)**: Built-in Model Context Protocol server that replaces full-file dumps with atomic line-slice reads and hunk edits.

### 2. Local Model Routing
- **Cost Arbitrage Across Task Phases**: High-volume, low-complexity agent turns (searching repository structure, running greps, reading file heads) are routed to **Claude 3.5 Haiku** ($0.80/1M vs $3.00/1M), saving **~73% on input costs** for exploration turns.
- **Session Stickiness (Cache Protection)**: Anthropic prompt caching offers a **90% discount** ($0.30/1M reads vs $3.00/1M). The router anchors to the destination model on Turn 1 and sticks to it, preventing mid-session model thrashing from invalidating the prompt cache.
- **Pluggable Escalation Triggers**:
  - **Explicit User Override**: Type `"use opus"` or `"switch to sonnet"` in your prompt to dynamically re-anchor.
  - **Complexity Jump**: Escalates to Claude 3.7 Sonnet with extended thinking when task difficulty jumps ($\Delta C \ge 0.40$).
  - **Consecutive Error Escalation**: Automatically escalates to flagship reasoning if the agent encounters $\ge 2$ consecutive test or tool failures.
  - **Data Sensitivity Gate**: Detects secrets (`.env`, `credentials`, AWS keys, JWTs) and routes them to strict compliance profiles.
- **Claude Family Wire Protocol Parity**: Parameter adaptation automatically strips `thinking` blocks and clamps `max_tokens` when down-routing to Haiku so Claude Code never encounters a `400 Bad Request`.

---

## Configuration (`~/.ace/routing.yaml`)

Routing and compression are fully customizable without touching Python code. The configuration defaults strictly to the tested **Claude model family** for 100% CLI stability:

```yaml
# ~/.ace/routing.yaml
version: "1.0"
active_profile: "balanced"

# Allowed candidate models. Add custom ARNs (e.g. AWS Bedrock / Vertex) or remove models.
allowed_models:
  - "claude-3-5-haiku-20241022"
  - "claude-3-5-sonnet-20241022"
  - "claude-3-7-sonnet-20250219"
  - "claude-3-opus-20240229"

profiles:
  balanced:
    exploration_model: "claude-3-5-haiku-20241022"
    authoring_model: "claude-3-7-sonnet-20250219"
    reasoning_model: "claude-3-7-sonnet-20250219"
    enable_thinking_on_reasoning: true
    thinking_budget: 4096

  economy:
    exploration_model: "claude-3-5-haiku-20241022"
    authoring_model: "claude-3-5-haiku-20241022"
    reasoning_model: "claude-3-7-sonnet-20250219"

  flagship:
    exploration_model: "claude-3-7-sonnet-20250219"
    authoring_model: "claude-3-7-sonnet-20250219"
    reasoning_model: "claude-3-7-sonnet-20250219"
```

### Routing CLI Commands
```bash
# View active routing profile and allowed models
ace routing list

# Dry-run test a routing decision on a prompt
ace routing test "where is main.py defined?"
# Result: Routes to claude-3-5-haiku-20241022 (exploration)

ace routing test "use opus to review distributed consensus design"
# Result: Routes to claude-3-opus-20240229 (explicit override)
```

---

## Dashboard & Observability

Even with optimization active, ACE Sidecar provides complete, transparent observability into your coding sessions:

- **Unified History**: Auto-discovers transcripts from `~/.claude/projects`, `~/.gemini/antigravity/brain`, and `~/.codex/sessions`.
- **Real Spend Against Provider Rates**: Tracks input, output, cache-read, and cache-write rates based on versioned provider catalogs.
- **Counterfactual Savings Rail**: Shows exact token and dollar savings recovered by context compression and model routing.
- **Where the Time Goes**: Wall-clock breakdown across model generation, tool execution, human think-time, and approval prompts.
- **Prometheus Metrics**: Live metrics exposed at `GET /metrics` for Grafana, Datadog, or OpenTelemetry.

---

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/messages` | Loopback proxy interceptor for Claude Code and coding agents |
| `GET /dashboard` | Visual web dashboard showing costs, savings, and session timelines |
| `GET /healthz` | Health check, active compression state, and routing status |
| `GET /api/stats` | JSON breakdown of saved bytes, saved tokens, and routed models |
| `GET /metrics` | Prometheus text exposition metrics |

---

## Who Builds This

ACE Sidecar is built by **[ACE Fleet](https://acefleet.dev)**.

ACE Fleet builds inference efficiency middleware for enterprises running production AI. While Fleet manages multi-tenant cloud traffic, **ACE Sidecar** is the open-source, local vertical designed specifically to optimize and compress individual developer coding agent workloads on their own machines.

| | **ACE Sidecar** (this repo) | **ACE Fleet** |
|---|---|---|
| **Scope** | Single developer workstation | Organization-wide inference fabric |
| **Workload** | Coding agents (Claude Code, Antigravity, Codex) | Production APIs, services, multi-agent systems |
| **Action** | **Acts locally**: Compresses context & routes models on loopback | **Acts centrally**: Dynamic routing, global cache, cloud fleets |
| **Privacy** | 100% local, zero data egress | Multi-tenant VPC / Private Cloud |
| **License** | Open source (AGPL-3.0) | Enterprise |

---

## License

[GNU Affero General Public License v3.0](LICENSE).
