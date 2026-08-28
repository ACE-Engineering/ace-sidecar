"""The ledger's arithmetic — the place a lever's proposal becomes money.

Every rate here is a stand-in, so these tests assert the *arithmetic* and not the shipped
price list. The counter is ``len(text) // 4`` for the same reason: exact expected token counts
are hand-checkable, which is what makes a wrong answer legible rather than merely red.

The properties under test are the three ways this arithmetic has already gone wrong in this
codebase: ignoring the cache-write penalty, summing levers that overlap, and rendering an
unpriced model as free.
"""

from __future__ import annotations

import pytest

from ace.gateway.pricing import Rates
from ace.sidecar import levers as L

RATES = Rates(model="m", input_per_mtok=3.0, output_per_mtok=15.0,
              cache_read_per_mtok=0.30, source="test", as_of="2026-08-27")
LOOKUP = lambda m: RATES if m == "m" else None  # noqa: E731

CTX = L.LeverContext(count_tokens=lambda t, *, model: len(t) // 4, mode=L.MODE_SHADOW)

BIG = 40_000
DUMP = "x" * BIG  # 10,000 tokens at 4 bytes/token

TRUNCATE = L.Proposal("truncate_dumps", (
    L.Edit(turn_index=0, call_index=0, kind="truncate", reason="dump", keep_bytes=4_000),
))
EXPIRE_AT_5 = L.Proposal("compaction", (
    L.Edit(turn_index=0, call_index=0, kind="expire", reason="stale", live_until=5),
))


def session(n_turns=10, model="m", prompt=50_000, cache_read=45_000, with_content=True):
    """`n` turns with one big Bash dump created at turn 0, prompt growing each turn."""
    turns = []
    for i in range(n_turns):
        calls = (
            [{"name": "Bash", "sig": "s0", "digest": "d0", "result_bytes": BIG}]
            if i == 0 else []
        )
        turns.append({
            "model": model, "ts": None, "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 0 if i == 0 else cache_read + i * 2_000,
            "cache_creation_input_tokens": prompt if i == 0 else 2_000,
            "calls": calls,
        })
    raw = {"session": "s1", "agent_type": "claude", "kind": "main", "turns": turns}
    content = (lambda ti, ci: (lambda: DUMP)) if with_content else None
    return L.from_corpus_session(raw, content_for=content)


# -- the prefix-safe case: a fresh dump, trimmed before it is ever cached ------------------


def test_prefix_safe_truncation_is_measured_and_free_of_penalty():
    e = L.price_proposal(session(10), TRUNCATE, CTX, rates_lookup=LOOKUP)
    assert e.fidelity == L.FIDELITY_MEASURED
    c = e.edits[0]
    assert c.removed_tokens == 10_000 - 1_000     # exact, counted, not inferred
    assert c.apply_at == 1 and c.turns_carried == 9
    assert c.prefix_safe and c.invalidated_tokens == 0
    assert c.cache_write_penalty_usd == pytest.approx(0.0)
    # Priced at the CACHE-READ rate: these tokens were resident in a cached prefix and
    # re-read each turn at ~0.1x. Valuing them at the input rate would inflate the lever 10x.
    assert c.gross_saving_usd == pytest.approx((9_000 / 1e6) * 0.30 * 9)
    assert c.net_usd == pytest.approx(c.gross_saving_usd)
    assert c.break_even_turn is None              # in profit immediately
    assert e.rate_sources == (("m", "test", "2026-08-27"),)


# -- the history-rewriting case: the penalty that makes a "saving" cost money --------------


def test_editing_cached_history_carries_a_penalty_and_can_be_a_net_loss():
    c = L.price_proposal(session(10), EXPIRE_AT_5, CTX, rates_lookup=LOOKUP).edits[0]
    assert c.apply_at == 6 and c.turns_carried == 4
    assert not c.prefix_safe, "editing cached history is never prefix-safe"
    assert c.invalidated_tokens > 0
    assert c.cache_write_penalty_usd > 0.0
    assert c.gross_saving_usd == pytest.approx((10_000 / 1e6) * 0.30 * 4)
    if c.net_usd < 0:
        assert c.break_even_turn is not None and c.break_even_turn > 10, (
            "a losing edit must break even beyond the session it ran in"
        )


def test_the_ttl_is_read_from_the_turn_not_assumed():
    """`strategies.TTL_SECONDS` hard-codes one hour while Claude Code defaults to 5m, and the
    two carry different write premiums (2x vs 1.25x)."""
    s = L.from_corpus_session({
        "session": "s2", "agent_type": "claude", "turns": [
            dict(model="m", input_tokens=100, cache_read_input_tokens=0,
                 cache_creation_input_tokens=50_000, ephemeral_1h_input_tokens=50_000,
                 output_tokens=50,
                 calls=[{"name": "Bash", "sig": "s", "result_bytes": BIG}]),
            *[dict(model="m", input_tokens=100, cache_read_input_tokens=45_000,
                   cache_creation_input_tokens=0, output_tokens=50, calls=[])
              for _ in range(9)],
        ]}, content_for=lambda ti, ci: (lambda: DUMP))
    c = L.price_proposal(s, EXPIRE_AT_5, CTX, rates_lookup=LOOKUP).edits[0]
    assert c.cache_write_ttl in ("5m", "1h")


# -- the three refusals -------------------------------------------------------------------


def test_no_content_means_no_dollars_and_it_says_why():
    """The refusal that keeps a measured claim measured: no `result_bytes / 4` fallback."""
    e = L.price_proposal(
        session(10, with_content=False), TRUNCATE, CTX, rates_lookup=LOOKUP
    )
    assert e.fidelity == L.FIDELITY_UNMEASURABLE
    assert e.edits == ()
    assert "cannot be counted exactly" in e.note
    assert e.net_usd == pytest.approx(0.0)


def test_an_unpriced_model_reports_real_tokens_and_absent_dollars():
    """A silent $0.00 looks like a cost win. Tokens are real; dollars are absent."""
    e = L.price_proposal(
        session(10, model="unknown-model"), TRUNCATE, CTX, rates_lookup=LOOKUP
    )
    assert e.fidelity == L.FIDELITY_UNPRICED
    assert not e.priced
    assert e.removed_tokens == 9_000
    assert e.net_usd == 0.0
    assert "not free" in e.note


def test_a_lever_firing_on_the_last_turn_saves_nothing_and_says_so():
    e = L.price_proposal(session(1), TRUNCATE, CTX, rates_lookup=LOOKUP)
    assert e.edits[0].turns_carried == 0
    assert e.edits[0].gross_saving_usd == pytest.approx(0.0)


def test_an_edit_free_proposal_is_measured_not_an_error():
    e = L.price_proposal(
        session(10), L.Proposal("loop_guard", (), {"loops_detected": 3}), CTX,
        rates_lookup=LOOKUP,
    )
    assert e.fidelity == L.FIDELITY_MEASURED
    assert e.diagnostics == {"loops_detected": 3}
    assert not e.priced and e.note == "lever proposed no edits"


# -- the report ranks, and refuses to total -----------------------------------------------


def test_the_report_ranks_and_has_no_total():
    """Two levers can target the same bytes; scored alone their figures overlap, so adding
    them produces a number larger than anything they could jointly deliver."""
    s = session(10)
    rep = L.price_all([(s, TRUNCATE), (s, EXPIRE_AT_5)], CTX, rates_lookup=LOOKUP)
    assert not hasattr(rep, "total_usd")
    ranked = rep.ranked()
    assert ranked[0][1] >= ranked[1][1]
    assert rep.unmeasured() == ()
