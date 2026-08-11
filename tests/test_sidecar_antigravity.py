"""Unit tests for Antigravity transcript observability and heterogeneous agent dashboard integration."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import ace.sidecar.insights as insights
from ace.sidecar import build_sidecar_app


@pytest.fixture
def mock_antigravity_brain(tmp_path):
    brain_dir = tmp_path / "antigravity" / "brain"
    cid = "test-conv-12345"
    logs_dir = brain_dir / cid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    transcript_file = logs_dir / "transcript.jsonl"
    lines = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "Add feature X to the codebase",
            "timestamp": "2026-08-06T08:00:00Z",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "I will examine the codebase",
            "model": "gemini-3.6-flash",
            "tool_calls": [
                {
                    "id": "call_1",
                    "name": "view_file",
                    "args": {"AbsolutePath": "/project/main.py"},
                }
            ],
            "usage": {
                "input_tokens": 500,
                "output_tokens": 120,
                "cache_read_input_tokens": 300,
            },
            "timestamp": "2026-08-06T08:00:02Z",
        },
        {
            "step_index": 2,
            "source": "SYSTEM",
            "type": "TOOL_RESULT",
            "status": "DONE",
            "tool_use_id": "call_1",
            "content": "print('hello')",
            "timestamp": "2026-08-06T08:00:03Z",
        },
    ]

    with open(transcript_file, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    return brain_dir


def test_scan_antigravity_transcripts(mock_antigravity_brain):
    sess = insights._scan_antigravity(str(mock_antigravity_brain))
    assert len(sess) == 1
    s = sess[0]
    assert s["agent_type"] == "antigravity"
    assert s["session"].startswith("agy_")
    assert len(s["turns"]) == 1

    t = s["turns"][0]
    assert t["model"] == "gemini-3.6-flash"
    assert t["input_tokens"] == 500
    assert t["output_tokens"] == 120
    assert t["cache_read_input_tokens"] == 300
    assert len(t["calls"]) == 1
    assert t["calls"][0]["name"] == "view_file"


def test_sessions_combines_claude_and_antigravity(tmp_path, mock_antigravity_brain):
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
                "id": "msg_c1",
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 100,
                },
                "content": [{"type": "text", "text": "Hi"}],
            },
            "timestamp": "2026-08-06T07:00:01Z",
        },
    ]
    with open(c_file, "w", encoding="utf-8") as f:
        for line_item in c_lines:
            f.write(json.dumps(line_item) + "\n")

    all_sess = insights.sessions(
        root=str(claude_dir), antigravity_root=str(mock_antigravity_brain)
    )
    assert len(all_sess) == 2

    types = {s["agent_type"] for s in all_sess}
    assert "claude" in types
    assert "antigravity" in types


def test_dashboard_api_with_agent_filter(mock_antigravity_brain):
    insights.ANTIGRAVITY_ROOT = str(mock_antigravity_brain)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    app = build_sidecar_app(
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    # Test /api/stats
    resp = client.get("/api/stats?range=all&agent=antigravity")
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent"] == "antigravity"
    assert "agent_breakdown" in data

    # Test /api/report
    rep_resp = client.get("/api/report?range=all")
    assert rep_resp.status_code == 200
    rep_data = rep_resp.json()
    assert "agent_breakdown" in rep_data

    # Test /dashboard rendering
    dash_resp = client.get("/dashboard")
    assert dash_resp.status_code == 200
    assert "Heterogeneous Coding Agent Observability" in dash_resp.text
    assert "Antigravity (Google)" in dash_resp.text


def test_claude_skill_proposal_recommendation():
    mock_sessions = [
        {
            "agent_type": "claude",
            "turns": [
                {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 50,
                    "calls": [{"name": "view_file"}, {"name": "grep_search"}],
                }
            ],
        }
    ]
    recs = insights.recommendations(
        {"prompt_tokens": 1000, "peak_context": 50000}, capture=None, sess=mock_sessions
    )
    skill_recs = [
        r for r in recs if "Claude Optimization — New Skill Proposal:" in r["title"]
    ]
    assert len(skill_recs) >= 1
    assert "codebase-auditor" in skill_recs[0]["title"]
    assert "Comprehensive Analysis & Setup Steps:" in skill_recs[0]["detail"]
