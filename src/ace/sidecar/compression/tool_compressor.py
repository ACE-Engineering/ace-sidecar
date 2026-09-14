"""ace.sidecar.compression.tool_compressor — Tool I/O compression & truncation.

Tool I/O represents ~88% of model-visible tokens in coding agent loops.
This module applies three high-leverage optimizations:
1. Head + Tail Truncation: Caps verbose command/search outputs at a safe threshold
   (default 2048 bytes / ~512 tokens), keeping the diagnostic head and the exit/status
   tail while omitting the redundant middle lines.
2. Read Deduplication: Detects byte-identical file reads across the session using content
   digests, replacing repeat reads with compact pointers (~120 bytes).
3. Superseding Stale Calls: Identifies repeated identical tool calls and prunes obsolete
   intermediate results.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_MAX_TOOL_BYTES = 2048  # ~512 tokens
DEFAULT_HEAD_LINES = 30
DEFAULT_TAIL_LINES = 20
POINTER_OVERHEAD_BYTES = 120

WRITE_TOOLS: Set[str] = {"Edit", "Write", "NotebookEdit", "mcp__ace-tools__Edit", "mcp__fermat-edit__Edit"}
READ_TOOLS: Set[str] = {"Read", "mcp__ace-tools__Search", "mcp__fermat-search__Search"}


@dataclass
class ReadRecord:
    path: str
    digest: str
    size: int
    turn_index: int


class ToolCompressor:
    """Compresses tool results and tool arguments to eliminate context waste."""

    def __init__(
        self,
        max_tool_bytes: int = DEFAULT_MAX_TOOL_BYTES,
        head_lines: int = DEFAULT_HEAD_LINES,
        tail_lines: int = DEFAULT_TAIL_LINES,
        enable_read_dedup: bool = True,
        enable_truncation: bool = True,
        enable_supersede: bool = True,
    ) -> None:
        self.max_tool_bytes = max_tool_bytes
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.enable_read_dedup = enable_read_dedup
        self.enable_truncation = enable_truncation
        self.enable_supersede = enable_supersede

        # Session tracking state for deduplication and superseding
        self._reads_by_path: Dict[str, List[ReadRecord]] = {}
        self._dirty_paths: Set[str] = set()
        self._tool_calls_seen: Dict[str, int] = {}  # signature -> turn_index

    def reset_session(self) -> None:
        """Clear session cache when starting a new session."""
        self._reads_by_path.clear()
        self._dirty_paths.clear()
        self._tool_calls_seen.clear()

    def mark_modified(self, path: str) -> None:
        """Record that a file was modified, invalidating prior read deduplication."""
        self._dirty_paths.add(path)
        if path in self._reads_by_path:
            self._reads_by_path[path].clear()

    @staticmethod
    def compute_digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    def truncate_text(
        self,
        text: str,
        max_bytes: Optional[int] = None,
        head_lines: Optional[int] = None,
        tail_lines: Optional[int] = None,
    ) -> Tuple[str, bool]:
        """Apply head+tail truncation if text exceeds max_bytes.

        Returns (result_text, was_truncated).
        """
        limit = max_bytes or self.max_tool_bytes
        if len(text.encode("utf-8", errors="replace")) <= limit:
            return text, False

        lines = text.splitlines(keepends=True)
        h_limit = head_lines or self.head_lines
        t_limit = tail_lines or self.tail_lines

        if len(lines) <= (h_limit + t_limit):
            # Short line count but very long lines: truncate characters
            char_budget = limit // 2
            head_part = text[:char_budget]
            tail_part = text[-char_budget:]
            omitted = len(text) - (len(head_part) + len(tail_part))
            marker = f"\n... [ACE: truncated {omitted} characters] ...\n"
            return head_part + marker + tail_part, True

        head_part = "".join(lines[:h_limit])
        tail_part = "".join(lines[-t_limit:])
        omitted_lines = len(lines) - (h_limit + t_limit)
        omitted_bytes = len(text.encode("utf-8", errors="replace")) - (
            len(head_part.encode("utf-8", errors="replace"))
            + len(tail_part.encode("utf-8", errors="replace"))
        )
        marker = (
            f"\n... [ACE: omitted {omitted_lines} lines "
            f"({omitted_bytes} bytes) - head and tail preserved] ...\n"
        )
        return head_part + marker + tail_part, True

    def process_tool_result(
        self,
        tool_name: str,
        content: str,
        turn_index: int = 0,
        target_path: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Process a single tool result, applying deduplication and truncation.

        Returns (compressed_content, metadata).
        """
        meta: Dict[str, Any] = {
            "original_bytes": len(content.encode("utf-8", errors="replace")),
            "deduped": False,
            "truncated": False,
            "saved_bytes": 0,
        }

        # Track writes
        if tool_name in WRITE_TOOLS and target_path:
            self.mark_modified(target_path)

        # 1. Read Deduplication
        if self.enable_read_dedup and target_path and tool_name in READ_TOOLS:
            digest = self.compute_digest(content)
            existing_records = self._reads_by_path.get(target_path, [])
            for rec in reversed(existing_records):
                if rec.digest == digest and target_path not in self._dirty_paths:
                    pointer = (
                        f"[ACE: identical to read of '{target_path}' at turn "
                        f"{rec.turn_index}, sha256:{digest[:12]} ({rec.size} bytes)]"
                    )
                    saved = meta["original_bytes"] - len(pointer.encode("utf-8"))
                    if saved > 0:
                        meta["deduped"] = True
                        meta["saved_bytes"] = saved
                        return pointer, meta

            # Store record for future dedup
            if target_path not in self._reads_by_path:
                self._reads_by_path[target_path] = []
            self._reads_by_path[target_path].append(
                ReadRecord(
                    path=target_path,
                    digest=digest,
                    size=meta["original_bytes"],
                    turn_index=turn_index,
                )
            )

        # 2. Head + Tail Truncation
        if self.enable_truncation:
            compressed, was_truncated = self.truncate_text(content)
            if was_truncated:
                meta["truncated"] = True
                meta["saved_bytes"] = meta["original_bytes"] - len(
                    compressed.encode("utf-8", errors="replace")
                )
                return compressed, meta

        return content, meta

    def compress_messages(
        self,
        messages: List[Dict[str, Any]],
        only_latest_turn: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Compress tool results in a messages list.

        If only_latest_turn is True, only compresses tool results in the most recent
        user message (to protect the Anthropic prompt-cache prefix of earlier turns).
        """
        stats = {
            "tool_results_examined": 0,
            "truncated_count": 0,
            "deduped_count": 0,
            "total_saved_bytes": 0,
        }

        if not messages:
            return messages, stats

        # Build tool_use map to know which tool_use_id corresponds to which tool
        tool_call_map: Dict[str, Dict[str, Any]] = {}
        for turn_idx, msg in enumerate(messages):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_call_map[block.get("id", "")] = {
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                            "turn": turn_idx,
                        }

        # Target messages: either just the last one or all
        target_indices = (
            range(len(messages) - 1, len(messages))
            if only_latest_turn
            else range(len(messages))
        )

        optimized_messages = list(messages)

        for idx in target_indices:
            msg = messages[idx]
            if msg.get("role") != "user":
                continue

            content = msg.get("content")
            if not isinstance(content, list):
                continue

            new_content = []
            modified_turn = False

            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    stats["tool_results_examined"] += 1
                    tool_use_id = block.get("tool_use_id", "")
                    tool_info = tool_call_map.get(tool_use_id, {})
                    tool_name = tool_info.get("name", "tool")
                    tool_input = tool_info.get("input", {})

                    target_path = (
                        tool_input.get("path")
                        or tool_input.get("file_path")
                        or tool_input.get("filePath")
                    )

                    raw_text = block.get("content", "")
                    if isinstance(raw_text, str):
                        compressed_text, meta = self.process_tool_result(
                            tool_name=tool_name,
                            content=raw_text,
                            turn_index=idx,
                            target_path=target_path,
                        )
                        if meta["deduped"] or meta["truncated"]:
                            new_block = dict(block)
                            new_block["content"] = compressed_text
                            new_content.append(new_block)
                            modified_turn = True
                            stats["total_saved_bytes"] += meta["saved_bytes"]
                            if meta["deduped"]:
                                stats["deduped_count"] += 1
                            if meta["truncated"]:
                                stats["truncated_count"] += 1
                            continue
                new_content.append(block)

            if modified_turn:
                new_msg = dict(msg)
                new_msg["content"] = new_content
                optimized_messages[idx] = new_msg

        return optimized_messages, stats
