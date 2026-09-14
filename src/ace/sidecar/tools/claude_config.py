"""ace.sidecar.tools.claude_config — Claude Code configuration generator.

Installs:
1. `.claude/settings.local.json`: Registers the local MCP server and disables wasteful vanilla tools.
2. `.claude/agents/ace-code.md`: Optimized agent prompt directing Claude Code to use
   `mcp__ace-tools__Search` and `mcp__ace-tools__Edit` while prohibiting verbose shell reads/writes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Optional

ACE_CODE_AGENT_PROMPT = """---
name: ace-code
description: Token-optimized ACE agent - file ops served by consolidated MCP facades (Search + Edit). Vanilla file tools are disabled.
model: inherit
disallowedTools: Read, Edit, Write, Grep, Glob, NotebookEdit, MultiEdit
---

You have two token-optimized MCP tools that replace Claude Code's vanilla file tools. The vanilla `Read` / `Edit` / `Write` / `Grep` / `Glob` / `MultiEdit` / `NotebookEdit` tools are **disabled**.

## Tool Selection

| Operation | Tool to Use |
|-----------|-------------|
| Read a file (or specific line range) | `mcp__ace-tools__Search` with `path`, `startLine`, `endLine` |
| Content search (regex) | `mcp__ace-tools__Search` with `contentRegex` |
| File discovery (glob) | `mcp__ace-tools__Search` with `pattern` |
| String replace in a file | `mcp__ace-tools__Edit` with `path`, `old_string`, `new_string` |
| Create a new file | `mcp__ace-tools__Edit` with `path`, `new_string` (omit `old_string`) |
| Full-file rewrite | `mcp__ace-tools__Edit` with `path`, `new_string`, `overwrite: true` |
| Build / test / shell execution | `Bash` |

## Context Economy Rules

1. **Never use Bash for reads**: Do **not** run `cat`, `head`, `tail`, `ls`, `grep`, `find`, `rg`, `sed -n`, or `awk`. Use `mcp__ace-tools__Search`. It extracts only relevant line slices, saving thousands of tokens per turn.
2. **Never use Bash for writes**: Do **not** run `sed -i`, `cat > file`, `echo > file`, `tee`, or in-place file clobbers. Use `mcp__ace-tools__Edit`.
3. Call the MCP tool names exactly: `mcp__ace-tools__Search` and `mcp__ace-tools__Edit`.
"""


def generate_settings_json(python_bin: Optional[str] = None) -> Dict:
    py = python_bin or sys.executable
    sidecar_src = str(Path(__file__).resolve().parents[2])
    return {
        "agent": "ace-code",
        "mcpServers": {
            "ace-tools": {
                "command": py,
                "args": ["-m", "ace.sidecar.cli", "mcp"],
                "env": {
                    "PYTHONPATH": sidecar_src,
                },
            }
        },
        "permissions": {
            "defaultMode": "acceptEdits",
            "allow": [
                "mcp__ace-tools__Search",
                "mcp__ace-tools__Edit",
            ],
        },
    }


def install_claude_config(workspace_dir: str = ".") -> Dict[str, str]:
    """Write .claude/settings.local.json and .claude/agents/ace-code.md in workspace."""
    root = Path(workspace_dir).resolve()
    claude_dir = root / ".claude"
    agents_dir = claude_dir / "agents"

    claude_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)

    settings_path = claude_dir / "settings.local.json"
    settings_data = generate_settings_json()
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(settings_data, fh, indent=2)

    agent_path = agents_dir / "ace-code.md"
    with open(agent_path, "w", encoding="utf-8") as fh:
        fh.write(ACE_CODE_AGENT_PROMPT.strip() + "\n")

    return {
        "settings_path": str(settings_path),
        "agent_path": str(agent_path),
        "status": "ok",
    }
