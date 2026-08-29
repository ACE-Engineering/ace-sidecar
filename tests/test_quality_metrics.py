"""Tests for ace.sidecar code quality, verification hygiene, and reliability metrics."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from ace.sidecar.dashboard_render import render
from ace.sidecar.insights import (
    MIN_QUALITY_EVAL_TURNS,
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
    assert qm["scored"] is False
    # Nothing observed is not a perfect score. An empty scope reports no score at all.
    assert qm["quality_score"] is None
    assert qm["grade"] == "—"
    assert qm["thrashed_files_count"] == 0
    assert qm["files_edited"] == 0


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
    assert qm["scored"] is True
    assert qm["verification_rate"] == 1.0
    assert qm["verification_rate_pct"] == 100.0
    assert qm["tool_success_rate"] == 1.0
    assert qm["tool_error_rate"] == 0.0
    assert qm["thrashed_files_count"] == 0
    # Two distinct files, each edited once, in one session.
    assert qm["files_edited"] == 2
    assert qm["edit_convergence_rate"] == 1.0
    assert qm["sessions_with_edits"] == 1
    assert qm["sessions_with_tests"] == 1
    assert qm["quality_score"] == 100
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
    assert qm["scored"] is True
    assert qm["verification_rate"] == 0.0  # Zero tests run
    assert qm["sessions_with_edits"] == 1
    assert qm["sessions_with_tests"] == 0
    assert qm["thrashed_files_count"] == 1
    assert qm["thrashed_files_list"] == [
        {"path": "src/flaky.py", "edits": 4, "sessions": 1}
    ]
    # The only file edited was thrashed, so nothing converged.
    assert qm["files_edited"] == 1
    assert qm["edit_convergence_rate"] == 0.0
    assert qm["tool_error_rate"] > 0.0
    # Verification 0 and convergence 0 leave only the 20% tool-success weight.
    assert qm["quality_score"] < 30
    assert qm["grade"] == "F"


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
    assert qm["quality_score"] == 100

    # Prometheus export check
    prom_text = format_prometheus_metrics(payload)
    assert 'ace_quality_score{agent="all"} 100' in prom_text
    assert 'ace_quality_verification_rate{agent="all"} 1.0' in prom_text
    assert 'ace_quality_edit_convergence_rate{agent="all"} 1.0' in prom_text
    assert 'ace_quality_tool_success_rate{agent="all"} 1.0' in prom_text
    assert "ace_quality_thrashed_files_total 0" in prom_text
    assert "ace_quality_files_edited_total 1" in prom_text

    # Render dashboard check
    html = render(payload)
    assert "CODE QUALITY &amp; RELIABILITY" in html or "CODE QUALITY & RELIABILITY" in html
    assert "quality_score" in html
    assert "verification_rate" in html
    assert "edit_convergence" in html
    assert "tool_success" in html


def _turns(model: str, calls_first: List[Dict[str, Any]], n: int = 25) -> List[Dict[str, Any]]:
    """``n`` turns of one model, the tool calls all on the first."""
    return [
        {
            "model": model,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 0,
            "calls": calls_first if i == 0 else [],
        }
        for i in range(n)
    ]


def test_quality_metrics_by_agent_and_model() -> None:
    # A row needs MIN_QUALITY_EVAL_SESSIONS sessions *and* MIN_QUALITY_EVAL_TURNS turns:
    # 5 sessions x 25 turns clears both for each engine.
    sess: List[Dict[str, Any]] = []
    for i in range(5):
        sess.append(
            {
                "session": f"c{i}",
                "agent_type": "claude",
                "cwds": ["/test"],
                "events": [],
                "turns": _turns(
                    "claude-sonnet-4-6",
                    [
                        {"name": "Edit", "raw_target": f"src/a{i}.py", "is_edit": True, "is_src_file": True},
                        {"name": "Bash", "command": "pytest", "is_test_run": True, "is_error": False},
                    ],
                ),
            }
        )
        sess.append(
            {
                "session": f"g{i}",
                "agent_type": "antigravity",
                "cwds": ["/test"],
                "events": [],
                "turns": _turns(
                    "gemini-3.6-flash",
                    [
                        {"name": "write_to_file", "raw_target": f"src/b{i}.py", "is_edit": True, "is_src_file": True, "is_error": True},
                    ],
                ),
            }
        )

    qm = quality_metrics(sess)
    assert "claude" in qm["by_agent"]
    assert "antigravity" in qm["by_agent"]

    claude_q = qm["by_agent"]["claude"]
    assert claude_q["sessions"] == 5
    assert claude_q["verification_rate_pct"] == 100.0
    assert claude_q["edit_convergence_rate_pct"] == 100.0
    assert claude_q["quality_score"] == 100

    agy_q = qm["by_agent"]["antigravity"]
    assert agy_q["verification_rate_pct"] == 0.0
    assert agy_q["tool_success_rate_pct"] == 0.0
    # Every edit landed first try, so convergence is clean even though nothing verified.
    # It is diagnostic, so a perfect convergence rate does not lift the score at all.
    assert agy_q["edit_convergence_rate_pct"] == 100.0
    assert agy_q["quality_score"] == 0

    models = [m["model"] for m in qm["by_model"]]
    assert "claude-sonnet-4-6" in models
    assert "gemini-3.6-flash" in models

    # Dashboard render with comparative table
    payload = _build_payload(sess, capture=None, range_key="all", agent="all", store_path=None)
    html = render(payload)
    assert "Claude Code" in html or "claude" in html
    assert "claude-sonnet-4-6" in html
    assert "gemini-3.6-flash" in html
    assert "ENGINE / MODEL" in html or "Engine / model" in html
    assert "Convergence" in html


def test_quality_metrics_unscored_slice_reports_no_score() -> None:
    """A read-only slice has no code quality to report, and must not read as a perfect one."""
    sess: List[Dict[str, Any]] = [
        {
            "session": f"r{i}",
            "agent_type": "claude",
            "cwds": ["/test"],
            "events": [],
            "turns": _turns(
                "claude-sonnet-4-6",
                [{"name": "view_file", "raw_target": "src/server.py", "is_view": True}],
            ),
        }
        for i in range(5)
    ]

    qm = quality_metrics(sess)
    assert qm["available"] is True
    assert qm["scored"] is False
    assert qm["quality_score"] is None
    assert qm["grade"] == "—"
    assert qm["sessions_with_edits"] == 0

    payload = _build_payload(sess, capture=None, range_key="all", agent="all", store_path=None)
    # No fabricated series for a slice with nothing to score.
    assert 'ace_quality_score{agent="all"}' not in format_prometheus_metrics(payload)
    html = render(payload)
    assert "no editing sessions in scope" in html


def test_quality_metrics_convergence_counts_session_file_pairs() -> None:
    """A file thrashed once and clean elsewhere is one bad pair, not one bad file."""

    def _sess(sid: str, edits: int) -> Dict[str, Any]:
        return {
            "session": sid,
            "agent_type": "claude",
            "cwds": ["/test"],
            "events": [],
            "turns": [
                {
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "calls": [
                        {"name": "Edit", "raw_target": "src/hot.py", "is_edit": True, "is_src_file": True}
                        for _ in range(edits)
                    ],
                }
            ],
        }

    # Same path across four sessions; only one of them thrashed it.
    qm = quality_metrics([_sess("s1", 4), _sess("s2", 1), _sess("s3", 1), _sess("s4", 1)])
    assert qm["files_edited"] == 4
    assert qm["thrashed_files_count"] == 1
    assert qm["edit_convergence_rate"] == 0.75
    # One path, thrashed in one of the four sessions, worst pass count carried through.
    assert qm["thrashed_files_list"] == [
        {"path": "src/hot.py", "edits": 4, "sessions": 1}
    ]
    assert qm["thrashed_files_distinct"] == 1


def test_quality_metrics_excludes_agent_scratch_files() -> None:
    """Antigravity plan scratch files go through the edit tools but are not code."""
    sess: List[Dict[str, Any]] = [
        {
            "session": "s1",
            "agent_type": "antigravity",
            "cwds": ["/test"],
            "events": [],
            "turns": [
                {
                    "model": "gemini-3.6-flash",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "calls": [
                        {"name": "write_to_file", "raw_target": "/home/u/.gemini/antigravity/brain/x/notes.md", "is_edit": True},
                        {"name": "write_to_file", "raw_target": "/repo/implementation_plan.md", "is_edit": True},
                        {"name": "write_to_file", "raw_target": "/repo/walkthrough.md", "is_edit": True},
                    ] * 3,
                }
            ],
        }
    ]
    qm = quality_metrics(sess)
    # The session still counts as editing, but no scratch file enters convergence.
    assert qm["sessions_with_edits"] == 1
    assert qm["files_edited"] == 0
    assert qm["thrashed_files_count"] == 0
    assert qm["edit_convergence_rate"] == 1.0


def test_quality_metrics_turn_threshold_filter() -> None:
    # One short session clears neither gate, so it gets no comparison row of its own.
    short_sess = [
        {
            "session": "short_s1",
            "agent_type": "codex",
            "cwds": ["/test"],
            "turns": [
                {
                    "model": "gpt-5.3-codex",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "calls": [],
                }
                for _ in range(5)  # 1 session, 5 turns
            ],
            "events": [],
        }
    ]

    qm = quality_metrics(short_sess)
    assert "codex" not in qm["by_agent"]
    assert len(qm["by_model"]) == 0

    # Turns alone are not enough either: one very long session is still one session.
    long_single = [dict(short_sess[0], turns=short_sess[0]["turns"] * 40)]
    assert len(long_single[0]["turns"]) > MIN_QUALITY_EVAL_TURNS
    assert "codex" not in quality_metrics(long_single)["by_agent"]


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
    assert 'ace_quality_category_verification_rate{category="ui"}' in prom_text
    # The research session edits nothing, so it contributes no score series.
    assert 'ace_quality_category_score{category="research"}' not in prom_text

    # Render dashboard
    html = render(payload)
    assert (
        "CAPABILITY &amp; PERFORMANCE BY CODING TASK DOMAIN" in html
        or "Capability by coding task domain" in html
    )
    assert "UI &amp; Frontend" in html or "UI & Frontend" in html


def test_quality_score_weighting() -> None:
    """The composite is exactly the three published weights over the three rates."""
    sess: List[Dict[str, Any]] = [
        {
            "session": "s1",
            "agent_type": "claude",
            "cwds": ["/test"],
            "events": [],
            "turns": [
                {
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "calls": [
                        # Four files: two thrashed, two clean -> convergence 0.5.
                        {"name": "Edit", "raw_target": "a.py", "is_edit": True},
                        {"name": "Edit", "raw_target": "a.py", "is_edit": True},
                        {"name": "Edit", "raw_target": "a.py", "is_edit": True},
                        {"name": "Edit", "raw_target": "b.py", "is_edit": True},
                        {"name": "Edit", "raw_target": "b.py", "is_edit": True},
                        {"name": "Edit", "raw_target": "b.py", "is_edit": True},
                        {"name": "Edit", "raw_target": "c.py", "is_edit": True},
                        {"name": "Edit", "raw_target": "d.py", "is_edit": True},
                        # 8 edits + 2 more calls, one of which errors -> tool success 0.9.
                        {"name": "Bash", "command": "pytest", "is_test_run": True},
                        {"name": "Bash", "command": "ls", "is_error": True},
                    ],
                }
            ],
        }
    ]
    qm = quality_metrics(sess)
    assert qm["verification_rate"] == 1.0  # the one editing session ran pytest
    assert qm["edit_convergence_rate"] == 0.5
    assert qm["tool_success_rate"] == 0.9
    # 0.70(1.0) + 0.30(0.9) = 0.97. Convergence is 0.5 and does not enter the score.
    assert qm["quality_score"] == int(round((0.70 * 1.0 + 0.30 * 0.9) * 100.0))
    assert qm["quality_score"] == 97
    assert qm["grade"] == "A"
    assert qm["tool_error_rate_pct"] == 10.0


def test_convergence_is_diagnostic_not_scored() -> None:
    """Two slices identical but for convergence must score the same."""
    from ace.sidecar.insights import _DIAGNOSTIC_RATES, _QUALITY_WEIGHTS

    assert not set(_DIAGNOSTIC_RATES) & {k for k, _ in _QUALITY_WEIGHTS}
    assert round(sum(w for _, w in _QUALITY_WEIGHTS), 6) == 1.0

    def _build(edits_per_file: int) -> Dict[str, Any]:
        calls: List[Dict[str, Any]] = []
        for f in range(4):
            calls += [
                {"name": "Edit", "raw_target": f"src/f{f}.py", "is_edit": True}
                for _ in range(edits_per_file)
            ]
        calls.append({"name": "Bash", "command": "pytest", "is_test_run": True})
        return {
            "session": "s1",
            "agent_type": "claude",
            "cwds": ["/test"],
            "events": [],
            "turns": [
                {
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                    "calls": calls,
                }
            ],
        }

    clean = quality_metrics([_build(1)])
    thrashed = quality_metrics([_build(9)])

    assert clean["edit_convergence_rate"] == 1.0
    assert thrashed["edit_convergence_rate"] == 0.0
    # Wildly different convergence, identical verification and tool success -> same score.
    assert clean["quality_score"] == thrashed["quality_score"] == 100
    # ...but the diagnostic still names the offenders.
    assert thrashed["thrashed_files_distinct"] == 4
    assert clean["thrashed_files_list"] == []


def test_thrash_list_is_ranked_by_severity_and_capped() -> None:
    """The panel is evidence, so the worst offender must not sort below an 'a' filename."""
    from ace.sidecar.insights import _THRASH_LIST_MAX

    def _edits(path: str, n: int) -> List[Dict[str, Any]]:
        return [{"name": "Edit", "raw_target": path, "is_edit": True} for _ in range(n)]

    calls: List[Dict[str, Any]] = _edits("zzz_worst.py", 40)
    # Fifteen mildly thrashed files that all sort ahead of it alphabetically.
    for i in range(15):
        calls += _edits(f"aaa_{i:02}.py", 3)

    qm = quality_metrics(
        [
            {
                "session": "s1",
                "agent_type": "claude",
                "cwds": ["/test"],
                "events": [],
                "turns": [
                    {
                        "model": "claude-sonnet-4-6",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                        "calls": calls,
                    }
                ],
            }
        ]
    )

    listed = qm["thrashed_files_list"]
    assert len(listed) == _THRASH_LIST_MAX
    assert listed[0] == {"path": "zzz_worst.py", "edits": 40, "sessions": 1}
    assert qm["thrashed_files_distinct"] == 16
    assert [f["edits"] for f in listed] == sorted(
        (f["edits"] for f in listed), reverse=True
    )


def test_edit_targets_normalise_quoting() -> None:
    """`"/a/b.py"` and `/a/b.py` are one file, and the quoted form is still source."""
    from ace.sidecar.insights import _classify_call, _norm_path

    assert _norm_path('"/repo/src/app.py"') == "/repo/src/app.py"
    assert _norm_path("'/repo/src/app.py'") == "/repo/src/app.py"
    assert _norm_path("  /repo/src/app.py  ") == "/repo/src/app.py"

    quoted = _classify_call("Edit", {"file_path": '"/repo/src/app.py"'})
    bare = _classify_call("Edit", {"file_path": "/repo/src/app.py"})
    assert quoted["raw_target"] == bare["raw_target"]
    # The trailing quote used to defeat the extension check entirely.
    assert quoted["is_src_file"] is True
    assert quoted["target"] == bare["target"] if "target" in quoted else True

    # Two spellings of one file are one file, and together they thrash it.
    calls = [
        {"name": "Edit", "raw_target": _classify_call("Edit", {"file_path": p})["raw_target"], "is_edit": True}
        for p in ('"/repo/a.py"', "/repo/a.py", '"/repo/a.py"')
    ]
    qm = quality_metrics(
        [
            {
                "session": "s1",
                "agent_type": "claude",
                "cwds": ["/test"],
                "events": [],
                "turns": [
                    {
                        "model": "claude-sonnet-4-6",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                        "calls": calls,
                    }
                ],
            }
        ]
    )
    assert qm["files_edited"] == 1
    assert qm["thrashed_files_count"] == 1
    assert qm["edit_convergence_rate"] == 0.0
