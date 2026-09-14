"""Unit tests for ace.sidecar.tools modules."""

from __future__ import annotations

import json
from pathlib import Path

from ace.sidecar.tools.claude_config import install_claude_config
from ace.sidecar.tools.mcp_server import handle_rpc_request, tool_edit, tool_search


def test_tool_search_targeted_read(tmp_path: Path):
    test_file = tmp_path / "sample.py"
    lines = [f"line_{i} = {i}" for i in range(1, 21)]
    test_file.write_text("\n".join(lines), encoding="utf-8")

    # Read slice lines 5 to 10
    result = tool_search(path=str(test_file), startLine=5, endLine=10)
    assert "lines 5-10 of 20" in result
    assert "line_5" in result
    assert "line_10" in result
    assert "line_1 " not in result
    assert "line_15" not in result


def test_tool_search_regex(tmp_path: Path):
    f1 = tmp_path / "a.py"
    f1.write_text("TARGET_CONST = 42\nother = 1\n", encoding="utf-8")

    result = tool_search(path=str(tmp_path), contentRegex=r"TARGET_\w+")
    assert "TARGET_CONST" in result
    assert "Found 1 match" in result


def test_tool_edit_string_replace_and_create(tmp_path: Path):
    target = tmp_path / "module.py"

    # Create
    res_create = tool_edit(path=str(target), new_string="def run(): pass\n")
    assert "created file" in res_create.lower()
    assert target.exists()

    # Replace
    res_edit = tool_edit(
        path=str(target),
        old_string="pass",
        new_string="return True",
    )
    assert "Successfully updated" in res_edit
    assert "return True" in target.read_text(encoding="utf-8")


def test_mcp_rpc_dispatch():
    # Initialize
    init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    init_resp = handle_rpc_request(init_req)
    assert init_resp["result"]["serverInfo"]["name"] == "ace-tools"

    # Tools List
    list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    list_resp = handle_rpc_request(list_req)
    tools = {t["name"] for t in list_resp["result"]["tools"]}
    assert "Search" in tools
    assert "Edit" in tools


def test_claude_config_installation(tmp_path: Path):
    install_claude_config(workspace_dir=str(tmp_path))
    settings_file = tmp_path / ".claude" / "settings.local.json"
    agent_file = tmp_path / ".claude" / "agents" / "ace-code.md"

    assert settings_file.exists()
    assert agent_file.exists()

    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    assert settings["agent"] == "ace-code"
    assert "mcp__ace-tools__Search" in settings["permissions"]["allow"]

    prompt = agent_file.read_text(encoding="utf-8")
    assert "name: ace-code" in prompt
    assert "disallowedTools: Read, Edit, Write, Grep, Glob, NotebookEdit, MultiEdit" in prompt
