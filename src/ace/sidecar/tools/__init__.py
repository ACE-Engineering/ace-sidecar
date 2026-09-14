"""ace.sidecar.tools — High-efficiency local MCP tools & Claude Code configuration."""

from __future__ import annotations

from ace.sidecar.tools.claude_config import install_claude_config
from ace.sidecar.tools.mcp_server import run_mcp_server

__all__ = ["install_claude_config", "run_mcp_server"]
