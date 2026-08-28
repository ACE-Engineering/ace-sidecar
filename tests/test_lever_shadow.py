"""The live path: counting with the in-flight credential, and measuring a proxied turn.

This is the half that makes "measured" true rather than simulated, so the tests are mostly
about the two things that could quietly make it false:

* the relayed request must go upstream byte-for-byte, whatever a lever proposes;
* a token delta must be *counted*, and a credential the counting endpoint refuses must
  produce an explanation rather than a blank.

No live provider call is made anywhere here. The upstream relay and the counting endpoint are
both driven through ``httpx.MockTransport``, which is the same discipline the rest of this
route's suite uses.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI

from ace.gateway.local_store import LocalStore
from ace.gateway.messages import MessagesConfig, install_messages_route
from ace.gateway.messages_auth import MODE_LOOPBACK, AuthConfig
from ace.sidecar import levers as L
from ace.sidecar.levers import rail
from ace.sidecar.levers.counter import AnthropicCounter, resolve_counter
from ace.sidecar.levers.shadow import (
    ShadowRunner,
    apply_edits,
    body_to_session,
    price_delta,
)
from ace.sidecar.levers.types import Usage

DUMP = "ERROR line\n" * 3000

# The splice mechanics live in `ace-skills`, which the sidecar deliberately does not depend on
# (see `test_the_sidecar_still_works_with_no_lever_package_installed`). Without it `actuate`
# refuses by design and returns the original bytes, so the two tests that assert bytes actually
# CHANGED have nothing to observe — skip them rather than assert a behaviour this install
# cannot have. Everything else here, actuation's refusals included, still runs.
needs_splice = pytest.mark.skipif(
    importlib.util.find_spec("ace_skills") is None,
    reason="actuation needs ace-skills' splice mechanics",
)


def body(dump=DUMP):
    """A Claude Code shaped turn: one tool call, one big result, a system prompt and tools."""
    return {
        "model": "claude-sonnet-5", "max_tokens": 1024, "system": "You are a coding agent.",
        "tools": [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "find the bug"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "Bash",
                 "input": {"command": "cat big.log"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": dump}]},
        ],
    }


class Truncate:
    id, label = "truncate_dumps", "Truncate large tool dumps"
    risk, requires_content = L.RISK_LOW, False

    def propose(self, session, ctx):
        cap = int(ctx.settings.get("keep_bytes", 2048))
        return L.Proposal(self.id, tuple(
            L.Edit(turn_index=t, call_index=c, kind="truncate", reason="over cap",
                   keep_bytes=cap)
            for t, c, x in session.iter_calls() if x.result_bytes > cap
        ), {"scanned": session.n_turns})


class LoopGuard:
    id, label = "loop_guard", "Loop guardrail"
    risk, requires_content = L.RISK_NONE, False

    def propose(self, session, ctx):
        return L.Proposal(self.id, (), {"loops_detected": 2, "detail": "dropped: not a number"})


def counting_transport(calls=None):
    """A stand-in counting endpoint: tokens == serialized bytes / 4."""
    def handler(req):
        if calls is not None:
            calls.append(json.loads(req.content))
        return httpx.Response(200, json={"input_tokens": len(req.content) // 4})
    return httpx.MockTransport(handler)


def upstream_transport(seen=None):
    def handler(req):
        if seen is not None:
            seen["body"] = req.content
            seen["headers"] = dict(req.headers)
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-sonnet-5", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1200, "output_tokens": 90,
                      "cache_read_input_tokens": 30000,
                      "cache_creation_input_tokens": 12000}})
    return httpx.MockTransport(handler)


# -- the counter --------------------------------------------------------------------------


def test_no_credential_is_an_ordinary_outcome_with_a_reason():
    """`no_key: true` is this sidecar's own default, so this is the common state."""
    counter, note = resolve_counter(None)
    if counter is None:                       # no key exported in this environment
        assert "no credential available" in note
    else:                                     # a developer who did export one
        assert "count_tokens" in note


def test_an_oauth_token_is_presented_as_bearer_with_the_required_beta():
    """An OAuth token sent as `x-api-key` is rejected, and /v1/messages needs the beta."""
    seen = []
    def handler(req):
        seen.append(dict(req.headers))
        return httpx.Response(200, json={"input_tokens": 7})
    c = AnthropicCounter("sk-ant-oat-tok", "bearer",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c("hello", model="claude-sonnet-5") == 7
    assert "authorization" in seen[0]
    assert "x-api-key" not in seen[0]
    assert seen[0]["anthropic-beta"] == "oauth-2025-04-20"


def test_count_body_strips_fields_the_endpoint_rejects():
    calls = []
    c = AnthropicCounter("k", "api_key", client=httpx.Client(transport=counting_transport(calls)))
    c.count_body({"model": "m", "messages": [], "system": "S", "tools": [],
                  "stream": True, "max_tokens": 5, "temperature": 0.7, "metadata": {}})
    assert sorted(calls[0]) == ["messages", "model", "system", "tools"]


def test_a_refused_credential_latches_and_explains():
    """The OAuth-vs-API-key question is settled here, as a side effect of the first count —
    there is no preflight probe. A refusal must be remembered, not re-asked every turn."""
    n = {"i": 0}
    def deny(req):
        n["i"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})
    c = AnthropicCounter("sk-ant-oat-tok", "bearer",
                         client=httpx.Client(transport=httpx.MockTransport(deny)))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            c("x", model="m")
    assert n["i"] == 1, "a definitive refusal must stop the calling"
    assert not c.usable
    assert "OAuth token" in c.note and "401" in c.note


def test_a_throttle_does_not_disable_counting():
    """A 429 says nothing about the credential. Latching on it would look exactly like the
    feature not working."""
    n = {"i": 0}
    def throttle(req):
        n["i"] += 1
        return httpx.Response(429, json={})
    c = AnthropicCounter("k", "api_key",
                         client=httpx.Client(transport=httpx.MockTransport(throttle)))
    for _ in range(3):
        with pytest.raises(Exception):
            c("x", model="m")
    assert n["i"] == 3
    assert c.usable


# -- wire format -> typed model -----------------------------------------------------------


def test_body_adapts_to_the_same_model_a_transcript_does():
    s, anchors = body_to_session(body(), session_id="s1")
    assert s.agent == "claude" and s.n_turns == 1
    (ti, ci, call), = list(s.iter_calls())
    assert (ti, ci) == (0, 0) and call.name == "Bash"
    assert call.result_bytes == len(DUMP)
    # THE difference from the transcript path: real bytes are in hand.
    assert call.has_content and call.content.resolve() == DUMP
    assert anchors[(0, 0)].is_tail


def test_the_newest_result_is_the_prefix_safe_one():
    b = body()
    b["messages"] += [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_2", "name": "Read", "input": {"file_path": "/a"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_2", "content": "short"}]},
    ]
    _, anchors = body_to_session(b)
    assert not anchors[(0, 0)].is_tail, "already cached history"
    assert anchors[(1, 0)].is_tail, "produced this turn, not yet written to cache"


def test_historical_turns_carry_no_fabricated_usage():
    """The body does not record what earlier turns were billed, and inventing a plausible
    number is what would make the ledger's arithmetic silently wrong."""
    u = Usage(input_tokens=1200, cache_read_tokens=30000, cache_write_tokens=12000)
    b = body()
    b["messages"] += [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_2", "name": "Read", "input": {"file_path": "/a"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_2", "content": "short"}]},
    ]
    s, _ = body_to_session(b, usage=u)
    assert s.turns[0].usage.prompt_tokens == 0
    assert s.turns[-1].usage.prompt_tokens == u.prompt_tokens


# -- the counterfactual --------------------------------------------------------------------


def test_apply_edits_never_mutates_the_request():
    """Shadow means shadow. The relayed body and the counterfactual share no mutated object."""
    b = body()
    _, anchors = body_to_session(b)
    new, applied, skipped = apply_edits(
        b, [L.Edit(turn_index=0, call_index=0, kind="truncate", reason="r", keep_bytes=2048)],
        anchors,
    )
    assert len(b["messages"][2]["content"][0]["content"]) == len(DUMP)
    assert len(new["messages"][2]["content"][0]["content"]) == 2048
    assert [e.applied for e in applied] == [True] and skipped == 0


def test_a_list_shaped_result_stays_a_list():
    b = body(dump=None)
    b["messages"][2]["content"][0]["content"] = [{"type": "text", "text": "y" * 9000}]
    _, anchors = body_to_session(b)
    new, _, _ = apply_edits(
        b, [L.Edit(turn_index=0, call_index=0, kind="truncate", reason="r", keep_bytes=1000)],
        anchors,
    )
    out = new["messages"][2]["content"][0]["content"]
    assert isinstance(out, list) and len(out[0]["text"]) == 1000


def test_expire_changes_no_bytes_and_says_so():
    """Volume levers and accounting levers must not share a number."""
    b = body()
    _, anchors = body_to_session(b)
    new, applied, skipped = apply_edits(
        b, [L.Edit(turn_index=0, call_index=0, kind="expire", reason="stale", live_until=3)],
        anchors,
    )
    assert skipped == 1
    assert not applied[0].applied
    assert "not priced on this path" in applied[0].note
    assert new["messages"][2]["content"][0]["content"] == DUMP


# -- pricing one live turn ------------------------------------------------------------------


def test_removed_tokens_are_drawn_newest_bucket_first():
    """The allocation IS the pricing argument: the end of an agent prompt is the part that
    was not served from cache, and the same delta is worth ~12x more coming out of a write."""
    from ace.gateway.pricing import Rates
    r = Rates(model="m", input_per_mtok=3.0, output_per_mtok=15.0,
              cache_read_per_mtok=0.30, source="t", as_of="x")
    u = Usage(input_tokens=1200, cache_read_tokens=30000, cache_write_tokens=12000,
              cache_write_by_ttl={"5m": 12000})

    usd, w, i, rd = price_delta(5_000, u, r)
    assert (w, i, rd) == (5_000, 0, 0)
    assert usd == pytest.approx(5_000 / 1e6 * r.cache_write_per_mtok("5m"))

    _, w, i, rd = price_delta(13_000, u, r)
    assert (w, i, rd) == (12_000, 1_000, 0)

    _, w, i, rd = price_delta(20_000, u, r)
    assert (w, i, rd) == (12_000, 1_200, 6_800), "the overflow falls back to the cache rate"


def test_an_unpriced_model_yields_no_dollars():
    assert price_delta(5_000, Usage(cache_write_tokens=9_000), None) == (0.0, 0, 0, 0)


# -- the whole path, through the real route --------------------------------------------------


def make_app(store, *, seen=None, config=None, levers=(Truncate, LoopGuard)):
    runner = ShadowRunner(
        config=config or {"levers": {"truncate_dumps": "shadow", "loop_guard": "shadow"}},
        sink=store.record_lever_turns,
    )
    runner._levers = tuple(L.RegisteredLever(lever=k(), dist="ace-skills") for k in levers)
    runner.set_counter(
        AnthropicCounter("k", "api_key", client=httpx.Client(transport=counting_transport()))
    )
    app = FastAPI()
    install_messages_route(
        app,
        config=MessagesConfig(base_url="https://upstream.test", timeout_s=5),
        client=httpx.AsyncClient(transport=upstream_transport(seen)),
        auth_config=AuthConfig(mode=MODE_LOOPBACK, local_api_key="k"),
        accountant=store, shadow=runner,
    )
    return app, runner


async def drive(app, n=1, payload=None):
    sent = json.dumps(payload or body()).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        for _ in range(n):
            r = await c.post("/v1/messages", content=sent,
                             headers={"x-api-key": "k", "content-type": "application/json"})
            assert r.status_code == 200
    return sent


@pytest.fixture
def store(tmp_path):
    return LocalStore(str(tmp_path / "telemetry.db"))


async def settle(store, rows):
    for _ in range(100):
        await asyncio.sleep(0.01)
        if store.lever_summary()["rows"] >= rows:
            return
    raise AssertionError(f"shadow rows never reached {rows}")


def test_the_relayed_bytes_are_untouched_whatever_a_lever_proposes(store):
    """THE fidelity invariant. A lever that would strip 90% of the prompt must still leave
    the request that actually goes upstream byte-identical."""
    seen = {}
    app, _ = make_app(store, seen=seen)

    async def main():
        sent = await drive(app)
        await settle(store, 2)
        return sent

    sent = asyncio.run(main())
    assert seen["body"] == sent
    assert store.lever_summary()["by_lever"], "the lever did run"


def test_a_proxied_turn_is_measured_persisted_and_ranked(store):
    app, _ = make_app(store)

    async def main():
        await drive(app, n=3)
        await settle(store, 6)

    asyncio.run(main())
    s = store.lever_summary()
    assert s["rows"] == 6 and s["turns_observed"] == 3
    by = {r["lever"]: r for r in s["by_lever"]}

    trunc = by["truncate_dumps"]
    assert trunc["turns"] == 3 and trunc["removed_tokens"] > 0 and trunc["usd"] > 0
    assert trunc["edits_applied"] == 3
    # The dump is new this turn, so it comes out of the cache-write bucket.
    assert trunc["from_cache_write"] == trunc["removed_tokens"]

    # An edit-free lever is recorded, not dropped: "it ran and found nothing" is a result.
    assert by["loop_guard"]["turns"] == 3
    assert by["loop_guard"]["removed_tokens"] == 0

    assert "total_usd" not in s, "levers overlap; a total would exceed what they can deliver"


def test_the_store_keeps_numbers_only(store):
    """The one invariant of this store. Diagnostics are third-party authored and are the
    only field that could carry a developer's own text."""
    app, _ = make_app(store)

    async def main():
        await drive(app)
        await settle(store, 2)

    asyncio.run(main())
    got = {
        d for (d,) in sqlite3.connect(store.path).execute(
            "SELECT DISTINCT diagnostics FROM lever_turns"
        )
    }
    assert json.dumps({"loops_detected": 2}) in got
    assert not any("dropped: not a number" in (d or "") for d in got)


def test_real_spend_is_recorded_alongside_the_counterfactual(store):
    """`turns` and `lever_turns` are siblings: one is what was billed, the other is a prompt
    that was never sent. Both have to be there for the saving to mean anything."""
    app, _ = make_app(store)

    async def main():
        await drive(app, n=2)
        await settle(store, 4)

    asyncio.run(main())
    assert store.summary()["turns"] == 2
    assert store.summary()["cost_usd"] > 0


def test_the_rail_reports_measured_only_once_something_was_measured(store):
    app, _ = make_app(store)
    before = rail.rail_payload([], store=store)
    assert before["status"] in (rail.STATUS_NO_PACKAGE, rail.STATUS_ALL_OFF)
    assert before["measured"] == {}

    async def main():
        await drive(app)
        await settle(store, 2)

    asyncio.run(main())
    after = rail.rail_payload([], store=store)
    assert after["status"] == rail.STATUS_MEASURED
    assert after["turns_observed"] == 1
    assert {r["lever"] for r in after["measured"]["by_lever"]} == {
        "truncate_dumps", "loop_guard"
    }


@pytest.mark.parametrize(
    "discovered,expected_qualifier",
    [
        ((), "no lever package is installed now"),
        ("installed-but-off", "every installed lever is now off"),
    ],
)
def test_recorded_results_survive_the_lever_being_removed(
    store, monkeypatch, discovered, expected_qualifier
):
    """Recorded results and installed packages are independent facts, and they disagree in an
    ordinary way: a developer measures a lever for a week, then uninstalls or disables it.

    Dropping the rows would hide a real measurement behind a packaging detail; reporting them
    unqualified would imply the lever is still running. Both facts have to be said.

    Discovery is pinned rather than left to the environment — whether a lever package happens
    to be installed in the venv running the suite is not what this test is about.
    """
    app, _ = make_app(store)

    async def main():
        await drive(app)
        await settle(store, 2)

    asyncio.run(main())

    if discovered == "installed-but-off":
        discovered = (L.RegisteredLever(lever=Truncate(), dist="ace-skills"),)
    monkeypatch.setattr(rail, "_DISCOVERED", discovered)

    payload = rail.rail_payload([], store=store, config={})
    assert payload["status"] == rail.STATUS_MEASURED
    assert expected_qualifier in payload["note"]
    assert payload["measured"]["by_lever"], "the rows are still reported"


def test_nothing_enabled_costs_the_turn_nothing(store):
    """The ordinary state for the open-source sidecar: one cached entry-point lookup."""
    app, runner = make_app(store, config={"levers": {}})
    assert not runner.enabled

    async def main():
        await drive(app)
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert store.lever_summary()["rows"] == 0
    assert store.summary()["turns"] == 1, "accounting still happened"


# -- the re-read instrument -----------------------------------------------------------------


def read_body(calls):
    """`calls` is [(file_path, offset, result_size)] in order."""
    messages = [{"role": "user", "content": "go"}]
    for i, (path, off, size) in enumerate(calls):
        messages += [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"tu_{i}", "name": "Read",
                 "input": {"file_path": path, "offset": off}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"tu_{i}", "content": "x" * size}]},
        ]
    return {"model": "claude-sonnet-5", "messages": messages}


def truncating_edits(session, anchors):
    return [
        L.Edit(turn_index=t, call_index=c, kind="truncate", reason="big", keep_bytes=2048)
        for t, c, x in session.iter_calls()
        if x.result_bytes >= 4096 and (t, c) in anchors
    ]


def test_a_result_never_returned_to_is_not_a_reread():
    from ace.sidecar.levers.shadow import observe_revisits
    b = read_body([("/a.py", 0, 200_000), ("/b.py", 0, 200_000)])
    s, anchors = body_to_session(b)
    rv = observe_revisits(b, truncating_edits(s, anchors), anchors)
    assert rv.candidates == 2
    assert rv.revisited == 0
    assert rv.reread_rate == 0.0


def test_pagination_is_not_counted_as_a_reread():
    """A later Read at a HIGHER offset fetches fresh bytes from the file. It would have
    happened whether or not the earlier result was trimmed, so counting it as damage would
    condemn the lever for behaviour it did not cause. This distinction moved the observed
    rate on the reference corpus from 51.4% to 17.2% — across the break-even line."""
    from ace.sidecar.levers.shadow import observe_revisits
    b = read_body([("/a.py", 0, 200_000), ("/a.py", 500, 9_000)])
    s, anchors = body_to_session(b)
    rv = observe_revisits(b, truncating_edits(s, anchors), anchors)
    assert rv.revisited >= 1
    assert rv.paginated >= 1
    assert rv.same_or_earlier == 0
    assert rv.reread_rate == 0.0


def test_returning_to_the_same_region_is_a_reread():
    from ace.sidecar.levers.shadow import observe_revisits
    b = read_body([("/a.py", 0, 200_000), ("/a.py", 0, 200_000)])
    s, anchors = body_to_session(b)
    rv = observe_revisits(b, truncating_edits(s, anchors), anchors)
    assert rv.same_or_earlier >= 1
    assert rv.reread_rate > 0.0


def test_returning_to_an_earlier_region_counts_against_the_lever():
    from ace.sidecar.levers.shadow import observe_revisits
    b = read_body([("/a.py", 800, 200_000), ("/a.py", 0, 200_000)])
    s, anchors = body_to_session(b)
    rv = observe_revisits(b, truncating_edits(s, anchors), anchors)
    assert rv.same_or_earlier >= 1


def test_the_reread_rate_is_persisted_and_aggregated(store):
    """Without a row it is an anecdote. The rate is the input that decides whether the
    saving above it is worth anything."""
    class Trunc:
        id, label = "tail_truncation", "Cap oversized tool results"
        risk, requires_content = L.RISK_LOW, False
        def propose(self, session, ctx):
            return L.Proposal(self.id, tuple(
                L.Edit(turn_index=t, call_index=c, kind="truncate", reason="big",
                       keep_bytes=2048)
                for t, c, x in session.iter_calls() if x.result_bytes >= 4096))

    app, _ = make_app(store, config={"levers": {"tail_truncation": "shadow"}},
                      levers=(Trunc,))

    async def main():
        # first read is big and later returned to at the same offset -> one true re-read
        await drive(app, payload=read_body([("/a.py", 0, 200_000), ("/a.py", 0, 9_000)]))
        await settle(store, 1)

    asyncio.run(main())
    row = store.lever_summary()["by_lever"][0]
    assert row["revisit_candidates"] >= 1
    assert row["revisits"] >= 1, "the return to the same region must be recorded"


def test_an_old_database_gains_the_columns(tmp_path):
    """CREATE TABLE IF NOT EXISTS keeps the OLD shape, so a schema change alone never reaches
    a developer who has been running the sidecar."""
    import sqlite3
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE lever_turns (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
        " request_id TEXT, session_id TEXT, lever TEXT NOT NULL, mode TEXT, model TEXT,"
        " baseline_tokens INTEGER, counterfactual_tokens INTEGER, removed_tokens INTEGER,"
        " from_cache_write INTEGER, from_input INTEGER, from_cache_read INTEGER, usd REAL,"
        " priced INTEGER, prefix_safe INTEGER, edits_applied INTEGER, diagnostics TEXT,"
        " note TEXT)")
    db.commit(); db.close()

    s = LocalStore(path)   # must migrate, not raise
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(lever_turns)")}
    assert {"revisit_candidates", "revisits_observed", "revisits_paginated",
            "revisits_same_earlier"} <= cols
    assert s.lever_summary()["rows"] == 0


# -- actuation: byte-splice, the only path that changes what goes upstream -------------------


def cached_body(dump=DUMP):
    """A body shaped like a real cached agent turn: a breakpoint mid-history, a fresh tail."""
    return {
        "model": "claude-sonnet-5", "max_tokens": 1024,
        "system": [{"type": "text", "text": "agent", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "considering", "signature": "SIG-abc-123"},
                {"type": "tool_use", "id": "tu_1", "name": "Bash",
                 "input": {"command": "cat old.log"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": dump,
                 "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_2", "name": "Bash",
                 "input": {"command": "cat new.log"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_2", "content": dump}]},
        ],
    }


class TruncAll:
    """Proposes on EVERY oversized result, including cached history — so the splice guard,
    not the lever's good manners, is what has to refuse the unsafe one."""

    id, label = "tail_truncation", "Cap oversized tool results"
    risk, requires_content = L.RISK_LOW, False

    def propose(self, session, ctx):
        return L.Proposal(self.id, tuple(
            L.Edit(turn_index=t, call_index=c, kind="truncate", reason="big", keep_bytes=256)
            for t, c, x in session.iter_calls() if x.result_bytes >= 4096))


def test_actuation_is_off_unless_a_lever_says_on(store):
    """`on` is a word typed per lever. Shadow must never change a byte."""
    app, runner = make_app(store, config={"levers": {"tail_truncation": "shadow"}},
                           levers=(TruncAll,))
    assert not runner.actuating
    seen = {}
    app, runner = make_app(store, seen=seen, levers=(TruncAll,),
                           config={"levers": {"tail_truncation": "shadow"}})

    async def main():
        return await drive(app, payload=cached_body())

    sent = asyncio.run(main())
    assert seen["body"] == sent, "shadow mode must relay byte-for-byte"


@needs_splice
def test_actuation_splices_the_tail_and_leaves_the_cached_prefix_alone(store):
    """The whole design in one assertion: fewer bytes upstream, prefix untouched."""
    seen = {}
    app, runner = make_app(store, seen=seen, levers=(TruncAll,),
                           config={"levers": {"tail_truncation": {"mode": "on",
                                                                  "keep_bytes": 256}}})
    assert runner.actuating

    async def main():
        return await drive(app, payload=cached_body())

    sent = asyncio.run(main())
    got = seen["body"]
    assert len(got) < len(sent), "actuation must reduce the outbound body"

    orig, new = json.loads(sent), json.loads(got)
    # the cached history is untouched...
    assert new["messages"][2]["content"][0]["content"] == orig["messages"][2]["content"][0]["content"]
    # ...and the fresh tail is trimmed
    assert len(new["messages"][4]["content"][0]["content"]) < len(DUMP)

    # everything the cache holds is byte-for-byte identical
    from ace_skills.splice import last_breakpoint_offset
    bp = last_breakpoint_offset(sent)
    assert bp > 0
    assert got[: bp + 1] == sent[: bp + 1], "bytes at or before the breakpoint changed"


def test_actuation_preserves_the_fields_a_round_trip_would_lose(store):
    """The reason this splices instead of re-serializing. `cache_control` markers and
    extended-thinking signatures have no slot in any intermediate representation; losing the
    first means nothing is cached at all, losing the second gets the turn rejected."""
    seen = {}
    app, _ = make_app(store, seen=seen, levers=(TruncAll,),
                      config={"levers": {"tail_truncation": {"mode": "on",
                                                             "keep_bytes": 256}}})

    async def main():
        return await drive(app, payload=cached_body())

    asyncio.run(main())
    new = json.loads(seen["body"])
    assert new["messages"][1]["content"][0]["signature"] == "SIG-abc-123"
    assert new["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert new["messages"][2]["content"][0]["cache_control"] == {"type": "ephemeral"}


@needs_splice
def test_a_splice_before_the_breakpoint_is_refused_not_applied(store):
    """The guard is a byte-offset comparison, not a claim about the lever's intent — TruncAll
    proposes on cached history and the splice layer is what says no."""
    from ace.sidecar.levers.shadow import ShadowRunner
    from ace.sidecar.levers.counter import AnthropicCounter

    runner = ShadowRunner(config={"levers": {"tail_truncation": {"mode": "on",
                                                                 "keep_bytes": 256}}})
    runner._levers = (L.RegisteredLever(lever=TruncAll(), dist="test"),)
    body = cached_body()
    raw = json.dumps(body).encode()
    new_raw, info = runner.actuate(raw, body)

    assert new_raw is not None
    assert info["spliced"] == 1, "only the tail may be spliced"
    assert any("cache breakpoint" in r for r in info["refused"]), info["refused"]


def test_actuation_never_costs_a_turn_when_it_fails(store):
    """Any doubt returns the original bytes. A corrupted request is worse than an
    unoptimized one, and a lever's saving is never worth one."""
    class Exploding:
        id, label = "boom", "Boom"
        risk, requires_content = L.RISK_LOW, False
        def propose(self, session, ctx):
            raise ValueError("nope")

    seen = {}
    app, _ = make_app(store, seen=seen, levers=(Exploding,),
                      config={"levers": {"boom": {"mode": "on"}}})

    async def main():
        return await drive(app, payload=cached_body())

    sent = asyncio.run(main())
    assert seen["body"] == sent, "a failing lever must relay the original bytes"


def test_actuation_does_not_count_tokens_on_the_hot_path(store):
    """Counting is a network round trip. A lever reaching for it during actuation raises,
    is isolated, and the turn goes out unmodified rather than slowly."""
    calls = []

    class NeedsCount:
        id, label = "needs_count", "Needs counting"
        risk, requires_content = L.RISK_LOW, False
        def propose(self, session, ctx):
            calls.append(1)
            ctx.count_tokens("x", model="m")     # must raise
            return L.Proposal(self.id, ())

    from ace.sidecar.levers.shadow import ShadowRunner
    runner = ShadowRunner(config={"levers": {"needs_count": {"mode": "on"}}})
    runner._levers = (L.RegisteredLever(lever=NeedsCount(), dist="test"),)
    body = cached_body()
    new_raw, info = runner.actuate(json.dumps(body).encode(), body)
    assert calls, "the lever did run"
    assert new_raw is None, "a counting lever must not block or mutate the turn"


def test_the_sidecar_still_works_with_no_lever_package_installed(store, monkeypatch):
    """The dependency direction that matters. `ace-skills` supplies levers AND the splice
    mechanics; the sidecar must not require either. No package means no lever resolves to
    `on`, means actuation is never reached — and if it somehow is, it refuses rather than
    raising."""
    import builtins
    real = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("ace_skills"):
            raise ImportError(f"No module named {name!r}")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)

    from ace.sidecar.levers.shadow import ShadowRunner
    # TruncAll.id is "tail_truncation" — the config key must name the lever that is on, or
    # this test passes for the wrong reason (nothing enabled, so nothing to splice).
    runner = ShadowRunner(config={"levers": {"tail_truncation": {"mode": "on",
                                                                 "keep_bytes": 256}}})
    runner._levers = (L.RegisteredLever(lever=TruncAll(), dist="test"),)
    assert runner.actuating, "the lever must actually be on for this to prove anything"
    body = cached_body()
    new_raw, info = runner.actuate(json.dumps(body).encode(), body)
    assert new_raw is None, "no splice mechanics means the original bytes go out"
    assert any("ace-skills is not installed" in r for r in info["refused"])


# -- the rail must show a lever that was installed but has produced no number ----------------


def test_an_installed_lever_is_visible_even_with_no_measurement(store):
    """The bug this covers: discovery found the levers, the payload carried them, every mode
    resolved them — and the rail rendered only simulated headroom and measured rows, so a
    developer who installed a lever package saw no evidence of it anywhere on the page."""
    from ace.sidecar.dashboard_render import _lever_rail

    d = {
        "scorecards": {"standalone": []},
        "levers": {
            "status": "all_off",
            "installed": [
                {"id": "tail_truncation", "label": "Cap oversized tool results",
                 "risk": "LOW", "dist": "ace-skills", "requires_content": False},
                {"id": "image_stripping", "label": "Strip stale screenshots from history",
                 "risk": "MEDIUM", "dist": "ace-skills", "requires_content": True},
            ],
            "modes": {"tail_truncation": "off", "image_stripping": "shadow"},
            "measured": {},
        },
    }
    html = _lever_rail(d)
    assert "Cap oversized tool results" in html
    assert "Strip stale screenshots from history" in html
    assert "ace-skills" in html, "the providing package must be named"
    assert "OFF" in html and "SHADOW" in html, "the resolved mode is the actionable part"
    assert "needs the proxy path" in html, "a content-requiring lever must say so"


def test_a_measured_lever_is_not_listed_twice(store):
    """It already has a row above with a real figure; repeating it as 'installed' would imply
    two levers."""
    from ace.sidecar.dashboard_render import _lever_rail

    d = {
        "scorecards": {"standalone": []},
        "levers": {
            "status": "measured",
            "installed": [{"id": "tail_truncation", "label": "Cap oversized tool results",
                           "risk": "LOW", "dist": "ace-skills", "requires_content": False}],
            "modes": {"tail_truncation": "shadow"},
            "measured": {"by_lever": [
                {"lever": "tail_truncation", "turns": 3, "removed_tokens": 900, "usd": 0.5,
                 "unpriced_turns": 0, "revisit_candidates": 0, "revisits": 0,
                 "revisits_paginated": 0}]},
        },
    }
    html = _lever_rail(d)
    assert html.count("Cap oversized tool results") == 1
    assert "MEASURED" in html


def test_nothing_installed_adds_nothing(store):
    from ace.sidecar.dashboard_render import _lever_rail
    html = _lever_rail({"scorecards": {"standalone": []}, "levers": {}})
    assert "Installed levers" not in html
