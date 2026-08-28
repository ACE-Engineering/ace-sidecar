"""ace.sidecar.levers.ledger — the one place a lever's proposal becomes money.

The rule
--------
**No estimates.** Both sides of every figure here are either measured or absent.

The baseline side is ground truth: the provider's own per-turn token counts, read off the
transcript. The proposed side is a prompt that was never sent, so its tokens have to be
produced — and the only honest way to produce them is to count the actual text with the
provider's own counter (``ctx.count_tokens``: Anthropic's ``/v1/messages/count_tokens`` for
Claude, tiktoken for OpenAI models where it is that vendor's own BPE, Gemini's counting
endpoint for Gemini).

Where the text is not in hand, this module returns :data:`FIDELITY_UNMEASURABLE` and prices
nothing. It does **not** fall back to ``result_bytes / 4``. A ratio-derived saving is an
estimate wearing a dollar sign, and one number here that a developer can contradict against
their own invoice discredits the measured half of the dashboard along with it. Ranking
levers without content is a real and useful job — it is what ``strategies.py`` does, in
byte-turns, explicitly labelled a simulation — but it is not this module's job.

The three arithmetic traps this module exists to avoid
-----------------------------------------------------
**1. Ignoring the cache-write penalty.** Every lever removes tokens from a prompt that the
provider was mostly serving from cache at ~0.1x. Removing them saves that cheap rate, not
the fresh-input rate. And an edit that changes content the cache has *already* seen
invalidates the prefix from that point, so the next turn re-writes it at a premium (1.25x at
Anthropic's 5-minute TTL, 2x at one hour). A lever that reports gross saving and omits the
penalty can report a win on an edit that cost money. :class:`EditCost` carries both legs and
``net_usd`` is the only figure meant to be quoted.

**2. Summing levers.** Two levers can target the same bytes; scored alone their figures
overlap. :class:`LedgerReport` therefore ranks and never totals — the same discipline
``strategies.STANDALONE`` already documents.

**3. Calling an unpriced model free.** A model with no catalog entry yields
``priced=False`` and zeroes, which must render as "unpriced", never as $0.00 of spend. A
silent zero looks like a cost win.

What is out of scope
--------------------
Only *volume* is priced here: edits that put fewer tokens in a later prompt. Accounting
levers — buying a longer cache TTL, normalising a mutating field so a prefix stops
breaking — convert price without sending less, produce no :class:`Edit`, and are worth
exactly nothing against a token cap. Conflating the two is the easiest way to overstate this
product, so they do not share a number with it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ace.gateway.pricing import Rates, rates_for
from ace.sidecar.levers.protocol import Edit, LeverContext, Proposal
from ace.sidecar.levers.types import Session, ToolCall, Turn

__all__ = [
    "FIDELITY_MEASURED",
    "FIDELITY_UNMEASURABLE",
    "FIDELITY_UNPRICED",
    "EditCost",
    "LedgerEntry",
    "LedgerReport",
    "price_proposal",
    "price_all",
]

# Counted exactly, with a catalog rate behind every dollar. The only tier that may be quoted.
FIDELITY_MEASURED = "measured"
# The text was not in hand, so the token delta could not be counted. No dollars, by design.
FIDELITY_UNMEASURABLE = "unmeasurable"
# Counted exactly, but the model has no catalog entry. Tokens are real; dollars are absent.
FIDELITY_UNPRICED = "unpriced"

_MTOK = 1_000_000.0

# What a dropped result is replaced by in context — a short pointer, not nothing. Matches
# ``strategies.POINTER_BYTES``; at ~4 bytes/token it is a rounding error, but counting it as
# zero would claim a saving the applied lever does not actually deliver.
_POINTER_TOKENS = 30


@dataclass(frozen=True, slots=True)
class EditCost:
    """One edit, priced against the turns that actually carried its bytes.

    ``prefix_safe`` is the distinction that decides whether this edit is nearly free or has
    to earn back a penalty first. A tool result created at turn *i* first enters the prompt
    at turn *i+1* and is written to the cache there for the first time. Editing it before
    that write costs nothing — the same single write happens, just smaller. Editing content
    the cache has already stored invalidates the prefix from that point on, and the next turn
    re-writes the remainder at the write premium.

    That is why tail-acting levers (truncate a fresh dump, strip a screenshot on the way in)
    can ship far earlier than history-rewriting ones: they are prefix-safe by construction
    and carry ``cache_write_penalty_usd == 0``.
    """

    lever: str
    turn_index: int
    call_index: int
    kind: str
    reason: str
    model: str

    removed_tokens: int = 0
    # First turn whose prompt is smaller because of this edit.
    apply_at: int = 0
    # How many turns carried the removed tokens and now do not.
    turns_carried: int = 0

    prefix_safe: bool = True
    invalidated_tokens: int = 0
    cache_write_ttl: str = "5m"

    gross_saving_usd: float = 0.0
    cache_write_penalty_usd: float = 0.0
    priced: bool = True

    @property
    def net_usd(self) -> float:
        """The only figure meant to be quoted. May be negative — that is the point."""
        return self.gross_saving_usd - self.cache_write_penalty_usd

    @property
    def per_turn_saving_usd(self) -> float:
        return self.gross_saving_usd / self.turns_carried if self.turns_carried else 0.0

    @property
    def break_even_turn(self) -> Optional[int]:
        """The turn at which this edit stops costing money and starts saving it.

        ``None`` when there is no penalty to earn back (a prefix-safe edit is in profit
        immediately) or when the edit saves nothing per turn. This is the number worth
        putting in front of a developer: "compacting here costs $0.04 now and saves
        $0.011/turn — you break even at turn 7, and this session ran 60."
        """
        per_turn = self.per_turn_saving_usd
        if self.cache_write_penalty_usd <= 0.0 or per_turn <= 0.0:
            return None
        return self.apply_at + math.ceil(self.cache_write_penalty_usd / per_turn)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One lever, on one session, priced.

    ``fidelity`` qualifies every number above it and must be carried into the UI. A
    :data:`FIDELITY_UNMEASURABLE` entry has real ``diagnostics`` and no dollars; rendering it
    beside a measured entry without the label is how an estimate ends up quoted as a
    measurement.
    """

    lever: str
    session_id: str
    agent: str
    fidelity: str = FIDELITY_MEASURED
    edits: Tuple[EditCost, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    # Why nothing was priced, when fidelity is not MEASURED.
    note: str = ""
    # Provenance for every rate used, so a figure can cite the price list that produced it.
    rate_sources: Tuple[Tuple[str, str, str], ...] = ()  # (model, source, as_of)

    @property
    def removed_tokens(self) -> int:
        return sum(e.removed_tokens for e in self.edits)

    @property
    def gross_saving_usd(self) -> float:
        return sum(e.gross_saving_usd for e in self.edits)

    @property
    def cache_write_penalty_usd(self) -> float:
        return sum(e.cache_write_penalty_usd for e in self.edits)

    @property
    def net_usd(self) -> float:
        return self.gross_saving_usd - self.cache_write_penalty_usd

    @property
    def priced(self) -> bool:
        return self.fidelity == FIDELITY_MEASURED and bool(self.edits)


@dataclass(frozen=True, slots=True)
class LedgerReport:
    """Every lever's entries, ranked. Deliberately without a total.

    There is no ``total_usd`` here and there must not be one. Levers are scored alone, so two
    of them can claim the same bytes and their figures overlap; adding them up produces a
    number that is larger than anything the levers could jointly deliver. Ranking answers the
    question that is actually being asked — which lever is worth building or enabling first.
    """

    entries: Tuple[LedgerEntry, ...] = ()

    def by_lever(self) -> Dict[str, float]:
        """``{lever_id: net usd}`` summed across sessions — safe, because it is one lever."""
        out: Dict[str, float] = {}
        for e in self.entries:
            if e.priced:
                out[e.lever] = out.get(e.lever, 0.0) + e.net_usd
        return out

    def ranked(self) -> List[Tuple[str, float]]:
        """Levers, best first. The ordering the rail exists to show."""
        return sorted(self.by_lever().items(), key=lambda kv: -kv[1])

    def unmeasured(self) -> Tuple[LedgerEntry, ...]:
        """Entries that produced no dollars, with the reason. Surface these, do not drop them."""
        return tuple(e for e in self.entries if e.fidelity != FIDELITY_MEASURED)


def _call_at(session: Session, edit: Edit) -> Optional[ToolCall]:
    if not (0 <= edit.turn_index < len(session.turns)):
        return None
    calls = session.turns[edit.turn_index].calls
    if not (0 <= edit.call_index < len(calls)):
        return None
    return calls[edit.call_index]


def _count(text: Any, model: str, ctx: LeverContext) -> Optional[int]:
    """Exact token count, or ``None`` when the text is not countable text.

    A non-string body (a list of content parts) is serialized the way the provider would
    render it only if the lever already reduced it to text. Anything else returns ``None``
    and the edit goes unmeasured — guessing at a multimodal part's token cost is precisely
    the estimate this module refuses to make.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return None
    try:
        n = ctx.count_tokens(text, model=model)
    except Exception:
        return None
    return int(n) if n is not None and n >= 0 else None


def _removed_tokens(
    edit: Edit, call: ToolCall, model: str, ctx: LeverContext
) -> Optional[int]:
    """Tokens this edit takes out of every later prompt. Exact, or ``None``.

    Requires the original bytes for every kind except ``expire``, which removes the whole
    result from later prompts and so still needs the result's own token count.
    """
    if call.content is None or not call.content.available:
        return None
    try:
        body = call.content.resolve()
    except Exception:
        return None

    original = _count(body, model, ctx)
    if original is None:
        return None

    if edit.kind == "drop":
        return max(0, original - _POINTER_TOKENS)
    if edit.kind == "expire":
        # Nothing is rewritten; the result simply stops being resident after ``live_until``.
        return original
    if edit.kind == "replace":
        kept = _count(edit.replacement or "", model, ctx)
        return None if kept is None else max(0, original - kept)
    if edit.kind == "truncate":
        if edit.keep_bytes is None:
            return None
        if not isinstance(body, str):
            return None
        kept = _count(body[: edit.keep_bytes], model, ctx)
        return None if kept is None else max(0, original - kept)
    return None


def _apply_at(edit: Edit) -> int:
    """First turn whose prompt this edit changes.

    A tool result created at turn *i* is not in turn *i*'s own prompt — it lands in *i+1*'s.
    ``expire`` instead takes effect the turn after the result stops being worth keeping.
    """
    if edit.kind == "expire":
        return (edit.live_until if edit.live_until is not None else edit.turn_index) + 1
    return edit.turn_index + 1


def _invalidated_tokens(session: Session, edit_turn: int, apply_at: int) -> int:
    """Cached tokens the prefix loses when this edit lands after the content was cached.

    Derived from two ground-truth numbers and nothing else: the prompt size at the turn the
    content was created and at the turn the edit takes effect. What sits between them is what
    the cache holds beyond the edit point and must be re-written.

    Capped by the tokens actually served from cache at ``apply_at`` — a prefix cannot lose
    more than it held, and reporting a penalty larger than the cache read would overstate the
    cost of every history-rewriting lever.

    When the prompt SHRANK between the two turns the subtraction is meaningless: something
    else already rewrote the history (a compaction, a context edit), so the edited content's
    position can no longer be derived from sizes. The answer there is the whole cached
    prefix, not zero. Both readings are wrong, and they are wrong in opposite directions —
    assuming zero understates the penalty, which overstates the saving, which is the one
    error this module exists to prevent.
    """
    turns = session.turns
    if not (0 <= edit_turn < len(turns)) or not (0 <= apply_at < len(turns)):
        return 0
    cached = turns[apply_at].usage.cache_read_tokens
    grew = turns[apply_at].usage.prompt_tokens - turns[edit_turn].usage.prompt_tokens
    if grew < 0:
        return max(0, int(cached))
    return max(0, min(int(grew), int(cached)))


def _ttl_for(turn: Turn) -> str:
    """The TTL this turn's cache writes were actually billed at.

    Read from the turn rather than assumed. ``strategies.TTL_SECONDS`` hard-codes one hour
    while the default on a Claude Code session is the 5-minute tier, and the two carry
    different write premiums (2x vs 1.25x) — an assumed TTL prices the penalty wrong in
    whichever direction the assumption is off.
    """
    by_ttl = turn.usage.cache_write_by_ttl
    if by_ttl:
        return max(by_ttl.items(), key=lambda kv: kv[1])[0]
    return "5m"


def price_proposal(
    session: Session,
    proposal: Proposal,
    ctx: LeverContext,
    *,
    rates_lookup: Callable[[str], Optional[Rates]] = rates_for,
) -> LedgerEntry:
    """Price one lever's proposal against one session. The core of this module.

    Returns an entry rather than raising: an unmeasurable proposal is an ordinary outcome
    (the measurement path has no content for any of them) and the caller needs the
    diagnostics either way.
    """
    n = session.n_turns
    costs: List[EditCost] = []
    sources: Dict[str, Tuple[str, str]] = {}
    unmeasured = 0

    for edit in proposal.edits:
        call = _call_at(session, edit)
        if call is None:
            unmeasured += 1
            continue

        apply_at = _apply_at(edit)
        turns_carried = n - apply_at
        if turns_carried <= 0:
            # The result never reached another prompt, so removing it saves nothing. Recorded
            # as a zero rather than dropped: "this lever fired on the last turn and therefore
            # saved nothing" is a real and useful thing for a rail row to say.
            turns_carried = 0

        model = session.turns[min(apply_at, n - 1)].model if n else ""
        removed = _removed_tokens(edit, call, model, ctx)
        if removed is None:
            unmeasured += 1
            continue

        rates = rates_lookup(model)
        prefix_safe = apply_at <= edit.turn_index + 1
        invalidated = (
            0 if prefix_safe else _invalidated_tokens(session, edit.turn_index, apply_at)
        )
        ttl = _ttl_for(session.turns[min(apply_at, n - 1)]) if n else "5m"

        if rates is None:
            costs.append(
                EditCost(
                    lever=proposal.lever, turn_index=edit.turn_index,
                    call_index=edit.call_index, kind=edit.kind, reason=edit.reason,
                    model=model, removed_tokens=removed, apply_at=apply_at,
                    turns_carried=turns_carried, prefix_safe=prefix_safe,
                    invalidated_tokens=invalidated, cache_write_ttl=ttl, priced=False,
                )
            )
            continue

        sources[model] = (rates.source, rates.as_of)
        # Priced at the CACHE-READ rate, not the fresh-input rate. These tokens were resident
        # in a cached prefix and re-read each turn at ~0.1x; valuing them at the input rate
        # would inflate every lever tenfold. It is also the conservative direction.
        gross = (removed / _MTOK) * rates.cache_read_per_mtok * turns_carried
        # The penalty is the DELTA between writing those tokens and reading them, not the
        # full write price: they were going to be paid for either way.
        penalty = (invalidated / _MTOK) * (
            rates.cache_write_per_mtok(ttl) - rates.cache_read_per_mtok
        )
        costs.append(
            EditCost(
                lever=proposal.lever, turn_index=edit.turn_index,
                call_index=edit.call_index, kind=edit.kind, reason=edit.reason,
                model=model, removed_tokens=removed, apply_at=apply_at,
                turns_carried=turns_carried, prefix_safe=prefix_safe,
                invalidated_tokens=invalidated, cache_write_ttl=ttl,
                gross_saving_usd=gross, cache_write_penalty_usd=max(0.0, penalty),
                priced=True,
            )
        )

    if not costs:
        note = (
            "no tool-result content available; token delta cannot be counted exactly"
            if proposal.edits
            else "lever proposed no edits"
        )
        return LedgerEntry(
            lever=proposal.lever, session_id=session.id, agent=session.agent,
            fidelity=FIDELITY_UNMEASURABLE if proposal.edits else FIDELITY_MEASURED,
            diagnostics=dict(proposal.diagnostics), note=note,
        )

    fidelity = (
        FIDELITY_MEASURED if all(c.priced for c in costs) else FIDELITY_UNPRICED
    )
    diagnostics = dict(proposal.diagnostics)
    if unmeasured:
        diagnostics["edits_unmeasured"] = unmeasured
    return LedgerEntry(
        lever=proposal.lever, session_id=session.id, agent=session.agent,
        fidelity=fidelity, edits=tuple(costs), diagnostics=diagnostics,
        note="" if fidelity == FIDELITY_MEASURED else "model has no catalog entry — unpriced, not free",
        rate_sources=tuple((m, s, a) for m, (s, a) in sorted(sources.items())),
    )


def price_all(
    pairs: Sequence[Tuple[Session, Proposal]],
    ctx: LeverContext,
    *,
    rates_lookup: Callable[[str], Optional[Rates]] = rates_for,
) -> LedgerReport:
    """Price many ``(session, proposal)`` pairs into one ranked report."""
    return LedgerReport(
        entries=tuple(
            price_proposal(s, p, ctx, rates_lookup=rates_lookup) for s, p in pairs
        )
    )
