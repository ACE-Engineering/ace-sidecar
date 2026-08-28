"""The lever contract: the normalized model, mode resolution, and failure isolation.

Nothing here installs a real lever. The point of ``ace.sidecar.levers`` is that it defines
what a lever *is* and contains none, so these tests stand in local implementations and assert
the properties the package promises third-party code — above all that presence never implies
consent, and that one bad lever costs its own row rather than the dashboard.
"""

from __future__ import annotations

import logging

import pytest

from ace.sidecar import levers as L

# One session, three agents' worth of shape. `agent_type` is "codex" deliberately: the model
# is agent-neutral and a test that only ever exercises Claude Code would not prove it.
RAW = {
    "session": "s1", "agent_type": "codex", "kind": "main",
    "turns": [
        {"model": "gpt-5", "ts": "2026-08-27T10:00:00Z", "input_tokens": 100,
         "output_tokens": 20, "cache_read_input_tokens": 900,
         "cache_creation_input_tokens": 400, "ephemeral_5m_input_tokens": 400,
         "calls": [{"id": "t1", "name": "Bash", "sig": "abc", "target": "deadbeef",
                    "digest": "d1", "result_bytes": 240000}]},
        {"model": "gpt-5", "ts": "2026-08-27T10:01:00Z", "input_tokens": 50,
         "output_tokens": 10, "cache_read_input_tokens": 1400,
         "cache_creation_input_tokens": 0,
         "calls": [{"name": "Read", "sig": "xyz", "digest": "d1", "result_bytes": 1200}]},
    ],
}


class Truncator:
    """Reads sizes only, so it scores from transcripts alone — the common case."""

    id, label = "truncate_dumps", "Truncate large tool dumps"
    risk, requires_content = L.RISK_LOW, False

    def propose(self, session, ctx):
        cap = int(ctx.settings.get("keep_bytes", 4096))
        return L.Proposal(
            lever=self.id,
            edits=tuple(
                L.Edit(turn_index=ti, call_index=ci, kind="truncate",
                       reason="dump over cap", sig=c.sig, keep_bytes=cap)
                for ti, ci, c in session.iter_calls() if c.result_bytes > cap
            ),
            diagnostics={"scanned": session.n_turns},
        )


class NeedsBytes(Truncator):
    id, requires_content = "needs_bytes", True

    def propose(self, session, ctx):
        body = session.turns[0].calls[0].content.resolve()
        return L.Proposal(lever=self.id, diagnostics={"bytes": len(body)})


class Boom(Truncator):
    id = "boom"

    def propose(self, session, ctx):
        raise ValueError("bad session")


@pytest.fixture
def ctx():
    return L.LeverContext(
        count_tokens=lambda t, *, model: len(t) // 4,
        mode=L.MODE_SHADOW,
        settings={"keep_bytes": 4096},
    )


@pytest.fixture
def session():
    return L.from_corpus_session(RAW)


@pytest.fixture
def registered():
    return L.RegisteredLever(lever=Truncator(), dist="ace-skills")


# -- the normalized model ----------------------------------------------------------------


def test_one_adapter_serves_any_agent(session):
    assert (session.agent, session.n_turns, session.kind) == ("codex", 2, "main")
    assert session.turns[0].ts is not None
    assert list(session.iter_calls())[1][:2] == (1, 0)


def test_prompt_tokens_sums_the_cached_buckets(session):
    """`input_tokens` EXCLUDES the cached buckets, so this is a sum and not a max.

    Subtracting cache_read from input to "correct" it under-reports prompt volume — a bug
    with no visible symptom.
    """
    assert session.turns[0].usage.prompt_tokens == 100 + 900 + 400


def test_ttl_breakdown_is_provider_neutral(session):
    assert session.turns[0].usage.cache_write_by_ttl == {"5m": 400}


def test_measure_only_sessions_carry_no_content(session):
    assert session.turns[0].calls[0].content is None
    assert not session.turns[0].calls[0].has_content


# -- levers ------------------------------------------------------------------------------


def test_a_lever_scores_on_hashes_and_sizes_alone(registered, session, ctx):
    assert isinstance(registered.lever, L.Lever)
    p = L.propose_safely(registered, session, ctx)
    assert p is not None and len(p.edits) == 1
    assert p.edits[0].keep_bytes == 4096


def test_edit_free_proposal_is_a_real_result(session, ctx):
    """A loop guardrail's whole output is its diagnostics; `if proposal:` must not eat it."""
    p = L.Proposal("loop_guard", (), {"loops_detected": 3})
    assert not p.edits
    assert p.diagnostics == {"loops_detected": 3}


# -- presence is not consent -------------------------------------------------------------


@pytest.mark.parametrize(
    "config,expected",
    [
        ({}, L.MODE_OFF),
        ({"levers": {"truncate_dumps": "shadow"}}, L.MODE_SHADOW),
        ({"levers": {"truncate_dumps": {"mode": "on"}}}, L.MODE_ON),
        # A bare `true` is an enablement, and the safe reading of "enabled" is the mode that
        # changes nothing about the request.
        ({"levers": {"truncate_dumps": True}}, L.MODE_SHADOW),
    ],
)
def test_mode_resolution(registered, config, expected):
    assert L.resolve_modes([registered], config=config) == {"truncate_dumps": expected}


def test_an_installed_lever_defaults_to_off(registered):
    """The rule the registry exists to enforce. An unconfigured lever must never act."""
    assert L.resolve_modes([registered], config={}) == {"truncate_dumps": L.MODE_OFF}


def test_an_unknown_mode_resolves_to_off_not_to_the_default(registered, caplog):
    """A typo becoming `shadow` is tolerable; a typo becoming `on` is not."""
    with caplog.at_level(logging.WARNING):
        modes = L.resolve_modes([registered], config={"levers": {"truncate_dumps": "Bogus"}})
    assert modes == {"truncate_dumps": L.MODE_OFF}


def test_settings_exclude_mode():
    got = L.load_settings(
        "truncate_dumps",
        config={"levers": {"truncate_dumps": {"mode": "on", "keep_bytes": 99}}},
    )
    assert got == {"keep_bytes": 99}


# -- requires_content ---------------------------------------------------------------------


def test_content_requiring_lever_is_refused_not_raised(session, ctx):
    """Refused outright rather than allowed to half-run: a partial proposal is worse than
    none, because the ledger cannot tell it from a complete one."""
    nb = L.RegisteredLever(lever=NeedsBytes())
    assert L.propose_safely(nb, session, ctx) is None


def test_content_requiring_lever_runs_where_an_actuator_supplied_bytes(ctx):
    s = L.from_corpus_session(
        RAW, content_for=lambda ti, ci: (lambda: "x" * 240000) if ti == 0 else None
    )
    assert s.turns[0].calls[0].has_content
    assert not s.turns[1].calls[0].has_content
    p = L.propose_safely(L.RegisteredLever(lever=NeedsBytes()), s, ctx)
    assert p is not None and p.diagnostics == {"bytes": 240000}


def test_content_ref_refuses_in_measure_only_mode():
    with pytest.raises(L.ContentUnavailable):
        L.ContentRef().resolve()


# -- failure isolation --------------------------------------------------------------------


def test_a_throwing_lever_costs_its_own_row_and_nothing_else(session, ctx, caplog):
    with caplog.at_level(logging.WARNING):
        assert L.propose_safely(L.RegisteredLever(lever=Boom()), session, ctx) is None


def test_no_lever_package_is_the_ordinary_case():
    """An empty entry-point group is not an error state — it is the open-source default."""
    assert L.discover("ace.sidecar.levers.nonexistent") == ()
