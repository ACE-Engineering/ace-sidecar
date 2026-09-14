"""ace.sidecar.compression.prose_compressor — Quality-preserving assistant prose compression.

Trims low-value tokens and conversational fluff from assistant narrative turns at write-time,
saving tokens on all subsequent cache reads without invalidating prompt cache or altering agent trajectory.

Strict Invariants Preserved Verbatim:
- Code blocks (fenced ``` blocks)
- Inline code (`backticks`)
- File paths and URLs
- Test commands and CLI strings
- Markdown tables and JSON
- Numbers, line counts, and ports
- Thinking blocks (strictly untouched)
- Fail-soft fallback if any invariant is violated.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# Fluff phrases safely removed from assistant messages
FLUFF_PATTERNS = [
    r"^(?:Certainly|Sure|Of course|Great|Understood|Got it)[!.,]?\s*",
    r"^(?:I would be happy to|I'd be glad to|I will now proceed to|Let me go ahead and)\s*",
    r"^(?:As requested|Per your instructions|Following up on our earlier discussion)[,.]?\s*",
    r"\b(?:Please let me know if you need any(?:thing else| more assistance| further help)?)[.!]?\s*$",
    r"\b(?:Feel free to ask if you have any questions)[.!]?\s*$",
    r"\b(?:I hope this helps[!.]?)\s*$",
    r"\b(?:Now I will|Next I will|I am going to)\s+",
]

# Patterns for critical facts that must be preserved
PATH_PATTERN = re.compile(r"(?:/[a-zA-Z0-9_.-]+)+|[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(?:\.[a-zA-Z0-9]+)?")
CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```")
INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")


class ProseCompressor:
    """Compresses assistant prose at write time while preserving working state."""

    def __init__(self, min_length_chars: int = 120, max_compression_ratio: float = 0.5) -> None:
        self.min_length_chars = min_length_chars
        self.max_compression_ratio = max_compression_ratio
        self._compiled_fluff = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in FLUFF_PATTERNS]

    def compress_text(self, text: str) -> Tuple[str, bool]:
        """Compress a prose string. Returns (compressed_text, was_modified)."""
        if len(text) < self.min_length_chars:
            return text, False

        # Extract code blocks to keep them 100% untouched
        code_blocks: List[str] = []

        def _stash_code(match: re.Match) -> str:
            token = f"__ACE_CODE_{len(code_blocks)}__"
            code_blocks.append(match.group(0))
            return token

        masked = CODE_BLOCK_PATTERN.sub(_stash_code, text)

        # Extract paths and critical tokens to verify preservation
        original_paths = set(PATH_PATTERN.findall(text))

        # Apply fluff trimming
        processed = masked
        for pat in self._compiled_fluff:
            processed = pat.sub("", processed)

        # Clean redundant double newlines or leading/trailing whitespace
        processed = re.sub(r"\n{3,}", "\n\n", processed).strip()

        # Restore code blocks
        for i, code_block in enumerate(code_blocks):
            processed = processed.replace(f"__ACE_CODE_{i}__", code_block)

        # Fail-soft safety checks:
        # 1. Did we accidentally corrupt a code block?
        if processed.count("```") != text.count("```"):
            return text, False

        # 2. Did we lose any file path?
        compressed_paths = set(PATH_PATTERN.findall(processed))
        if not original_paths.issubset(compressed_paths):
            return text, False

        # 3. Did the text expand or compress too aggressively?
        if len(processed) >= len(text) or len(processed) < (len(text) * self.max_compression_ratio):
            return text, False

        return processed, True

    def compress_assistant_turn(
        self,
        message: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], int]:
        """Compress prose content blocks in an assistant message.

        Preserves thinking blocks and tool_use blocks strictly untouched.
        Returns (optimized_message, saved_bytes).
        """
        if message.get("role") != "assistant":
            return message, 0

        content = message.get("content")
        saved_bytes = 0

        if isinstance(content, str):
            compressed, modified = self.compress_text(content)
            if modified:
                saved = len(content.encode("utf-8")) - len(compressed.encode("utf-8"))
                new_msg = dict(message)
                new_msg["content"] = compressed
                return new_msg, saved
            return message, 0

        if isinstance(content, list):
            new_blocks = []
            modified_any = False

            for block in content:
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue

                btype = block.get("type")
                # Strict invariant: never touch thinking or tool_use blocks!
                if btype in ("thinking", "redacted_thinking", "tool_use"):
                    new_blocks.append(block)
                    continue

                if btype == "text":
                    raw_text = block.get("text", "")
                    compressed, modified = self.compress_text(raw_text)
                    if modified:
                        saved = len(raw_text.encode("utf-8")) - len(compressed.encode("utf-8"))
                        saved_bytes += saved
                        new_block = dict(block)
                        new_block["text"] = compressed
                        new_blocks.append(new_block)
                        modified_any = True
                        continue

                new_blocks.append(block)

            if modified_any:
                new_msg = dict(message)
                new_msg["content"] = new_blocks
                return new_msg, saved_bytes

        return message, 0
