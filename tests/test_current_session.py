"""The "current session" window: every session still in progress, else each agent's latest.

Picking the single session with the newest turn hid parallel work: two agents running at
once showed as one, and with Claude Code idle the window showed whichever agent spoke last.
"""

from __future__ import annotations

import datetime

from ace.sidecar import insights
from ace.sidecar.insights import AGENT_CLAUDE, AGENT_CODEX, filter_range


def _iso(hours_ago: float) -> str:
    now = datetime.datetime.now(datetime.UTC)
    return (now - datetime.timedelta(hours=hours_ago)).isoformat()


def _session(sid: str, agent: str, hours_ago: float) -> dict:
    return {"id": sid, "agent_type": agent, "turns": [{"ts": _iso(hours_ago)}]}


def _ids(sessions: list) -> set:
    return {s["id"] for s in sessions}


def test_every_session_with_a_turn_in_the_last_two_hours_is_current():
    sess = [
        _session("a", AGENT_CLAUDE, 0.1),
        _session("b", AGENT_CLAUDE, 1.5),
        _session("c", AGENT_CODEX, 0.5),
        _session("old", AGENT_CLAUDE, 5),
    ]
    assert _ids(filter_range(sess, insights.RANGES[insights.SESSION])) == {
        "a",
        "b",
        "c",
    }


def test_with_nothing_in_progress_each_agent_keeps_its_latest_session():
    sess = [
        _session("claude-new", AGENT_CLAUDE, 3),
        _session("claude-old", AGENT_CLAUDE, 30),
        _session("codex-new", AGENT_CODEX, 50),
        _session("codex-old", AGENT_CODEX, 90),
    ]
    assert _ids(filter_range(sess, -1.0)) == {"claude-new", "codex-new"}


def test_the_agent_filter_applies_before_the_selection():
    sess = [_session("a", AGENT_CLAUDE, 0.1), _session("c", AGENT_CODEX, 0.2)]
    assert _ids(filter_range(sess, -1.0, agent=AGENT_CODEX)) == {"c"}


def test_no_sessions_and_sessions_without_turns():
    assert filter_range([], -1.0) == []
    sess = [{"id": "empty", "agent_type": AGENT_CLAUDE, "turns": []}]
    assert _ids(filter_range(sess, -1.0)) == {"empty"}
