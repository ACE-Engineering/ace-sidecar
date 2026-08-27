"""ace.sidecar.levers.protocol — what a lever is, and what it is forbidden to do.

A lever proposes; it never prices
---------------------------------
:meth:`Lever.propose` returns :class:`Edit` objects and nothing else. It does not return
dollars, tokens saved, or a percentage. Pricing happens once, in the ledger, which owns the
tokenizer and the rate catalog and is the only thing that knows the provider's cache-write
premium.

That split is not tidiness, it is the credibility of the number. A lever that reports its
own saving is a lever that can overstate it, and the three ways this arithmetic has already
gone wrong in this codebase were all self-reporting:

* compaction savings denominated in whitespace words and priced per BPE token, which
  undersold the leg by the word->BPE ratio (see ``ace.gateway.tokenizer``);
* a de-dup lever keyed on file path rather than full tool input, measuring $36.88 where the
  provable version measures $0.33;
* the same avoided call counted twice — once as avoided, once as a counterfactual.

With pricing centralized, a lever cannot commit any of them. It also means a lever needs no
knowledge of which provider it is running against, which is what lets one implementation
serve Claude Code, Antigravity and Codex.

Counting must be exact, so the counter is injected
--------------------------------------------------
The baseline side of every counterfactual is ground truth: the provider's own token counts,
read off the transcript. The proposed side is a prompt that was never sent, so its tokens
have to be produced — and an approximation there turns a measured claim into an estimate.

:class:`TokenCounter` is therefore a seam with an exact implementation per model family:
Anthropic's ``POST /v1/messages/count_tokens`` for Claude (tiktoken is a *proxy* for
non-OpenAI models, not a truth), tiktoken for OpenAI models where it is that provider's own
BPE, and the provider's counting endpoint for Gemini. A lever calls ``ctx.count_tokens``
and stays out of that decision.

Modes
-----
``off`` / ``shadow`` / ``on`` mirror the cloud gateway's vocabulary on purpose, so the two
products' telemetry reads as one thing. ``shadow`` is the default and should stay the
default: this process sits in front of a developer's real coding session, and a lever that
silently rewrites a prompt owns every unexplained agent failure that follows. Shadow costs
nothing and proves the same number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    ClassVar,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from ace.sidecar.levers.types import Session

__all__ = [
    "MODE_OFF",
    "MODE_SHADOW",
    "MODE_ON",
    "MODES",
    "RISK_NONE",
    "RISK_LOW",
    "RISK_MEDIUM",
    "RISK_HIGH",
    "EditKind",
    "Edit",
    "Proposal",
    "TokenCounter",
    "LeverContext",
    "Lever",
]

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

# Same vocabulary as ``strategies.LEVER_RISK``, so a lever's declared risk and the rail's
# scored risk are comparable without a mapping table.
RISK_NONE, RISK_LOW, RISK_MEDIUM, RISK_HIGH = "NONE", "LOW", "MEDIUM", "HIGH"

EditKind = Literal["truncate", "drop", "replace", "expire"]


@dataclass(frozen=True, slots=True)
class Edit:
    """One proposed change to one tool result.

    Addressed positionally by ``(turn_index, call_index)`` rather than by tool-call id:
    ids exist in Claude Code transcripts and not reliably elsewhere, and position is
    unambiguous in every scanner's output. ``sig`` rides along for debugging and for the
    ledger's audit line, never as the key.

    The four kinds split along a line that decides what an edit costs:

    ``truncate`` / ``drop`` / ``replace`` change bytes. Applied at the tail — to a result
    that has not yet entered the cached prefix — they are free. Applied to history they
    invalidate the cached prefix from that point on and the next turn pays a full cache
    write, which is why the ledger nets that penalty before reporting anything.

    ``expire`` changes nothing about the bytes. It asserts the result stops being worth
    keeping resident after ``live_until``, which is an accounting claim about residency, not
    a smaller prompt. Volume levers help against a token cap; ``expire`` helps only against a
    bill. Conflating the two is the easiest way to overstate this product, so the ledger
    reports them separately and never sums them.
    """

    turn_index: int
    call_index: int
    kind: EditKind
    reason: str
    sig: str = ""
    # truncate: bytes retained from the head (and, if the lever keeps a tail, from the end).
    keep_bytes: Optional[int] = None
    # replace: the substituted text. Only ever populated in actuation mode, where the lever
    # was handed the original bytes to begin with.
    replacement: Optional[str] = None
    # expire: the last turn index at which this result is still worth holding in context.
    live_until: Optional[int] = None


@dataclass(frozen=True, slots=True)
class Proposal:
    """What one lever would do to one session. No savings figure — see the module docstring."""

    lever: str
    edits: Tuple[Edit, ...] = ()
    # Free-form counters a lever wants surfaced for debugging or for its dashboard row
    # ("loops_detected": 3). Never priced, never summed into a saving.
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    # No ``__bool__``. An edit-free proposal is a legitimate and important result: a loop
    # guardrail's whole output is "I detected three runaway tool cycles" with nothing to
    # rewrite, and the most valuable thing a truncation lever can report on a clean session
    # is that it found nothing to do. Defining truthiness as "has edits" makes ``if
    # proposal:`` quietly discard both. Callers test ``proposal.edits`` when they mean edits
    # and ``proposal is None`` when they mean the lever declined or failed.


class TokenCounter(Protocol):
    """Exact token count for ``text`` under ``model``. Must not approximate.

    Implementations may be slow and may do I/O — Anthropic's counting endpoint is a network
    call. Levers should call it on whole segments rather than per word, and the runtime is
    free to batch or sample across turns; that policy lives in the runtime, not here.
    """

    def __call__(self, text: str, *, model: str) -> int: ...


@dataclass(frozen=True, slots=True)
class LeverContext:
    """Everything a lever is allowed to depend on.

    Deliberately small. It carries no rate catalog (levers do not price), no database, no
    HTTP client and no agent identity beyond what ``Session.agent`` already says. A lever
    needing something absent here is a lever reaching past its contract — extend this
    dataclass rather than importing around it, so the dependency stays visible at the seam.
    """

    count_tokens: TokenCounter
    mode: str = MODE_SHADOW
    now: float = 0.0
    # Per-lever configuration from ``~/.ace/config.json``, already narrowed to this lever's
    # own key. A lever must tolerate an empty mapping: the common case is a user who enabled
    # it and tuned nothing.
    settings: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Lever(Protocol):
    """One optimization, scored or applied against the normalized session model.

    Implementations live outside this repository. This protocol and
    :mod:`ace.sidecar.levers.types` are the entire public surface they compile against, and
    both are versioned as a contract: adding an optional field is fine, changing the meaning
    of one is not.

    ``requires_content`` is the honest declaration of what a lever needs. A lever reading only
    ``sig``/``digest``/``result_bytes`` scores from transcripts alone and therefore works for
    every agent the sidecar can read, with no proxy and no hooks. A lever that must rewrite
    text needs the bytes in hand, so the registry offers it only where an actuator supplied
    them — today that is Claude Code's proxy and hook paths. Declaring ``False`` and then
    calling ``ContentRef.resolve`` raises rather than silently degrading.
    """

    id: ClassVar[str]
    label: ClassVar[str]
    risk: ClassVar[str]
    requires_content: ClassVar[bool]

    def propose(self, session: Session, ctx: LeverContext) -> Proposal:
        """Edits this lever would make to ``session``. Must not mutate ``session``.

        Called on the measurement path for every session in a developer's history, so it is
        expected to be cheap in the ``requires_content = False`` case and to raise nothing:
        a lever that throws on one malformed session must not take the dashboard down with
        it. The registry isolates failures, but a lever should not rely on that.
        """
        ...
