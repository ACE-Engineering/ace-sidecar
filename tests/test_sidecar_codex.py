"""Unit tests for Codex transcript observability and multi-agent dashboard integration."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import ace.sidecar.insights as insights
from ace.sidecar import build_sidecar_app


@pytest.fixture
def mock_codex_sessions(tmp_path):
    codex_dir = tmp_path / "codex" / "sessions"
    codex_dir.mkdir(parents=True, exist_ok=True)

    # 1. JSONL format session
    session_jsonl = codex_dir / "session_123.jsonl"
    jsonl_lines = [
        {
            "role": "user",
            "content": "Implement user authentication with JWT",
            "timestamp": "2026-08-10T10:00:00Z",
            "cwd": "/workspace/auth-service",
        },
        {
            "role": "assistant",
            "model": "gpt-5.3-codex",
            "content": "I will create auth middleware.",
            "tool_calls": [
                {
                    "id": "call_jwt_1",
                    "function": {
                        "name": "write_to_file",
                        "arguments": json.dumps(
                            {"path": "/workspace/auth.py", "content": "import jwt"}
                        ),
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 250,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
            "timestamp": "2026-08-10T10:00:05Z",
        },
        {
            "role": "tool",
            "tool_call_id": "call_jwt_1",
            "content": "File written successfully",
            "timestamp": "2026-08-10T10:00:06Z",
        },
    ]
    with open(session_jsonl, "w", encoding="utf-8") as f:
        for line in jsonl_lines:
            f.write(json.dumps(line) + "\n")

    # 2. Single JSON format session
    session_json = codex_dir / "session_456.json"
    json_data = {
        "id": "sess_456_id",
        "cwd": "/workspace/payments",
        "messages": [
            {
                "role": "user",
                "content": "Refactor stripe payment handler",
                "timestamp": "2026-08-11T12:00:00Z",
            },
            {
                "role": "assistant",
                "model": "gpt-5.3-codex",
                "content": "Refactored payment handler to support 3D Secure.",
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 300},
                },
                "timestamp": "2026-08-11T12:00:08Z",
            },
        ],
    }
    with open(session_json, "w", encoding="utf-8") as f:
        json.dump(json_data, f)

    # 3. Real Codex rollout event-stream format session
    rollout_jsonl = codex_dir / "rollout_session_789.jsonl"
    rollout_lines = [
        {
            "timestamp": "2026-08-27T16:33:16.163Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "session_id": "01a04410-f1f6-7873-a99b-697e594cb876",
                "cwd": "/workspace/gateway-app",
                "base_instructions": {
                    "provenance": {"type": "model", "model": "gpt-5.6-terra"}
                },
            },
        },
        {
            "timestamp": "2026-08-27T16:33:16.502Z",
            "ordinal": 5,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Build AI gateway"}],
            },
        },
        {
            "timestamp": "2026-08-27T16:33:23.593Z",
            "ordinal": 14,
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "id": "ctc_1",
                "call_id": "call_1",
                "name": "exec",
                "input": "{\"cmd\": \"ls -la\"}",
            },
        },
        {
            "timestamp": "2026-08-27T16:33:23.827Z",
            "ordinal": 17,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 20000,
                        "cached_input_tokens": 15000,
                        "output_tokens": 200,
                    },
                    "last_token_usage": {
                        "input_tokens": 20000,
                        "cached_input_tokens": 15000,
                        "output_tokens": 200,
                        "cache_write_input_tokens": 0,
                    },
                },
            },
        },
    ]
    with open(rollout_jsonl, "w", encoding="utf-8") as f:
        for line in rollout_lines:
            f.write(json.dumps(line) + "\n")

    return codex_dir


def test_scan_codex_transcripts(mock_codex_sessions):
    sess = insights._scan_codex(str(mock_codex_sessions))
    assert len(sess) == 3

    s_map = {s["session"]: s for s in sess}

    # Verify session 1 (JSONL)
    s1 = s_map.get("codex_session_123") or next(
        s for s in sess if "session_123" in s["session"]
    )
    assert s1["agent_type"] == "codex"
    assert len(s1["turns"]) == 1

    t1 = s1["turns"][0]
    assert t1["model"] == "gpt-5.3-codex"
    assert t1["input_tokens"] == 200  # 1000 total prompt tokens - 800 cached tokens
    assert t1["output_tokens"] == 250
    assert t1["cache_read_input_tokens"] == 800
    assert len(t1["calls"]) == 1
    assert t1["calls"][0]["name"] == "write_to_file"
    assert "/workspace/auth-service" in s1["cwds"]

    # Verify session 2 (JSON)
    s2 = s_map.get("codex_session_456") or next(
        s for s in sess if "session_456" in s["session"]
    )
    assert s2["agent_type"] == "codex"
    assert len(s2["turns"]) == 1
    t2 = s2["turns"][0]
    assert t2["model"] == "gpt-5.3-codex"
    assert t2["input_tokens"] == 200  # 500 - 300
    assert t2["output_tokens"] == 150
    assert t2["cache_read_input_tokens"] == 300
    assert "/workspace/payments" in s2["cwds"]

    # Verify session 3 (Rollout event stream JSONL)
    s3 = s_map.get("codex_rollout_session_789") or next(
        s for s in sess if "rollout_session_789" in s["session"]
    )
    assert s3["agent_type"] == "codex"
    assert len(s3["turns"]) == 1
    t3 = s3["turns"][0]
    assert t3["model"] == "gpt-5.6-terra"
    assert t3["input_tokens"] == 5000  # 20000 - 15000
    assert t3["output_tokens"] == 200
    assert t3["cache_read_input_tokens"] == 15000
    assert len(t3["calls"]) == 1
    assert t3["calls"][0]["name"] == "exec"
    assert "/workspace/gateway-app" in s3["cwds"]


def test_sessions_combines_claude_antigravity_and_codex(
    tmp_path, mock_codex_sessions
):
    # Setup Claude directory
    claude_dir = tmp_path / "claude" / "projects"
    claude_dir.mkdir(parents=True, exist_ok=True)
    c_proj = claude_dir / "proj1"
    c_proj.mkdir(parents=True, exist_ok=True)
    c_file = c_proj / "session1.jsonl"
    c_lines = [
        {
            "type": "user",
            "message": {"content": "Hello Claude"},
            "timestamp": "2026-08-06T07:00:00Z",
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 40,
                },
                "content": [{"type": "text", "text": "Hi!"}],
            },
            "timestamp": "2026-08-06T07:00:02Z",
        },
    ]
    with open(c_file, "w", encoding="utf-8") as f:
        for line in c_lines:
            f.write(json.dumps(line) + "\n")

    # Setup Antigravity directory
    brain_dir = tmp_path / "antigravity" / "brain"
    cid = "test-conv-agy"
    logs_dir = brain_dir / cid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    a_file = logs_dir / "transcript.jsonl"
    a_lines = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "Add Gemini feature",
            "timestamp": "2026-08-08T08:00:00Z",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "model": "gemini-3.6-flash",
            "usage": {
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_read_input_tokens": 200,
            },
            "timestamp": "2026-08-08T08:00:04Z",
        },
    ]
    with open(a_file, "w", encoding="utf-8") as f:
        for line in a_lines:
            f.write(json.dumps(line) + "\n")

    # Invalidate cache and scan all 3
    insights._cache["sessions"] = None
    insights._cache["key"] = None

    all_sess = insights.sessions(
        root=str(claude_dir),
        antigravity_root=str(brain_dir),
        codex_root=str(mock_codex_sessions),
    )

    assert len(all_sess) == 5  # 1 claude + 1 antigravity + 3 codex

    types = {s["agent_type"] for s in all_sess}
    assert types == {"claude", "antigravity", "codex"}

    # Test agent scoping
    claude_only = insights.filter_range(all_sess, window=None, agent="claude")
    assert len(claude_only) == 1
    assert claude_only[0]["agent_type"] == "claude"

    agy_only = insights.filter_range(all_sess, window=None, agent="antigravity")
    assert len(agy_only) == 1
    assert agy_only[0]["agent_type"] == "antigravity"

    codex_only = insights.filter_range(all_sess, window=None, agent="codex")
    assert len(codex_only) == 3
    assert all(s["agent_type"] == "codex" for s in codex_only)

    # Test agent breakdown
    ab = insights.agent_breakdown(all_sess)
    assert "codex" in ab
    assert ab["codex"]["sessions"] == 3
    assert ab["codex"]["turns"] == 3
    assert ab["codex"]["prompt_tokens"] == 21500  # (200+800) + (200+300) + (5000+15000)
    assert ab["codex"]["output_tokens"] == 600  # 250 + 150 + 200
    assert ab["codex"]["cache_read_tokens"] == 16100  # 800 + 300 + 15000
    assert ab["codex"]["cost_usd"] > 0.0


def test_dashboard_and_api_with_codex(tmp_path, mock_codex_sessions, monkeypatch):
    monkeypatch.setattr(insights, "CODEX_ROOT", str(mock_codex_sessions))
    monkeypatch.setattr(insights, "TRANSCRIPT_ROOT", str(tmp_path / "empty_claude"))
    monkeypatch.setattr(
        insights, "ANTIGRAVITY_ROOT", str(tmp_path / "empty_antigravity")
    )
    insights._cache["sessions"] = None
    insights._cache["key"] = None

    app = build_sidecar_app(api_key="sk-test")
    client = TestClient(app, client=("127.0.0.1", 50000))

    # 1. HTML Dashboard
    resp = client.get("/dashboard?agent=codex")
    assert resp.status_code == 200
    assert "Heterogeneous Coding Agent Observability" in resp.text
    assert "Codex" in resp.text

    # 2. JSON API report
    resp_report = client.get("/api/report?agent=codex")
    assert resp_report.status_code == 200
    report_data = resp_report.json()
    assert "agent_breakdown" in report_data
    assert report_data["agent_breakdown"]["codex"]["sessions"] == 3

    # 3. Prometheus metrics
    resp_metrics = client.get("/metrics")
    assert resp_metrics.status_code == 200
    metrics_text = resp_metrics.text
    assert 'ace_sessions_total{agent="codex"}' in metrics_text
    assert 'ace_turns_total{agent="codex"}' in metrics_text
    assert 'ace_cost_usd_total{agent="codex"}' in metrics_text
