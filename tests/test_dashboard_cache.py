"""The dashboard build cache, and the agent scoping that reads from it.

Switching the agent tab re-derives the whole payload from the same transcripts — mining
skill proposals and building the fleet table dominate a multi-second rebuild — so
``insights.build`` memoises the derived half. Two properties keep that safe, and both are
easy to lose in a refactor that only looks at wall-clock time:

* **A cached payload never outlives its data.** The key carries the transcript fingerprint,
  so appending a turn retires every entry derived from the old one.
* **The live telemetry counters are never cached.** They move on each proxied turn rather
  than when a transcript is written; caching them would freeze the one number on the page
  that is meant to be live.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

import pytest

from ace.sidecar import insights as I


class FakeStore:
    """A telemetry store whose counters move on every read, like the real one under load."""

    path = "/tmp/fake-telemetry.db"

    def __init__(self) -> None:
        self.reads = 0

    def summary(self) -> Dict[str, Any]:
        self.reads += 1
        return {"turns": self.reads}

    def recent(self, n: int) -> List[Dict[str, Any]]:
        return [{"ts": 0.0, "turn": self.reads}]


def _transcript(dirpath, session_id: str, model: str, ts: str) -> None:
    """One Claude Code transcript holding a single priced assistant turn."""
    d = dirpath / "project"
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session_id,
        "cwd": "/work/project",
        "message": {
            "model": model,
            "usage": {"input_tokens": 1000, "output_tokens": 200},
        },
    }
    with open(d / f"{session_id}.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    """Point the scanner at a private transcript tree with the caches cleared.

    ``sessions`` and ``session_files`` take the roots as *default arguments*, bound at
    definition time, and ``build`` calls them with no arguments — so rebinding the module
    attributes alone leaves both still reading the developer's real ``~/.claude``. The
    defaults themselves have to be replaced, or every assertion here silently runs against
    live data and proves nothing.
    """
    root = tmp_path / "claude"
    anti = tmp_path / "antigravity"
    codex = tmp_path / "codex"
    root.mkdir()
    anti.mkdir()
    codex.mkdir()
    monkeypatch.setattr(I, "TRANSCRIPT_ROOT", str(root))
    monkeypatch.setattr(I, "ANTIGRAVITY_ROOT", str(anti))
    monkeypatch.setattr(I, "CODEX_ROOT", str(codex))
    monkeypatch.setattr(I.sessions, "__defaults__", (str(root), str(anti), str(codex)))
    monkeypatch.setattr(
        I.session_files,
        "__defaults__",
        (str(root), str(anti), str(codex)) + I.session_files.__defaults__[3:],
    )

    def _reset() -> None:
        I._cache.update({"key": None, "sessions": None, "at": 0.0, "roots": None})
        I._build_cache.clear()

    _reset()
    yield root
    _reset()


def test_repeated_builds_reuse_the_cached_payload(transcripts):
    """A second toggle to the same agent must not re-derive anything."""
    _transcript(transcripts, "s1", "claude-opus-5", "2026-08-15T10:00:00Z")

    first = I.build(range_key="all", agent="claude")
    entries = len(I._build_cache)
    second = I.build(range_key="all", agent="claude")

    assert entries == 1
    assert len(I._build_cache) == 1, "a repeat build added an entry instead of hitting"
    assert first["historical"] == second["historical"]


def test_each_agent_and_range_caches_separately(transcripts):
    """Distinct selections are distinct entries — one must not serve another's numbers."""
    _transcript(transcripts, "s1", "claude-opus-5", "2026-08-15T10:00:00Z")

    for agent in ("all", "claude", "antigravity"):
        I.build(range_key="all", agent=agent)
    assert len(I._build_cache) == 3

    I.build(range_key="24h", agent="claude")
    assert len(I._build_cache) == 4


def test_a_new_turn_retires_the_cached_payload(transcripts):
    """The fingerprint is in the key, so appended transcripts cannot be served stale."""
    _transcript(transcripts, "s1", "claude-opus-5", "2026-08-15T10:00:00Z")
    before = I.build(range_key="all", agent="claude")["historical"]["turns"]

    # The freshness window has to lapse before the scanner will look at the tree again.
    time.sleep(I._SESSIONS_TTL + 0.1)
    _transcript(transcripts, "s2", "claude-opus-5", "2026-08-15T11:00:00Z")

    after = I.build(range_key="all", agent="claude")["historical"]["turns"]
    assert after > before, "a transcript written after the build was served from cache"


def test_live_counters_are_read_per_request_not_cached(transcripts):
    """The cached half is transcript-derived; telemetry must still be read every time."""
    _transcript(transcripts, "s1", "claude-opus-5", "2026-08-15T10:00:00Z")
    store = FakeStore()

    first = I.build(store=store, range_key="all", agent="claude")
    second = I.build(store=store, range_key="all", agent="claude")

    assert first["live"] != second["live"], "live counters were frozen by the cache"
    assert second["live"]["turns"] == 2
    cached = next(iter(I._build_cache.values()))
    assert "live" not in cached and "recent" not in cached, (
        "per-request keys leaked into the shared cached payload"
    )


def test_cache_is_bounded(transcripts, monkeypatch):
    """Entries keyed on a retired fingerprint must not accumulate for the process's life."""
    _transcript(transcripts, "s1", "claude-opus-5", "2026-08-15T10:00:00Z")
    monkeypatch.setattr(I, "_BUILD_CACHE_MAX", 3)

    for i in range(10):
        # A fresh capture summary each time is enough to move the key.
        I.build(capture={"n": i}, range_key="all", agent="claude")

    assert len(I._build_cache) <= 3


def test_selected_agent_is_the_only_card_rendered(transcripts):
    """Section 00 shows the chosen agent's cost, not every agent's."""
    from ace.sidecar.dashboard_render import render

    _transcript(transcripts, "s1", "claude-opus-5", "2026-08-15T10:00:00Z")
    store = FakeStore()

    both = render(I.build(store=store, range_key="all", agent="all"))
    only = render(I.build(store=store, range_key="all", agent="claude"))

    def cards(html: str) -> List[str]:
        section = html.split("HETEROGENEOUS AGENT ENV")[1].split("/ FLEET METRICS")[0]
        return [
            label
            for label in ("Claude Code", "Antigravity (Google)")
            if f"8px'>{label}<" in section
        ]

    assert cards(both) == ["Claude Code"] or "Claude Code" in cards(both)
    assert cards(only) == ["Claude Code"]
    assert "Antigravity (Google)" not in cards(only)


def test_session_files_are_scoped_to_the_selected_agent(transcripts):
    """The file list backing the page follows the same selection as the cards."""
    _transcript(transcripts, "s1", "claude-opus-5", "2026-08-15T10:00:00Z")

    assert I.session_files(agent="claude"), "the Claude transcript went missing"
    assert (
        I.session_files(agent="antigravity") == []
    ), "a Claude transcript was listed under Antigravity"


def test_home_directory_is_masked_everywhere_it_is_rendered(transcripts, monkeypatch):
    """No absolute home path reaches the page.

    The dashboard is what people screenshot to show what their agents cost, so an unmasked
    path publishes their account name — and, in the session list, the names of the private
    repositories they work in. Masking lives in one helper precisely so that a new call site
    cannot quietly opt out of it; this test fails if one does.
    """
    from ace.sidecar import dashboard_render as R

    home = os.path.expanduser("~")
    assert home and home != "/", "no home directory to mask"

    _transcript(transcripts, "s1", "claude-opus-5", "2026-08-15T10:00:00Z")
    store = FakeStore()
    html = R.render(I.build(store=store, range_key="all", agent="all"))

    assert home not in html, "an absolute home path was rendered"
    # Claude Code encodes a project path into the transcript filename with dashes, which is a
    # second way the same directory can appear.
    assert home.replace(os.sep, "-") not in html, "a dash-encoded home path was rendered"


def test_mask_home_handles_both_encodings_and_non_strings():
    from ace.sidecar.dashboard_render import _mask_home

    home = os.path.expanduser("~")
    assert _mask_home(f"{home}/.ace/telemetry.db") == "~/.ace/telemetry.db"
    assert _mask_home(home.replace(os.sep, "-") + "-Documents") == "~-Documents"
    assert _mask_home(None) == "None"
    assert _mask_home("/etc/hosts") == "/etc/hosts"
