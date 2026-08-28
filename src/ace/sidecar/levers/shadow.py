"""ace.sidecar.levers.shadow — running levers for real, on a live proxied turn.

This is where "measured" stops being a promise. Everything else in this package either scores
transcripts (hashes and sizes, no text) or defines the contract; here the actual tool-result
bytes are in hand, the credential to count them is in flight, and the provider has just
reported what the turn really cost.

The three things this path has that the transcript path does not
---------------------------------------------------------------
1. **The bytes.** A ``/v1/messages`` request body carries every tool result verbatim. That is
   what lets a ``requires_content`` lever run at all, and what lets the token delta be
   *counted* rather than inferred from ``result_bytes / 4``.
2. **A credential.** Under ``no_key: true`` — the sidecar's own default — the OAuth token the
   proxy is about to relay is the only credential in the building. See
   :mod:`ace.sidecar.levers.counter`.
3. **Ground-truth usage.** The response says exactly how this turn's prompt was billed across
   fresh input, cache reads and cache writes. That split is what turns a token delta into a
   dollar figure without assuming anything.

Shadow means shadow
-------------------
The relayed bytes are never touched. Levers run against a **copy**, the counterfactual is
counted, priced and recorded, and the request the developer's agent actually made goes
upstream byte-for-byte — the invariant ``ace.gateway.messages`` exists to enforce ("parse to
decide, never parse to forward"). A lever resolved to ``on`` still does not mutate the request
here; actuation is a separate seam and deliberately not this one.

It also runs **after** the response, in a worker thread, so it costs the developer's turn
nothing. Counting is a network round trip and the counter is a synchronous client; doing
either on the hot path would trade a real latency regression for a number nobody asked to
wait for.

What is measured, and what is honestly not
------------------------------------------
Measured: the token delta between the real request body and the counterfactual, both counted
the same way through the provider's own counter. A constant offset in either count cancels;
the difference is exact.

Priced: that delta against **this turn's own usage split**, newest-bucket-first — see
:func:`price_delta`. No projection over future turns. A transcript-driven lever multiplies its
saving by the turns that carried the bytes, because it can see how the session ended; a live
turn cannot, and inventing that multiplier is how a one-turn saving becomes a headline number
that never arrives.

Not priced: ``expire`` edits, which change no bytes and therefore no prompt (see
``protocol.Edit``), and the cache-write penalty of an edit that rewrites already-cached
history, which is only observable on the *following* turn. Both are recorded and labelled
rather than guessed at.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ace.sidecar.levers.protocol import MODE_OFF, Edit, LeverContext, Proposal
from ace.sidecar.levers.registry import (
    RegisteredLever,
    discover,
    load_settings,
    propose_safely,
    resolve_modes,
)
from ace.sidecar.levers.types import ContentRef, Session, ToolCall, Turn, Usage

__all__ = [
    "TOOL_RESULT",
    "TOOL_USE",
    "Anchor",
    "LiveEdit",
    "TurnMeasurement",
    "body_to_session",
    "apply_edits",
    "price_delta",
    "ShadowRunner",
]

log = logging.getLogger(__name__)

TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"

_MTOK = 1_000_000.0

# What a dropped result is replaced by. Matches ``ledger._POINTER_TOKENS`` in intent: a
# reference, not nothing, because the applied lever does leave a marker behind and claiming
# otherwise would overstate the saving by exactly the marker.
_POINTER_TEXT = "[tool result elided by an ACE lever]"


@dataclass(frozen=True, slots=True)
class Anchor:
    """Where one ``ToolCall`` lives in the request body, so an edit can be applied back.

    The typed model addresses calls by ``(turn_index, call_index)`` — stable and
    agent-neutral — while the body addresses them by ``messages[m]["content"][b]``. Levers
    only ever see the former, so something has to hold the mapping; keeping it beside the
    session rather than inside it is what stops the wire format leaking into the contract
    every lever compiles against.
    """

    msg_index: int
    block_index: int
    # True for the newest result in the body. Its bytes have not yet been written to the
    # provider's cache, so editing it is prefix-safe and carries no invalidation penalty.
    is_tail: bool = False


@dataclass(frozen=True, slots=True)
class LiveEdit:
    """One edit, priced against the turn it would have changed."""

    lever: str
    kind: str
    reason: str
    turn_index: int
    call_index: int
    prefix_safe: bool = True
    applied: bool = True
    note: str = ""


@dataclass(frozen=True, slots=True)
class TurnMeasurement:
    """One lever's counterfactual for one live turn. The row that gets persisted.

    ``removed_tokens`` is the whole point and is exact. ``usd`` is exact *for this turn* and
    deliberately carries no forward projection.
    """

    lever: str
    mode: str
    model: str
    request_id: str = ""
    session_id: Optional[str] = None
    ts: float = 0.0

    baseline_tokens: int = 0
    counterfactual_tokens: int = 0
    removed_tokens: int = 0

    # How the removed tokens were allocated against this turn's real usage buckets.
    from_cache_write: int = 0
    from_input: int = 0
    from_cache_read: int = 0

    usd: float = 0.0
    priced: bool = True
    # The provider's own reported prompt size, kept purely as a cross-check on the baseline
    # count. Never used as an operand — see ``counter.count_body``.
    reported_prompt_tokens: int = 0

    edits: Tuple[LiveEdit, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""
    elapsed_ms: float = 0.0

    @property
    def measured(self) -> bool:
        return self.priced and self.removed_tokens > 0


# ---------------------------------------------------------------------------------------
# Wire format -> typed model
# ---------------------------------------------------------------------------------------


def _blocks(content: Any) -> List[Any]:
    """A message's content as a block list. A bare string is one implicit text block."""
    if isinstance(content, list):
        return content
    return [] if content is None else [content]


def _result_text(content: Any) -> str:
    """A tool result's textual payload, images excluded.

    Mirrors ``insights._digest``'s treatment: an image's base64 is never part of the text a
    truncation lever reasons about, and two screenshots of the same page are never
    byte-identical anyway.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, default=str)
    parts: List[str] = []
    for blk in content:
        if isinstance(blk, str):
            parts.append(blk)
        elif isinstance(blk, dict):
            if blk.get("type") == "image":
                continue
            t = blk.get("text")
            parts.append(t if isinstance(t, str) else json.dumps(blk, default=str))
    return "".join(parts)


def body_to_session(
    body: Mapping[str, Any],
    *,
    session_id: str = "",
    agent: str = "claude",
    usage: Optional[Usage] = None,
) -> Tuple[Session, Dict[Tuple[int, int], Anchor]]:
    """Adapt one ``/v1/messages`` request body into the model levers read.

    Sibling of ``types.from_corpus_session``, and deliberately not a replacement for it: that
    one adapts a finished transcript (hashes and sizes, every turn's usage known), this one
    adapts a request in flight (real bytes, only the current turn's usage knowable). Levers
    cannot tell the difference, which is the property that lets one lever serve both paths.

    A "turn" here is an assistant message that made tool calls, paired with the results that
    came back in the following user message. Historical turns carry an empty :class:`Usage`
    — the body does not record what they were billed — so the live path prices with
    :func:`price_delta` rather than the transcript ledger, which needs those numbers.

    ``sig``/``target``/``digest`` are computed by ``insights``' own helpers rather than
    reimplemented. Two hashing conventions for the same quantity is precisely how a lever
    ends up scoring one thing on transcripts and a different thing live.
    """
    # Lazy: `insights` is a 2,700-line dashboard module and this is reached from the gateway.
    # One-time cost, off the hot path, and it buys a single definition of these hashes.
    from ace.sidecar.insights import _digest, _measure, _sig, _target

    messages = body.get("messages")
    messages = messages if isinstance(messages, list) else []

    # tool_use id -> where its result landed, so a call can be joined to its result across
    # the message boundary. Anthropic pairs them by id, which is reliable here (unlike in
    # some transcript formats, where position is the only anchor available).
    results: Dict[str, Tuple[int, int, Any]] = {}
    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        for bi, blk in enumerate(_blocks(msg.get("content"))):
            if isinstance(blk, dict) and blk.get("type") == TOOL_RESULT:
                tid = blk.get("tool_use_id")
                if isinstance(tid, str):
                    results[tid] = (mi, bi, blk.get("content"))

    # The newest result in the body is the one this turn just produced: it has not yet been
    # written to the provider's cache, so an edit to it is free of invalidation cost.
    tail_id = ""
    tail_pos = (-1, -1)
    for tid, (mi, bi, _) in results.items():
        if (mi, bi) > tail_pos:
            tail_pos, tail_id = (mi, bi), tid

    turns: List[Turn] = []
    anchors: Dict[Tuple[int, int], Anchor] = {}

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        calls: List[ToolCall] = []
        ti = len(turns)
        for blk in _blocks(msg.get("content")):
            if not (isinstance(blk, dict) and blk.get("type") == TOOL_USE):
                continue
            name = str(blk.get("name") or "?")
            tool_input = blk.get("input")
            tool_input = tool_input if isinstance(tool_input, dict) else {}
            tid = blk.get("tool_use_id") or blk.get("id")

            found = results.get(tid) if isinstance(tid, str) else None
            ci = len(calls)
            if found is None:
                # A call whose result is not in this body — the in-flight one, typically.
                # Recorded so positions stay stable, but with nothing to edit.
                calls.append(
                    ToolCall(name=name, sig=_sig(name, tool_input),
                             target=_target(tool_input), call_id=tid)
                )
                continue

            mi, bi, content = found
            anchors[(ti, ci)] = Anchor(mi, bi, is_tail=(tid == tail_id))
            calls.append(
                ToolCall(
                    name=name,
                    sig=_sig(name, tool_input),
                    target=_target(tool_input),
                    digest=_digest(content),
                    result_bytes=_measure(content),
                    call_id=tid,
                    # THE difference from the transcript path: real bytes, bound lazily so a
                    # lever that only reads sizes never materializes them.
                    content=ContentRef(lambda c=content: c),
                )
            )
        # Historical turns carry an empty Usage deliberately: the request body does not
        # record what they were billed, and fabricating a plausible number is exactly the
        # kind of input that would make the transcript ledger's arithmetic silently wrong.
        turns.append(
            Turn(index=ti, model=str(body.get("model") or ""), calls=tuple(calls))
        )

    # The one turn whose billing IS known is this one, and it belongs to the tail.
    if turns and usage is not None:
        last = turns[-1]
        turns[-1] = Turn(index=last.index, model=last.model, ts=last.ts,
                         stop_reason=last.stop_reason, usage=usage, calls=last.calls)

    return Session(id=session_id, agent=agent, turns=tuple(turns)), anchors


# ---------------------------------------------------------------------------------------
# Building the counterfactual
# ---------------------------------------------------------------------------------------


def _truncate_content(content: Any, keep_bytes: int) -> Any:
    """A tool result kept to its first ``keep_bytes`` of text, structure preserved.

    Truncation is applied to the *text*, block by block, so a result that is a list of parts
    stays a list of parts and an image inside it is dropped rather than sliced into
    corruption. A lever asking to keep 4 KB of a screenshot is asking for something
    meaningless; returning the blocks it can honour is better than returning bytes that no
    longer parse.
    """
    if keep_bytes <= 0:
        return _POINTER_TEXT
    if isinstance(content, str):
        return content if len(content) <= keep_bytes else content[:keep_bytes]
    if not isinstance(content, list):
        return content

    out: List[Any] = []
    budget = keep_bytes
    for blk in content:
        if budget <= 0:
            break
        if isinstance(blk, str):
            out.append(blk[:budget])
            budget -= min(len(blk), budget)
        elif isinstance(blk, dict) and blk.get("type") == "image":
            continue  # cannot be partially kept; keeping it whole would defeat the cap
        elif isinstance(blk, dict):
            t = blk.get("text")
            if isinstance(t, str):
                nb = dict(blk)
                nb["text"] = t[:budget]
                out.append(nb)
                budget -= min(len(t), budget)
            else:
                out.append(blk)
        else:
            out.append(blk)
    return out or _POINTER_TEXT


def apply_edits(
    body: Mapping[str, Any],
    edits: Sequence[Edit],
    anchors: Mapping[Tuple[int, int], Anchor],
) -> Tuple[Dict[str, Any], List[LiveEdit], int]:
    """The counterfactual body, plus what actually landed in it.

    Copy-on-write down the edited path only. A deep copy would duplicate a multi-megabyte
    agent prompt once per lever per turn, for the sake of changing a handful of strings.

    Returns ``(new_body, applied, skipped)``. An edit that addresses a call with no result in
    this body, or that is an ``expire``, changes no bytes and is reported rather than dropped
    — a lever whose every edit was skipped must not look like a lever that found nothing.
    """
    out = dict(body)
    messages = list(body.get("messages") or [])
    touched_msgs: Dict[int, Dict[str, Any]] = {}
    applied: List[LiveEdit] = []
    skipped = 0

    for edit in edits:
        anchor = anchors.get((edit.turn_index, edit.call_index))
        if anchor is None:
            skipped += 1
            applied.append(LiveEdit(
                lever="", kind=edit.kind, reason=edit.reason,
                turn_index=edit.turn_index, call_index=edit.call_index,
                applied=False, note="no tool result at this position in the request body",
            ))
            continue
        if edit.kind == "expire":
            # Changes residency, not bytes. Volume levers and accounting levers do not share
            # a number here for the same reason the ledger keeps them apart.
            skipped += 1
            applied.append(LiveEdit(
                lever="", kind=edit.kind, reason=edit.reason,
                turn_index=edit.turn_index, call_index=edit.call_index,
                prefix_safe=anchor.is_tail, applied=False,
                note="expire changes residency, not prompt bytes — not priced on this path",
            ))
            continue

        msg = touched_msgs.get(anchor.msg_index)
        if msg is None:
            src = messages[anchor.msg_index]
            msg = dict(src)
            msg["content"] = list(_blocks(src.get("content")))
            touched_msgs[anchor.msg_index] = msg
            messages[anchor.msg_index] = msg

        blocks = msg["content"]
        if not (0 <= anchor.block_index < len(blocks)):
            skipped += 1
            continue
        blk = blocks[anchor.block_index]
        if not isinstance(blk, dict):
            skipped += 1
            continue

        nb = dict(blk)
        if edit.kind == "truncate":
            if edit.keep_bytes is None:
                skipped += 1
                continue
            nb["content"] = _truncate_content(blk.get("content"), edit.keep_bytes)
        elif edit.kind == "drop":
            nb["content"] = _POINTER_TEXT
        elif edit.kind == "replace":
            nb["content"] = edit.replacement if edit.replacement is not None else _POINTER_TEXT
        else:
            skipped += 1
            continue

        blocks[anchor.block_index] = nb
        applied.append(LiveEdit(
            lever="", kind=edit.kind, reason=edit.reason,
            turn_index=edit.turn_index, call_index=edit.call_index,
            prefix_safe=anchor.is_tail, applied=True,
        ))

    out["messages"] = messages
    return out, applied, skipped


# ---------------------------------------------------------------------------------------
# Pricing one live turn
# ---------------------------------------------------------------------------------------


def price_delta(removed: int, usage: Usage, rates) -> Tuple[float, int, int, int]:
    """Value ``removed`` tokens against the turn's own billed buckets. Newest first.

    Returns ``(usd, from_cache_write, from_input, from_cache_read)``.

    The allocation is the whole argument, so it is worth stating plainly. A tool result that
    a lever trims sits at the **end** of the prompt, and the end of an agent prompt is the
    part that was not served from cache: it is either fresh input or the content being
    written to the cache for the next turn. The cached prefix in front of it is older
    material the edit never touches. So removed tokens are drawn from
    ``cache_write -> input -> cache_read``, in that order, and each bucket is valued at the
    rate the provider actually charged for it.

    This matters by an order of magnitude and in the direction that flatters the product,
    which is why it is derived rather than assumed. Valuing everything at the cache-read rate
    (~0.1x) understates a tail truncation roughly tenfold; valuing everything at the write
    rate (1.25x) overstates a history rewrite by about the same. Both numbers are wrong. The
    turn's own usage split is the only thing here that is ground truth, and it is free.

    Falls back to the cache-read rate — the conservative end — once the newer buckets are
    exhausted, which is what happens when an edit really does reach into cached history.
    """
    if removed <= 0 or rates is None:
        return 0.0, 0, 0, 0

    left = removed
    from_write = min(left, max(0, usage.cache_write_tokens))
    left -= from_write
    from_input = min(left, max(0, usage.input_tokens))
    left -= from_input
    from_read = max(0, left)

    ttl = "5m"
    if usage.cache_write_by_ttl:
        ttl = max(usage.cache_write_by_ttl.items(), key=lambda kv: kv[1])[0]

    usd = (
        from_write / _MTOK * rates.cache_write_per_mtok(ttl)
        + from_input / _MTOK * rates.input_per_mtok
        + from_read / _MTOK * rates.cache_read_per_mtok
    )
    return usd, from_write, from_input, from_read


# ---------------------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------------------


class ShadowRunner:
    """Runs enabled levers against live turns and hands the results to a sink.

    Holds the discovered levers and the counter for the life of the process. Discovery walks
    installed distribution metadata, which cannot change under a running process, and the
    counter carries the one fact worth remembering across turns — whether its credential is
    accepted at all.

    Cheap and total when nothing is installed. That is the ordinary state for the open-source
    sidecar, and it must cost a proxied turn nothing measurable: :meth:`enabled` is one cached
    entry-point lookup and a dict comparison.
    """

    def __init__(
        self,
        *,
        config: Optional[Mapping[str, Any]] = None,
        sink=None,
        counter=None,
        agent: str = "claude",
    ) -> None:
        self._config = config
        self._sink = sink
        self._counter = counter
        self._agent = agent
        self._levers: Optional[Tuple[RegisteredLever, ...]] = None
        self._modes: Optional[Dict[str, str]] = None

    # -- wiring ------------------------------------------------------------------------

    def _discovered(self) -> Tuple[RegisteredLever, ...]:
        if self._levers is None:
            try:
                self._levers = discover()
            except Exception:  # pragma: no cover - discovery isolates its own failures
                log.debug("[levers] discovery failed", exc_info=True)
                self._levers = ()
        return self._levers

    def modes(self) -> Dict[str, str]:
        if self._modes is None:
            self._modes = resolve_modes(self._discovered(), config=self._config)
        return self._modes

    def active(self) -> List[RegisteredLever]:
        modes = self.modes()
        return [r for r in self._discovered() if modes.get(r.id, MODE_OFF) != MODE_OFF]

    @property
    def enabled(self) -> bool:
        """Whether any lever would run. The early-out every proxied turn hits first."""
        return bool(self._discovered()) and bool(self.active())

    def set_counter(self, counter) -> None:
        """Adopt a counter built from the in-flight credential, once.

        Kept for the life of the process rather than rebuilt per turn: a rebuilt counter
        would forget that the credential had already been refused and re-ask the counting
        endpoint on every single turn.
        """
        if self._counter is None and counter is not None:
            self._counter = counter

    @property
    def counter(self):
        return self._counter

    # -- the measurement ---------------------------------------------------------------

    def observe(
        self,
        body: Mapping[str, Any],
        usage: Usage,
        *,
        model: str = "",
        request_id: str = "",
        session_id: Optional[str] = None,
        rates=None,
    ) -> List[TurnMeasurement]:
        """Measure every enabled lever against one completed turn. Blocking; call off-thread.

        Never raises. This runs after a response has already been served, and there is no
        failure here worth converting into a developer-visible error.
        """
        out: List[TurnMeasurement] = []
        levers = self.active()
        if not levers or self._counter is None:
            return out

        t0 = time.monotonic()
        try:
            session, anchors = body_to_session(
                body, session_id=session_id or "", agent=self._agent, usage=usage
            )
        except Exception:
            log.debug("[levers] could not adapt request body", exc_info=True)
            return out

        if rates is None:
            try:
                from ace.gateway.pricing import rates_for

                rates = rates_for(model or str(body.get("model") or ""))
            except Exception:
                rates = None

        # One baseline count, shared by every lever. Counting it per lever would multiply the
        # network cost by the number installed for an answer that cannot differ.
        try:
            baseline = self._counter.count_body(body)
        except Exception as exc:
            log.debug("[levers] baseline count failed: %s", exc)
            return out

        modes = self.modes()
        for reg in levers:
            m = self._measure_one(
                reg, session, anchors, body, usage, baseline, modes.get(reg.id, MODE_OFF),
                model=model or str(body.get("model") or ""),
                request_id=request_id, session_id=session_id, rates=rates,
            )
            if m is not None:
                out.append(m)

        if out:
            log.debug(
                "[levers] measured %d lever(s) in %.0fms",
                len(out), (time.monotonic() - t0) * 1000.0,
            )
        return out

    def _measure_one(
        self,
        reg: RegisteredLever,
        session: Session,
        anchors: Mapping[Tuple[int, int], Anchor],
        body: Mapping[str, Any],
        usage: Usage,
        baseline: int,
        mode: str,
        *,
        model: str,
        request_id: str,
        session_id: Optional[str],
        rates,
    ) -> Optional[TurnMeasurement]:
        t0 = time.monotonic()
        ctx = LeverContext(
            count_tokens=self._counter,
            mode=mode,
            now=time.time(),
            settings=load_settings(reg.id, config=self._config),
        )
        proposal = propose_safely(reg, session, ctx)
        if proposal is None:
            return None

        base_row = dict(
            lever=reg.id, mode=mode, model=model, request_id=request_id,
            session_id=session_id, ts=time.time(),
            baseline_tokens=baseline, reported_prompt_tokens=usage.prompt_tokens,
            diagnostics=dict(proposal.diagnostics),
        )

        if not proposal.edits:
            # A real and useful result, not a failure — a loop guardrail's entire output is
            # its diagnostics. Recorded so the rail can show the lever ran and found nothing.
            return TurnMeasurement(
                **base_row, counterfactual_tokens=baseline,
                note="lever proposed no edits",
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

        try:
            new_body, applied, skipped = apply_edits(body, proposal.edits, anchors)
        except Exception:
            log.debug("[levers] %r: counterfactual body failed", reg.id, exc_info=True)
            return None

        edits = tuple(
            LiveEdit(lever=reg.id, kind=e.kind, reason=e.reason, turn_index=e.turn_index,
                     call_index=e.call_index, prefix_safe=e.prefix_safe,
                     applied=e.applied, note=e.note)
            for e in applied
        )
        if not any(e.applied for e in edits):
            return TurnMeasurement(
                **base_row, counterfactual_tokens=baseline, edits=edits,
                note="lever proposed edits, none of which changed prompt bytes",
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

        try:
            counterfactual = self._counter.count_body(new_body)
        except Exception as exc:
            return TurnMeasurement(
                **base_row, counterfactual_tokens=0, edits=edits, priced=False,
                note=f"counterfactual could not be counted: {exc}",
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
            )

        removed = max(0, baseline - counterfactual)
        usd, w, i, r = price_delta(removed, usage, rates)
        note = "" if rates is not None else "model has no catalog entry — unpriced, not free"
        if any(e.applied and not e.prefix_safe for e in edits):
            # The invalidation cost lands on the NEXT turn's cache write, which has not
            # happened yet. Saying so is the difference between a net figure and a gross one
            # wearing a net figure's label.
            note = (note + "; " if note else "") + (
                "touches already-cached history — the cache-write penalty falls on the next "
                "turn and is not netted here"
            )

        return TurnMeasurement(
            **base_row,
            counterfactual_tokens=counterfactual,
            removed_tokens=removed,
            from_cache_write=w, from_input=i, from_cache_read=r,
            usd=usd, priced=rates is not None,
            edits=edits, note=note,
            elapsed_ms=(time.monotonic() - t0) * 1000.0,
        )

    # -- the async entry point the proxy uses ------------------------------------------

    async def observe_async(self, *args, **kwargs) -> List[TurnMeasurement]:
        """:meth:`observe` on a worker thread, results handed to the sink.

        The counter is a synchronous HTTP client; awaiting it directly would block the event
        loop that is serving every other turn. Runs detached, after the response.
        """
        import asyncio

        try:
            rows = await asyncio.to_thread(self.observe, *args, **kwargs)
        except Exception:  # pragma: no cover - a shadow run never surfaces
            log.debug("[levers] shadow run failed", exc_info=True)
            return []
        if rows and self._sink is not None:
            try:
                self._sink(rows)
            except Exception:
                log.debug("[levers] shadow sink failed", exc_info=True)
        return rows
