"""ace.sidecar.tools.mcp_server — Local stdio Model Context Protocol (MCP) server.

Provides two unified, token-efficient tools for Claude Code:
1. `Search`: Combined file reader (with line ranges), regex search, and glob discovery.
   Returns focused snippets rather than dumping entire files into context.
2. `Edit`: Precise string replacement and file creation with minimal confirmation tokens.

Speaks JSON-RPC 2.0 over stdio natively with zero external dependencies.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def tool_search(
    path: Optional[str] = None,
    pattern: Optional[str] = None,
    contentRegex: Optional[str] = None,
    startLine: Optional[int] = None,
    endLine: Optional[int] = None,
    maxMatches: int = 25,
) -> str:
    """Consolidated file search, globbing, and targeted section reading."""
    cwd = Path.cwd()

    # Mode 1: Targeted file read
    if path and not contentRegex:
        target = Path(path)
        if not target.is_absolute():
            target = cwd / target

        if not target.exists():
            return f"Error: file not found: {path}"
        if not target.is_file():
            return f"Error: {path} is not a regular file"

        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()

            total_lines = len(lines)
            s_line = max(1, startLine or 1)
            e_line = min(total_lines, endLine or total_lines)

            if s_line > total_lines:
                return f"File '{path}' has {total_lines} lines; startLine {s_line} is past EOF."

            selected = lines[s_line - 1 : e_line]
            output = [f"File: {path} (lines {s_line}-{e_line} of {total_lines}):"]
            for i, line_content in enumerate(selected, start=s_line):
                output.append(f"{i:5d} | {line_content.rstrip()}")
            return "\n".join(output)
        except Exception as e:
            return f"Error reading {path}: {e}"

    # Mode 2: Regex content search
    if contentRegex:
        try:
            compiled = re.compile(contentRegex)
        except re.error as e:
            return f"Invalid regex pattern '{contentRegex}': {e}"

        search_root = Path(path) if path else cwd
        if not search_root.is_absolute():
            search_root = cwd / search_root

        matches = []
        file_count = 0

        # Scan files respecting gitignore / hidden directories
        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", "venv", ".venv")]
            for f in files:
                if f.startswith("."):
                    continue
                file_count += 1
                fpath = Path(root) / f
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                        for line_no, line in enumerate(fh, start=1):
                            if compiled.search(line):
                                try:
                                    rel = fpath.relative_to(cwd)
                                except ValueError:
                                    rel = fpath
                                matches.append(f"{rel}:{line_no}: {line.strip()[:160]}")
                                if len(matches) >= maxMatches:
                                    break
                except Exception:
                    continue
                if len(matches) >= maxMatches:
                    break
            if len(matches) >= maxMatches:
                break

        if not matches:
            return f"No matches found for regex '{contentRegex}' across {file_count} files."
        res = [f"Found {len(matches)} match(es) for regex '{contentRegex}':"]
        res.extend(matches)
        if len(matches) >= maxMatches:
            res.append(f"... (results capped at {maxMatches} matches)")
        return "\n".join(res)

    # Mode 3: Glob pattern discovery
    if pattern:
        matched = []
        for p in glob.glob(pattern, recursive=True, root_dir=str(cwd)):
            if not p.startswith(".git") and "node_modules" not in p:
                matched.append(p)
                if len(matched) >= maxMatches:
                    break

        if not matched:
            return f"No files found matching pattern '{pattern}'"
        res = [f"Matched {len(matched)} file(s) for '{pattern}':"]
        res.extend(matched)
        return "\n".join(res)

    return "Please specify a 'path', 'pattern', or 'contentRegex'."


def tool_edit(
    path: str,
    new_string: str,
    old_string: Optional[str] = None,
    overwrite: bool = False,
) -> str:
    """Precise string replacement or file creation with minimal output overhead."""
    cwd = Path.cwd()
    target = Path(path)
    if not target.is_absolute():
        target = cwd / target

    # Mode 1: Create or overwrite
    if overwrite or old_string is None or not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(new_string)
            action = "Overwrote" if target.exists() and overwrite else "Created"
            return f"Successfully {action.lower()} file '{path}' ({len(new_string.splitlines())} lines)."
        except Exception as e:
            return f"Error writing file '{path}': {e}"

    # Mode 2: Targeted string replacement
    try:
        with open(target, "r", encoding="utf-8") as fh:
            content = fh.read()

        count = content.count(old_string)
        if count == 0:
            return f"Error: 'old_string' not found in file '{path}'."
        if count > 1:
            return f"Error: 'old_string' found {count} times in '{path}'. Must be unique."

        updated = content.replace(old_string, new_string, 1)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(updated)

        return f"Successfully updated '{path}'."
    except Exception as e:
        return f"Error editing file '{path}': {e}"


# Tool schemas exposed via MCP
MCP_TOOLS = [
    {
        "name": "Search",
        "description": "Consolidated file search, regex content matching, and line-range reader. Replaces Read/Grep/Glob.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative or absolute file/directory path to read or search"},
                "startLine": {"type": "integer", "description": "1-based starting line number to read"},
                "endLine": {"type": "integer", "description": "1-based ending line number to read"},
                "contentRegex": {"type": "string", "description": "Regular expression to search inside file contents"},
                "pattern": {"type": "string", "description": "Glob pattern to find files (e.g. '**/*.ts')"},
                "maxMatches": {"type": "integer", "description": "Maximum search matches to return (default 25)"},
            },
        },
    },
    {
        "name": "Edit",
        "description": "Precise string replacement, file creation, or overwrite. Replaces Edit/Write/MultiEdit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file to edit or create"},
                "old_string": {"type": "string", "description": "Exact text chunk to replace. Omit to create new file."},
                "new_string": {"type": "string", "description": "Replacement text or file content"},
                "overwrite": {"type": "boolean", "description": "If true, completely overwrite entire file"},
            },
            "required": ["path", "new_string"],
        },
    },
]


def handle_rpc_request(req: Dict[str, Any]) -> Dict[str, Any]:
    """Process single JSON-RPC request for the MCP protocol."""
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ace-tools", "version": "0.1.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS},
        }

    if method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "Search":
            res = tool_search(
                path=args.get("path"),
                pattern=args.get("pattern"),
                contentRegex=args.get("contentRegex"),
                startLine=args.get("startLine"),
                endLine=args.get("endLine"),
                maxMatches=args.get("maxMatches", 25),
            )
        elif tool_name == "Edit":
            res = tool_edit(
                path=args.get("path", ""),
                new_string=args.get("new_string", ""),
                old_string=args.get("old_string"),
                overwrite=args.get("overwrite", False),
            )
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": str(res)}]},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method '{method}' not found"},
    }


def run_mcp_server() -> None:
    """Run JSON-RPC MCP server on stdin/stdout."""
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            resp = handle_rpc_request(req)
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except Exception as e:
            err = {"jsonrpc": "2.0", "error": {"code": -32700, "message": str(e)}}
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_mcp_server()
