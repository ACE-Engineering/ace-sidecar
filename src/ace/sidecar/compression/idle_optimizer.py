"""ace.sidecar.compression.idle_optimizer — Anthropic prompt-cache TTL-aware context optimization.

Context Economics:
- Cache reads cost ~0.1x, cache writes cost 1.25x (a 12.5x difference!).
- Mutating context during active exchanges (< 300s TTL) invalidates cache prefixes,
  turning cheap 0.1x reads into expensive 1.25x writes.
- However, when a developer is idle for > 300 seconds (5 minutes), the cache expires anyway.
- IdleOptimizer triggers context compaction strictly after the 300s TTL has elapsed,
  compacting older conversation history into structured working-state episodes while
  keeping the freshest turns raw.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

ANTHROPIC_CACHE_TTL_SECONDS = 300.0  # 5 minutes
DEFAULT_ACTIVE_WINDOW_TURNS = 4  # Keep the last 4 turns raw


class IdleOptimizer:
    """Evaluates session idle time against cache TTL and compacts expired history."""

    def __init__(
        self,
        ttl_seconds: float = ANTHROPIC_CACHE_TTL_SECONDS,
        active_window_turns: int = DEFAULT_ACTIVE_WINDOW_TURNS,
        min_turns_to_compact: int = 8,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.active_window_turns = active_window_turns
        self.min_turns_to_compact = min_turns_to_compact
        self._last_turn_timestamp: float = 0.0

    def record_turn(self, ts: Optional[float] = None) -> None:
        """Update the timestamp of the most recent turn."""
        self._last_turn_timestamp = ts or time.time()

    def is_cache_expired(self, current_ts: Optional[float] = None) -> bool:
        """Check whether the elapsed idle time exceeds Anthropic's cache TTL."""
        if self._last_turn_timestamp <= 0.0:
            return False
        now = current_ts or time.time()
        return (now - self._last_turn_timestamp) >= self.ttl_seconds

    def extract_working_state(self, older_messages: List[Dict[str, Any]]) -> str:
        """Extract structured working state from older turns."""
        files_modified = set()
        test_commands = []
        user_constraints = []

        for msg in older_messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "user" and isinstance(content, str):
                if any(w in content.lower() for w in ("don't", "do not", "never", "must", "always", "ensure")):
                    user_constraints.append(content[:150].strip())

            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        if name in ("Edit", "Write", "mcp__ace-tools__Edit", "mcp__fermat-edit__Edit"):
                            path = inp.get("path") or inp.get("file_path") or inp.get("filePath")
                            if path:
                                files_modified.add(path)
                        elif name == "Bash":
                            cmd = inp.get("command", "")
                            if any(t in cmd for t in ("test", "pytest", "npm", "cargo", "go test")):
                                test_commands.append(cmd[:100])

        state_lines = ["### Context Summary (Compacted by ACE Sidecar after >5m idle)"]
        if files_modified:
            state_lines.append(f"- **Files modified so far**: {', '.join(sorted(files_modified))}")
        if test_commands:
            state_lines.append(f"- **Verification run**: `{test_commands[-1]}`")
        if user_constraints:
            state_lines.append(f"- **Active constraints**: {'; '.join(user_constraints[-2:])}")

        return "\n".join(state_lines)

    def optimize_if_idle(
        self,
        messages: List[Dict[str, Any]],
        current_ts: Optional[float] = None,
        force: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """If idle time exceeds TTL (or force=True), compact older messages into working state."""
        stats = {
            "cache_expired": False,
            "compacted": False,
            "turns_before": len(messages),
            "turns_after": len(messages),
            "idle_seconds": round((current_ts or time.time()) - self._last_turn_timestamp, 1)
            if self._last_turn_timestamp > 0
            else 0.0,
        }

        expired = force or self.is_cache_expired(current_ts)
        stats["cache_expired"] = expired

        if not expired or len(messages) < self.min_turns_to_compact:
            self.record_turn(current_ts)
            return messages, stats

        # Split into older turns to compact and active turns to preserve verbatim
        cutoff = max(1, len(messages) - self.active_window_turns)
        older = messages[:cutoff]
        freshest = messages[cutoff:]

        summary_text = self.extract_working_state(older)

        # Create a single summary turn for older context
        compacted_turn = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": summary_text,
                }
            ],
        }

        compacted_ack = {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Understood. I have the current state, modified files, and constraints loaded. Continuing task.",
                }
            ],
        }

        optimized = [compacted_turn, compacted_ack] + list(freshest)
        stats["compacted"] = True
        stats["turns_after"] = len(optimized)

        self.record_turn(current_ts)
        return optimized, stats
