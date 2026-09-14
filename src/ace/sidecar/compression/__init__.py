"""ace.sidecar.compression — local context compression engine for coding agents."""

from __future__ import annotations

from ace.sidecar.compression.engine import ContextOptimizer
from ace.sidecar.compression.idle_optimizer import IdleOptimizer
from ace.sidecar.compression.prose_compressor import ProseCompressor
from ace.sidecar.compression.tool_compressor import ToolCompressor

__all__ = [
    "ContextOptimizer",
    "IdleOptimizer",
    "ProseCompressor",
    "ToolCompressor",
]
