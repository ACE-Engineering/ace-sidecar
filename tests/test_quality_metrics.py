"""Tests for ace.sidecar code quality, verification hygiene, and reliability metrics."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from ace.sidecar.dashboard_render import render
from ace.sidecar.insights import (
    _classify_call,
    _build_payload,
    format_prometheus_metrics,
    quality_metrics,
)


def test_classify_call_test_commands() -> None:
    # Pytest
    cl = _classify_call("Bash", {"command": "pytest -v tests/"})
    assert cl["is_test_run"] is True
    assert cl["is_edit"] is False

    # npm test
    cl = _classify_call("run_command", {"CommandLine": "npm test"})
    assert cl["is_test_run"] is True

    # ruff check
    cl = _classify_call("Bash", {"command": "ruff check --fix ."})
    assert cl["is_test_run"] is True

    # cargo test
    cl = _classify_call("exec_command", {"command": "cargo test --all"})
    assert cl["is_test_run"] is True

    # non-test bash command
    cl = _classify_call("Bash", {"command": "git status"})
    assert cl["is_test_run"] is False


def test_classify_call_edits_and_views() -> None:
    # Edit tool
    cl = _classify_call("Edit", {"file_path": "src/main.py"})
    assert cl["is_edit"] is True
    assert cl["is_view"] is False
    assert cl["raw_target"] == "src/main.py"
    assert cl["is_src_file"] is True
    assert cl["is_test_file"] is False

    # write_to_file on test file
    cl = _classify_call("write_to_file", {"TargetFile": "/app/tests/test_api.py"})
    assert cl["is_edit"] is True
    assert cl["is_test_file"] is True
    assert cl["is_src_file"] is False

    # replace_file_content
    cl = _classify_call("replace_file_content", {"TargetFile": "web/app.tsx"})
    assert cl["is_edit"] is True
    assert cl["is_src_file"] is True

    # View / read_file
    cl = _classify_call("view_file", {"AbsolutePath": "/app/README.md"})
    assert cl["is_view"] is True
    assert cl["is_edit"] is False
    assert cl["raw_target"] == "/app/README.md"


def test_quality_metrics_empty() -> None:
    qm = quality_metrics([])
    assert qm["available"] is False
    assert qm["quality_score"] == 100
    assert qm["grade"] == "A"
    assert qm["verification_rate_pct"] == 100.0
    assert qm["first_pass_success_rate_pct"] == 100.0
    assert qm["thrashed_files_count"] == 0


def test_quality_metrics_clean_verified_session() -> None:
    sess: List[Dict[str, Any]] = [
        {
            "session": "s1",
            "agent_type": "claude",
            "turns": [
                {
                    "model": "claude-sonnet-4-6",
                    "calls": [
                        {
                            "name": "view_file",
                            "raw_target": "src/app.py",
                            "sig": "sig_v1",
                            "is_view": True,
                            "is_edit": False,
                            "is_test_run": False,
                        },
                        {
                            "name": "replace_file_content",
                            "raw_target": "src/app.py",
                            "sig": "sig_e1",
                            "is_view": False,
                            "is_edit": True,
                            "is_test_run": False,
                            "is_src_file": True,
                            "is_test_file": False,
                        },
                        {
                            "name": "write_to_file",
                            "raw_target": "tests/test_app.py",
                            "sig": "sig_e2",
                            "is_view": False,
                            "is_edit": True,
                            "is_test_run": False,
                            "is_src_file": False,
                            "is_test_file": True,
                        },
                        {
                            "name": "Bash",
                            "raw_target": "pytest",
                            "sig": "sig_t1",
                            "is_view": False,
                            "is_edit": False,
                            "is_test_run": True,
                            "is_error": False,
                        },
                    ],
                }
            ],
        }
    ]
    qm = quality_metrics(sess)
    assert qm["available"] is True
    assert qm["verification_rate"] == 1.0
    assert qm["verification_rate_pct"] == 100.0
    assert qm["first_pass_success_rate"] == 1.0
    assert qm["tool_error_rate"] == 0.0
    assert qm["thrashed_files_count"] == 0
    assert qm["redundant_reads_count"] == 0
    assert qm["sessions_with_edits"] == 1
    assert qm["sessions_with_tests"] == 1
    assert qm["quality_score"] >= 90
    assert qm["grade"] == "A"


def test_quality_metrics_unverified_and_thrashed_session() -> None:
    # 1 session editing a file 4 times (thrashing), zero tests, 1 tool error
    sess: List[Dict[str, Any]] = [
        {
            "session": "s2",
            "agent_type": "antigravity",
            "turns": [
                {
                    "model": "gemini-3.6-flash",
                    "calls": [
                        {
                            "name": "view_file",
                            "raw_target": "src/flaky.py",
                            "sig": "v1",
                            "is_view": True,
                        },
                        {
                            "name": "view_file",
                            "raw_target": "src/flaky.py",
                            "sig": "v1",
                            "is_view": True,  # Redundant read
                        },
                        {
                            "name": "edit",
                            "raw_target": "src/flaky.py",
                            "is_edit": True,
                            "is_src_file": True,
                            "is_error": True,  # Error 1
                        },
                    ],
                },
                {
                    "model": "gemini-3.6-flash",
                    "calls": [
                        {
                            "name": "edit",
                            "raw_target": "src/flaky.py",
                            "is_edit": True,
                            "is_src_file": True,
                        },
                        {
                            "name": "edit",
                            "raw_target": "src/flaky.py",
                            "is_edit": True,
                            "is_src_file": True,
                        },
                        {
                            "name": "edit",
                            "raw_target": "src/flaky.py",
                            "is_edit": True,
                            "is_src_file": True,
                        },
                    ],
                },
            ],
        }
    ]
    qm = quality_metrics(sess)
    assert qm["available"] is True
    assert qm["verification_rate"] == 0.0  # Zero tests run
    assert qm["sessions_with_edits"] == 1
    assert qm["sessions_with_tests"] == 0
    assert qm["thrashed_files_count"] == 1
    assert "src/flaky.py" in qm["thrashed_files_list"]
    assert qm["redundant_reads_count"] == 1
    assert qm["tool_error_rate"] > 0.0
    assert qm["quality_score"] < 70
    assert qm["grade"] in ("C", "D", "F")


def test_quality_in_payload_and_prometheus() -> None:
    sess: List[Dict[str, Any]] = [
        {
            "session": "s1",
            "agent_type": "claude",
            "cwds": ["/test/repo"],
            "turns": [
                {
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 800,
                    "cache_creation_input_tokens": 100,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "blocks": {"text": 1, "tool_use": 2},
                    "calls": [
                        {
                            "name": "Edit",
                            "raw_target": "app.py",
                            "is_edit": True,
                            "is_src_file": True,
                        },
                        {
                            "name": "Bash",
                            "command": "pytest",
                            "is_test_run": True,
                        },
                    ],
                    "ts": "2026-08-27T12:00:00Z",
                }
            ],
            "events": [
                (1787832000.0, "prompt", (), 1787832000.0),
                (1787832005.0, "assistant", ("Edit", "Bash"), 1787832005.0),
            ],
            "path": "/dummy/s1.jsonl",
            "bytes": 500,
            "mtime": 1787832010.0,
            "snippet": "fix bug and test",
        }
    ]

    payload = _build_payload(sess, capture=None, range_key="all", agent="all", store_path=None)
    assert "quality" in payload
    qm = payload["quality"]
    assert qm["available"] is True
    assert qm["verification_rate_pct"] == 100.0
    assert qm["quality_score"] >= 80

    # Prometheus export check
    prom_text = format_prometheus_metrics(payload)
    assert "ace_quality_score" in prom_text
    assert 'ace_quality_verification_rate{agent="all"} 1.0' in prom_text
    assert 'ace_quality_first_pass_success_rate{agent="all"} 1.0' in prom_text
    assert "ace_quality_thrashed_files_total 0" in prom_text
    assert "ace_quality_redundant_reads_total 0" in prom_text

    # Render dashboard check
    html = render(payload)
    assert "CODE QUALITY &amp; RELIABILITY" in html or "CODE QUALITY & RELIABILITY" in html
    assert "quality_score" in html
    assert "verification_rate" in html
    assert "first_pass_success" in html


def test_quality_metrics_by_agent_and_model() -> None:
    sess: List[Dict[str, Any]] = [
        # Session 1: Claude using Sonnet - verified, high quality
        {
            "session": "s1",
            "agent_type": "claude",
            "cwds": ["/test"],
            "turns": [
                {
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 800,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "calls": [
                        {"name": "Edit", "raw_target": "src/a.py", "is_edit": True, "is_src_file": True},
                        {"name": "Bash", "command": "pytest", "is_test_run": True, "is_error": False},
                    ],
                }
            ],
            "events": [],
        },
        # Session 2: Antigravity using Gemini Flash - unverified, error
        {
            "session": "s2",
            "agent_type": "antigravity",
            "cwds": ["/test"],
            "turns": [
                {
                    "model": "gemini-3.6-flash",
                    "input_tokens": 500,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "calls": [
                        {"name": "write_to_file", "raw_target": "src/b.py", "is_edit": True, "is_src_file": True, "is_error": True},
                    ],
                }
            ],
            "events": [],
        },
    ]

    qm = quality_metrics(sess)
    assert "by_agent" in qm
    assert "claude" in qm["by_agent"]
    assert "antigravity" in qm["by_agent"]

    claude_q = qm["by_agent"]["claude"]
    assert claude_q["verification_rate_pct"] == 100.0
    assert claude_q["quality_score"] >= 85

    agy_q = qm["by_agent"]["antigravity"]
    assert agy_q["verification_rate_pct"] == 0.0
    assert agy_q["first_pass_success_rate_pct"] == 0.0

    assert "by_model" in qm
    models = [m["model"] for m in qm["by_model"]]
    assert "claude-sonnet-4-6" in models
    assert "gemini-3.6-flash" in models

    # Dashboard render with comparative table
    payload = _build_payload(sess, capture=None, range_key="all", agent="all", store_path=None)
    html = render(payload)
    assert "Claude Code" in html or "claude" in html
    assert "claude-sonnet-4-6" in html
    assert "gemini-3.6-flash" in html
    assert "Engine / model" in html
    assert "Engine &amp; model reliability" in html


def test_quality_metrics_by_task_category() -> None:
    from ace.sidecar.insights import (
        _classify_session_task_category,
        TASK_CAT_UI,
        TASK_CAT_BACKEND,
        TASK_CAT_TESTING,
        TASK_CAT_DOCS,
        TASK_CAT_RESEARCH,
    )

    # 1. UI session
    s_ui = {
        "session": "ui_sess",
        "turns": [
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 0,
                "output_tokens": 50,
                "model": "claude-sonnet-4-6",
                "calls": [
                    {"name": "write_to_file", "raw_target": "src/components/Header.tsx", "is_edit": True},
                    {"name": "write_to_file", "raw_target": "src/styles/app.css", "is_edit": True},
                ],
            }
        ],
        "events": [],
    }
    assert _classify_session_task_category(s_ui) == TASK_CAT_UI

    # 2. Testing session
    s_test = {
        "session": "test_sess",
        "turns": [
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 0,
                "output_tokens": 50,
                "model": "claude-sonnet-4-6",
                "calls": [
                    {"name": "write_to_file", "raw_target": "tests/test_api.py", "is_edit": True, "is_test_file": True},
                    {"name": "Bash", "command": "pytest", "is_test_run": True},
                ],
            }
        ],
        "events": [],
    }
    assert _classify_session_task_category(s_test) == TASK_CAT_TESTING

    # 3. Docs session
    s_docs = {
        "session": "docs_sess",
        "turns": [
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 0,
                "output_tokens": 50,
                "model": "claude-sonnet-4-6",
                "calls": [
                    {"name": "write_to_file", "raw_target": "docs/architecture.md", "is_edit": True},
                    {"name": "replace_file_content", "raw_target": "README.md", "is_edit": True},
                ],
            }
        ],
        "events": [],
    }
    assert _classify_session_task_category(s_docs) == TASK_CAT_DOCS

    # 4. Research session (read-only)
    s_res = {
        "session": "res_sess",
        "turns": [
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 0,
                "output_tokens": 50,
                "model": "gemini-3.6-flash",
                "calls": [
                    {"name": "view_file", "raw_target": "src/server.py", "is_view": True},
                    {"name": "grep_search", "raw_target": "main", "is_view": False},
                ],
            }
        ],
        "events": [],
    }
    assert _classify_session_task_category(s_res) == TASK_CAT_RESEARCH

    # 5. Backend session
    s_back = {
        "session": "back_sess",
        "turns": [
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 0,
                "output_tokens": 50,
                "model": "gpt-5.3-codex",
                "calls": [
                    {"name": "replace_file_content", "raw_target": "src/ace/gateway/proxy.py", "is_edit": True, "is_src_file": True},
                ],
            }
        ],
        "events": [],
    }
    assert _classify_session_task_category(s_back) == TASK_CAT_BACKEND

    # Multi-session aggregation in quality_metrics
    sess_all = [s_ui, s_test, s_docs, s_res, s_back]
    qm = quality_metrics(sess_all)
    assert "by_category" in qm
    assert TASK_CAT_UI in qm["by_category"]
    assert TASK_CAT_BACKEND in qm["by_category"]
    assert TASK_CAT_TESTING in qm["by_category"]
    assert TASK_CAT_DOCS in qm["by_category"]
    assert TASK_CAT_RESEARCH in qm["by_category"]

    ui_cat = qm["by_category"][TASK_CAT_UI]
    assert ui_cat["sessions"] == 1
    assert ui_cat["label"] == "UI & Frontend"

    # Prometheus export
    payload = _build_payload(sess_all, capture=None, range_key="all", agent="all", store_path=None)
    prom_text = format_prometheus_metrics(payload)
    assert 'ace_quality_category_score{category="ui"}' in prom_text
    assert 'ace_quality_category_completion_rate{category="ui"}' in prom_text

    # Render dashboard
    html = render(payload)
    assert "Capability by coding task domain" in html
    assert "UI &amp; Frontend" in html or "UI & Frontend" in html


def test_extended_quality_metrics_turns_time_fixes_verbosity() -> None:
    from ace.sidecar.insights import _count_comment_and_code_lines, _classify_call

    # Test comment and code line parsing
    code_sample = """# Header comment
def solve():
    // inline comment
    x = 10
    y = 20
    return x + y
"""
    c_cnt, k_cnt = _count_comment_and_code_lines(code_sample)
    assert c_cnt == 2
    assert k_cnt == 4

    # Test _classify_call with CodeContent
    call_info = _classify_call("write_to_file", {"TargetFile": "src/main.py", "CodeContent": code_sample})
    assert call_info["is_edit"] is True
    assert call_info["comment_lines"] == 2
    assert call_info["code_lines"] == 4

    # Test session with multiple turns, follow-up edits on same file, timestamps, output tokens
    sess_complex = [
        {
            "session": "sess_1",
            "agent_type": "claude",
            "cwds": ["/test"],
            "events": [
                (1000.0, "user_turn", {}, 1000.0),
                (1120.0, "assistant_turn", {}, 1120.0),  # 120s duration = 2.0 mins
            ],
            "turns": [
                {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "output_tokens": 200,
                    "model": "claude-sonnet-4-6",
                    "calls": [
                        {
                            "name": "write_to_file",
                            "raw_target": "src/app.py",
                            "is_edit": True,
                            "is_src_file": True,
                            "comment_lines": 5,
                            "code_lines": 20,
                        },
                        {
                            "name": "write_to_file",
                            "raw_target": "src/app.py",  # Re-edit = 1 follow-up fix
                            "is_edit": True,
                            "is_src_file": True,
                            "comment_lines": 3,
                            "code_lines": 10,
                        },
                        {
                            "name": "Bash",
                            "command": "pytest",
                            "is_test_run": True,
                        },
                    ],
                },
                {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "output_tokens": 100,
                    "model": "claude-sonnet-4-6",
                    "calls": [],
                },
            ],
        }
    ]

    qm = quality_metrics(sess_complex)

    # 1. Turns per completion
    assert qm["turns_per_completion_avg"] == 2.0
    assert qm["clean_completed_sessions"] == 1

    # 2. Time taken per completion
    assert qm["duration_seconds_per_completion_avg"] == 120.0
    assert qm["duration_minutes_per_completion_avg"] == 2.0

    # 3. Follow-up fixes on same code
    assert qm["followup_code_fixes_count"] == 1
    assert qm["followup_code_fix_rate_pct"] == 50.0  # 1 fix out of (1 unique file + 1 fix)

    # 4. Verbosity & Comment Ratio
    # Total output tokens = 300 across 2 turns = 150.0 tok/turn
    assert qm["verbosity_tokens_per_turn"] == 150.0
    assert qm["verbosity_level"] == "Moderate"
    # Total comments = 8, Total code = 30 -> ratio = 8/30 = 0.27
    assert qm["comment_to_code_ratio"] == 0.27
    assert qm["comment_density_pct"] == round(8 / 38 * 100.0, 1)

    # Check Prometheus formatting
    payload = _build_payload(sess_complex, capture=None, range_key="all", agent="all", store_path=None)
    prom_text = format_prometheus_metrics(payload)
    assert "ace_quality_turns_per_completion_avg 2.0" in prom_text
    assert "ace_quality_duration_seconds_per_completion_avg 120.0" in prom_text
    assert "ace_quality_followup_code_fixes_total 1" in prom_text
    assert "ace_quality_comment_to_code_ratio 0.27" in prom_text
    assert "ace_quality_verbosity_tokens_per_turn 150.0" in prom_text

    # Check UI rendering
    html = render(payload)
    assert "TURNS / TASK" in html or "turns_per_task" in html
    assert "TIME / TASK" in html or "time_per_task" in html
    assert "FOLLOW-UP FIXES" in html or "followup_fixes" in html
    assert "CODE COMMENT DENSITY" in html or "comment_ratio" in html
    assert "VERBOSITY LEVEL" in html or "verbosity" in html


