"""ace.sidecar.levers.types — the normalized session model every lever reads.

Why this layer exists
---------------------
``insights._scan``, ``_scan_antigravity`` and ``_scan_codex`` already emit one shape —
"corpus-shaped sessions" — from three completely different on-disk formats. That shape is
the only agent-agnostic thing in the codebase, and it is what makes one lever work for
Claude Code, Antigravity and Codex without knowing which produced the session.

So a lever is defined against **this** model and never against a provider's wire format. A
lever that took an Anthropic ``messages[]`` body would work for exactly one of the three
agents and would have to be rewritten for the fourth. Adding an agent is then a scanner,
not a lever change.

Hashes and sizes, not content
-----------------------------
The corpus is deliberately "numbers and hashes only": ``target`` and ``digest`` are
truncated SHA-256, ``result_bytes`` is a size. That is a privacy property worth keeping —
the dashboard reads a developer's whole transcript history and nothing about it needs the
text.

It is also sufficient for the entire measurement half. Every lever in ``strategies.py``
(read de-dup, supersede, age-out, truncate) decides purely on ``sig``/``digest``/
``result_bytes``, which is why they can be scored on transcripts alone. Only *actuation*
needs bytes — you cannot truncate text you do not have — so content arrives through the
optional :class:`ContentRef` seam, resolved lazily and only in the proxy/hook path where
the bytes are in hand anyway. ``ContentRef`` is ``None`` in measure-only mode, and a lever
that declares ``requires_content`` is simply not offered there.

Provider neutrality in the usage record
---------------------------------------
:class:`Usage` carries a total ``cache_write_tokens`` plus a ``by_ttl`` breakdown rather
than Anthropic's ``ephemeral_5m``/``ephemeral_1h`` field names. The cache-write premium is
a *provider* property — Anthropic charges 1.25x at the 5-minute TTL and 2x at one hour;
other providers price prefix reuse differently and some charge no write premium at all.
Pricing that difference is the ledger's job (see ``ace.gateway.pricing``); the lever must
never see it, or a lever tuned against one provider's cache economics will quietly give
wrong answers on another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

__all__ = [
    "ContentUnavailable",
    "ContentRef",
    "ToolCall",
    "Usage",
    "Turn",
    "Session",
    "from_corpus_session",
    "from_corpus_sessions",
]


class ContentUnavailable(RuntimeError):
    """Raised when a lever asks for bytes that this run does not have.

    Reaching this is a wiring bug, not a runtime condition: a lever declaring
    ``requires_content = True`` must never be handed a measure-only session. The registry
    filters on that flag, so this exception exists to make a filtering mistake loud rather
    than to be caught.
    """


@dataclass(frozen=True, slots=True)
class ContentRef:
    """A lazy handle to a tool result's actual bytes.

    Deliberately not the bytes themselves. A session can hold thousands of tool results and
    the measurement path wants none of them; materializing every result to run a lever that
    only reads sizes would turn a transcript scan into a memory problem for no gain.
    """

    _resolve: Optional[Callable[[], Any]] = None

    @property
    def available(self) -> bool:
        return self._resolve is not None

    def resolve(self) -> Any:
        """The result body, in whatever shape the agent recorded it (str or list-of-parts)."""
        if self._resolve is None:
            raise ContentUnavailable(
                "tool result content is not available in measure-only mode"
            )
        return self._resolve()


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation and the result it put into context.

    ``sig`` vs ``target`` is the distinction that decides whether a de-dup lever is worth
    anything. ``target`` hashes the primary path argument alone, so three disjoint slices
    of one file share it; ``sig`` hashes the *whole* input, so ``offset``/``limit``
    participate. Keying de-dup on ``target`` measured $36.88 of headroom on the reference
    corpus where keying on ``sig`` + ``digest`` measures $0.33 — the same lever, two orders
    of magnitude apart. Prefer ``sig``, and require ``digest`` equality before claiming
    bytes are redundant.
    """

    name: str
    sig: str
    target: Optional[str] = None
    digest: Optional[str] = None
    result_bytes: int = 0
    call_id: Optional[str] = None
    content: Optional[ContentRef] = None

    @property
    def has_content(self) -> bool:
        return self.content is not None and self.content.available


@dataclass(frozen=True, slots=True)
class Usage:
    """One turn's billed token counts, as the provider reported them.

    These are ground truth — read off the transcript, not derived — which is what lets a
    counterfactual be stated against a real bill instead of against an estimate. Anything a
    lever *proposes* has to be counted separately and exactly (see
    ``protocol.TokenCounter``); never infer the counterfactual by scaling these.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # TTL label -> tokens written at that TTL, e.g. {"5m": 12000, "1h": 0}. Empty when the
    # provider reports no breakdown; the total above still stands on its own.
    cache_write_by_ttl: Mapping[str, int] = field(default_factory=dict)

    @property
    def prompt_tokens(self) -> int:
        """Everything that was in the prompt this turn, cached or not.

        ``input_tokens`` already EXCLUDES the cached buckets on Anthropic, so this is a sum
        and not a max. Subtracting ``cache_read`` from ``input`` to "correct" it under-reports
        prompt volume — an easy bug with no visible symptom.
        """
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True, slots=True)
class Turn:
    """One API request. Not one transcript record.

    Claude Code writes one record per content block and repeats the whole ``usage`` object
    on each; the scanners join on message id before emitting. Counting per record instead
    inflates prompt volume 1.95x and output 2.34x. A lever receives turns already joined and
    must not try to re-derive them.
    """

    index: int
    model: str = ""
    ts: Optional[float] = None  # epoch seconds
    stop_reason: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    calls: Tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class Session:
    """One agent session, normalized. The unit a lever reasons over.

    ``agent`` is one of ``insights.AGENTS`` ("claude", "antigravity", "codex"). A lever may
    read it — some tool names are agent-specific — but must not require a particular value:
    the whole point of this model is that a lever written today keeps working when a fourth
    scanner lands.
    """

    id: str
    agent: str
    kind: str = "main"  # "main" | "subagent"
    parent: Optional[str] = None
    turns: Tuple[Turn, ...] = ()

    @property
    def n_turns(self) -> int:
        return len(self.turns)

    def iter_calls(self):
        """``(turn_index, call_index, ToolCall)`` over the whole session, in order."""
        for t in self.turns:
            for ci, call in enumerate(t.calls):
                yield t.index, ci, call


def _usage_from_corpus(t: Mapping[str, Any]) -> Usage:
    by_ttl: Dict[str, int] = {}
    for label, key in (("5m", "ephemeral_5m_input_tokens"), ("1h", "ephemeral_1h_input_tokens")):
        v = int(t.get(key) or 0)
        if v:
            by_ttl[label] = v
    return Usage(
        input_tokens=int(t.get("input_tokens") or 0),
        output_tokens=int(t.get("output_tokens") or 0),
        cache_read_tokens=int(t.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(t.get("cache_creation_input_tokens") or 0),
        cache_write_by_ttl=by_ttl,
    )


def from_corpus_session(
    raw: Mapping[str, Any],
    *,
    content_for: Optional[Callable[[int, int], Optional[Callable[[], Any]]]] = None,
) -> Session:
    """Adapt one ``insights`` session dict into the typed model.

    This function is the entire cost of supporting a new agent: write a scanner that emits
    the corpus shape and every lever works on it unchanged.

    ``content_for(turn_index, call_index)`` is the actuation hook. It returns a zero-arg
    callable producing that result's body, or ``None`` where the bytes are not held. Omit it
    entirely for the measurement path — which is every transcript-driven caller — and every
    ``ToolCall.content`` is ``None``.
    """
    turns = []
    for i, t in enumerate(raw.get("turns") or []):
        calls = []
        for ci, c in enumerate(t.get("calls") or []):
            resolver = content_for(i, ci) if content_for is not None else None
            calls.append(
                ToolCall(
                    name=str(c.get("name") or "?"),
                    sig=str(c.get("sig") or ""),
                    target=c.get("target"),
                    digest=c.get("digest"),
                    result_bytes=int(c.get("result_bytes") or 0),
                    call_id=c.get("id"),
                    content=ContentRef(resolver) if resolver is not None else None,
                )
            )
        turns.append(
            Turn(
                index=i,
                model=str(t.get("model") or ""),
                ts=_epoch(t.get("ts")),
                stop_reason=t.get("stop_reason"),
                usage=_usage_from_corpus(t),
                calls=tuple(calls),
            )
        )
    return Session(
        id=str(raw.get("session") or ""),
        agent=str(raw.get("agent_type") or ""),
        kind=str(raw.get("kind") or "main"),
        parent=raw.get("parent"),
        turns=tuple(turns),
    )


def from_corpus_sessions(rows: Sequence[Mapping[str, Any]]) -> Tuple[Session, ...]:
    """Measure-only adaptation of a whole scan. The common case."""
    return tuple(from_corpus_session(r) for r in rows)


def _epoch(v: Any) -> Optional[float]:
    """Corpus timestamps are ISO strings; the model wants seconds.

    Kept local rather than imported from ``insights``: the dependency runs from insights
    into this package, and reversing it for a six-line helper would make the two mutually
    importable.
    """
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v:
        return None
    import datetime

    try:
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
