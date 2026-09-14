"""Unit tests for ace.sidecar.compression modules."""

from __future__ import annotations

from ace.sidecar.compression.engine import ContextOptimizer
from ace.sidecar.compression.idle_optimizer import IdleOptimizer
from ace.sidecar.compression.prose_compressor import ProseCompressor
from ace.sidecar.compression.tool_compressor import ToolCompressor


def test_tool_truncation_preserves_head_and_tail():
    compressor = ToolCompressor(max_tool_bytes=500, head_lines=5, tail_lines=5)
    # Generate 100 lines
    lines = [f"diagnostic line {i}" for i in range(50)] + [f"build status line {i}" for i in range(50, 100)]
    long_output = "\n".join(lines)

    truncated, meta = compressor.process_tool_result("Bash", long_output)
    assert meta["truncated"] is True
    assert meta["saved_bytes"] > 0
    # Head and tail must be preserved
    assert "diagnostic line 0" in truncated
    assert "diagnostic line 4" in truncated
    assert "build status line 99" in truncated
    assert "omitted" in truncated
    # Middle line should be omitted
    assert "diagnostic line 25" not in truncated


def test_tool_read_deduplication():
    compressor = ToolCompressor()
    path = "src/main.py"
    # Content must be larger than pointer string (~80 bytes) so saving is positive
    content = "def hello():\n" + ("    print('very large function body line')\n" * 10)

    # First read
    out1, meta1 = compressor.process_tool_result("Read", content, turn_index=0, target_path=path)
    assert meta1["deduped"] is False
    assert out1 == content

    # Second identical read
    out2, meta2 = compressor.process_tool_result("Read", content, turn_index=2, target_path=path)
    assert meta2["deduped"] is True
    assert meta2["saved_bytes"] > 0
    assert "identical to read" in out2
    assert "sha256:" in out2

    # If file was modified, deduplication should not fire
    compressor.mark_modified(path)
    out3, meta3 = compressor.process_tool_result("Read", content, turn_index=4, target_path=path)
    assert meta3["deduped"] is False
    assert out3 == content


def test_prose_compression_preserves_invariants():
    compressor = ProseCompressor(min_length_chars=50)
    input_text = (
        "Certainly! I would be happy to help with this task.\n"
        "I have reviewed the code in `/Users/dev/project/src/core.py`.\n"
        "Please run `pytest tests/test_core.py` to verify.\n"
        "Here is the updated implementation:\n"
        "```python\n"
        "def compute_delta(a, b):\n"
        "    return abs(a - b)\n"
        "```\n"
        "Please let me know if you need any more assistance!"
    )

    compressed, modified = compressor.compress_text(input_text)
    assert modified is True
    # Fluff removed
    assert "Certainly!" not in compressed
    assert "Please let me know if you need any more assistance!" not in compressed
    # Critical facts preserved verbatim
    assert "/Users/dev/project/src/core.py" in compressed
    assert "pytest tests/test_core.py" in compressed
    assert "def compute_delta(a, b):" in compressed


def test_idle_optimizer_triggers_after_ttl():
    optimizer = IdleOptimizer(ttl_seconds=300.0, active_window_turns=2, min_turns_to_compact=4)

    t0 = 10000.0
    optimizer.record_turn(ts=t0)

    # Within TTL (e.g. 100 seconds later)
    assert optimizer.is_cache_expired(current_ts=t0 + 100.0) is False

    # After TTL (e.g. 305 seconds later)
    assert optimizer.is_cache_expired(current_ts=t0 + 305.0) is True

    messages = [
        {"role": "user", "content": "Initial user task"},
        {"role": "assistant", "content": "Starting"},
        {"role": "user", "content": [{"type": "tool_use", "name": "Edit", "input": {"path": "src/app.py"}}]},
        {"role": "assistant", "content": "Done editing"},
        {"role": "user", "content": "Latest instruction"},
        {"role": "assistant", "content": "Working on latest"},
    ]

    # Run within TTL: no compaction
    unchanged, stats1 = optimizer.optimize_if_idle(messages, current_ts=t0 + 50.0)
    assert stats1["compacted"] is False
    assert len(unchanged) == len(messages)

    # Run after TTL: compaction occurs
    compacted, stats2 = optimizer.optimize_if_idle(messages, current_ts=t0 + 350.0)
    assert stats2["compacted"] is True
    assert len(compacted) < len(messages)
    # Working state extracted
    summary_text = compacted[0]["content"][0]["text"]
    assert "src/app.py" in summary_text


def test_context_optimizer_end_to_end():
    opt = ContextOptimizer(max_tool_bytes=200)

    payload = {
        "model": "claude-sonnet-5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": "\n".join([f"line {i}" for i in range(100)]),
                    }
                ],
            }
        ],
    }

    res = opt.optimize_request(payload)
    assert res.saved_bytes > 0
    assert res.tool_results_truncated == 1
    new_content = res.optimized_payload["messages"][0]["content"][0]["content"]
    assert "omitted" in new_content
