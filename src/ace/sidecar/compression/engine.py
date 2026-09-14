"""ace.sidecar.compression.engine — Unified ContextOptimizer pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ace.sidecar.compression.idle_optimizer import IdleOptimizer
from ace.sidecar.compression.prose_compressor import ProseCompressor
from ace.sidecar.compression.tool_compressor import ToolCompressor


@dataclass
class OptimizationResult:
    optimized_payload: Dict[str, Any]
    saved_bytes: int
    saved_tokens_est: int
    tool_results_truncated: int
    reads_deduped: int
    prose_turns_compressed: int
    idle_compacted: bool
    idle_seconds: float


class ContextOptimizer:
    """Orchestrates local context compression across tools, prose, and prompt-cache TTL."""

    def __init__(
        self,
        max_tool_bytes: int = 2048,
        enable_tool_compression: bool = True,
        enable_prose_compression: bool = True,
        enable_idle_optimization: bool = True,
        ttl_seconds: float = 300.0,
    ) -> None:
        self.tool_compressor = ToolCompressor(max_tool_bytes=max_tool_bytes)
        self.prose_compressor = ProseCompressor()
        self.idle_optimizer = IdleOptimizer(ttl_seconds=ttl_seconds)

        self.enable_tool_compression = enable_tool_compression
        self.enable_prose_compression = enable_prose_compression
        self.enable_idle_optimization = enable_idle_optimization

    def optimize_request(
        self,
        payload: Dict[str, Any],
        current_ts: Optional[float] = None,
        force_idle_compaction: bool = False,
    ) -> OptimizationResult:
        """Optimize an outbound /v1/messages request payload before sending to Anthropic."""
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            return OptimizationResult(
                optimized_payload=payload,
                saved_bytes=0,
                saved_tokens_est=0,
                tool_results_truncated=0,
                reads_deduped=0,
                prose_turns_compressed=0,
                idle_compacted=False,
                idle_seconds=0.0,
            )

        messages = [dict(m) for m in raw_messages]
        total_saved_bytes = 0
        truncated_count = 0
        dedup_count = 0
        prose_count = 0

        # Step 1: Idle TTL Optimization (only runs if gap > 300s TTL)
        idle_compacted = False
        idle_seconds = 0.0
        if self.enable_idle_optimization:
            messages, idle_stats = self.idle_optimizer.optimize_if_idle(
                messages, current_ts=current_ts, force=force_idle_compaction
            )
            idle_compacted = idle_stats["compacted"]
            idle_seconds = idle_stats["idle_seconds"]

        # Step 2: Tool-Result Compression (applied to latest user message)
        if self.enable_tool_compression:
            messages, tool_stats = self.tool_compressor.compress_messages(
                messages, only_latest_turn=not idle_compacted
            )
            total_saved_bytes += tool_stats["total_saved_bytes"]
            truncated_count = tool_stats["truncated_count"]
            dedup_count = tool_stats["deduped_count"]

        # Step 3: Prose Compression on previous assistant turns
        if self.enable_prose_compression:
            for idx in range(len(messages)):
                msg = messages[idx]
                if msg.get("role") == "assistant":
                    opt_msg, saved = self.prose_compressor.compress_assistant_turn(msg)
                    if saved > 0:
                        messages[idx] = opt_msg
                        total_saved_bytes += saved
                        prose_count += 1

        out_payload = dict(payload)
        out_payload["messages"] = messages

        # Rough token proxy (~4 chars per token)
        tokens_est = int(total_saved_bytes / 4.0)

        return OptimizationResult(
            optimized_payload=out_payload,
            saved_bytes=total_saved_bytes,
            saved_tokens_est=tokens_est,
            tool_results_truncated=truncated_count,
            reads_deduped=dedup_count,
            prose_turns_compressed=prose_count,
            idle_compacted=idle_compacted,
            idle_seconds=idle_seconds,
        )
