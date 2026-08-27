"""ace.sidecar.levers.rail — the dashboard's view of installed levers.

Sits between the scanner and the renderer so neither has to know about levers.
``insights._build_payload`` calls :func:`rail_payload` and hands the result through; the
renderer reads it. Nothing here scans transcripts and nothing here writes HTML.

What this is honest about
-------------------------
The rail already shows what each lever would be *worth* — ``strategies.standalone_levers``,
a byte-turn simulation over the developer's own sessions. That is a headroom estimate and it
is labelled one. This module adds the other half: what an installed lever, run for real,
actually measured.

Those two numbers must never be confused, so :func:`rail_payload` reports a ``status`` that
says which of them exists, and the renderer is expected to show it. Four states, and three of
them mean "no live number":

``no_package``   nothing registers against the ``ace.sidecar.levers`` entry-point group.
                 The ordinary state for the open-source sidecar on its own.
``all_off``      levers are installed but every one resolves to ``off`` in
                 ``~/.ace/config.json``. Presence is not consent; this is the default even
                 after installing a lever package.
``no_counter``   levers ran, but no exact token counter is configured, so the ledger priced
                 nothing. A byte-ratio fallback would produce a number here — which is why
                 there is none.
``measured``     real edits, exactly counted, priced from the catalog and net of the
                 cache-write penalty.

A fifth, ``no_content``, is the one that matters most for the dashboard: the transcript scan
deliberately carries sizes and hashes rather than text, so a lever can be installed, enabled,
and still price nothing here. **``measured`` is therefore unreachable from transcripts
alone** — it needs the proxy or a hook to supply the bytes. That is a property of the
measurement path, not a bug, and the page says which of the two it is rather than showing an
empty figure both times.

Only ``measured`` may put a dollar figure on the page.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ace.sidecar.levers.ledger import FIDELITY_MEASURED, price_all
from ace.sidecar.levers.protocol import MODE_OFF, LeverContext, TokenCounter
from ace.sidecar.levers.registry import discover, load_settings, propose_safely, resolve_modes
from ace.sidecar.levers.types import from_corpus_sessions

__all__ = ["STATUS_NO_PACKAGE", "STATUS_ALL_OFF", "STATUS_NO_COUNTER",
           "STATUS_NO_CONTENT", "STATUS_MEASURED",
           "resolve_counter", "rail_payload"]

log = logging.getLogger(__name__)

STATUS_NO_PACKAGE = "no_package"
STATUS_ALL_OFF = "all_off"
STATUS_NO_COUNTER = "no_counter"
STATUS_NO_CONTENT = "no_content"
STATUS_MEASURED = "measured"

_STATUS_NOTE = {
    STATUS_NO_PACKAGE: "no lever package installed — this release measures headroom only",
    STATUS_ALL_OFF: "levers installed, all off — enable one in ~/.ace/config.json",
    STATUS_NO_COUNTER: (
        "no exact token counter configured — the ledger prices nothing it cannot count"
    ),
    STATUS_NO_CONTENT: (
        "levers ran, but the transcript scan carries sizes and hashes, not tool-result text — "
        "an exact token delta needs the bytes, which reach the ledger only through the proxy "
        "or a hook"
    ),
    STATUS_MEASURED: "measured on your own sessions, net of the cache-write penalty",
}

# Discovery walks installed distribution metadata, which is stable for the life of the
# process and is on the cached dashboard path. Re-walking it per request buys nothing.
_DISCOVERED: Optional[Sequence[Any]] = None


def _levers() -> Sequence[Any]:
    global _DISCOVERED
    if _DISCOVERED is None:
        _DISCOVERED = discover()
    return _DISCOVERED


def resolve_counter() -> tuple[Optional[TokenCounter], str]:
    """An **exact** token counter and where it came from, or ``(None, reason)``.

    Exactness is the whole requirement, so this resolves only counters that are the model
    vendor's own: Anthropic's ``/v1/messages/count_tokens`` for Claude. ``tiktoken`` is
    deliberately not offered as a fallback — it is OpenAI's BPE and only a proxy for anything
    else, and a proxy here turns a measured saving into an estimate.

    Returning ``None`` is a normal outcome, not a failure. It costs the live column and
    leaves the measured headroom rail exactly as it is.
    """
    try:
        import anthropic  # noqa: F401
    except Exception:
        return None, "the `anthropic` package is not installed"

    import os

    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return None, "no ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN in the environment"

    try:
        from anthropic import Anthropic

        client = Anthropic()
    except Exception as exc:  # credentials present but unusable
        return None, f"the Anthropic client could not be constructed ({type(exc).__name__})"

    def count(text: str, *, model: str) -> int:
        r = client.messages.count_tokens(
            model=model or "claude-sonnet-5",
            messages=[{"role": "user", "content": text}],
        )
        return int(r.input_tokens)

    return count, "Anthropic /v1/messages/count_tokens"


def rail_payload(
    sessions: Sequence[Mapping[str, Any]],
    *,
    counter: Optional[TokenCounter] = None,
    counter_note: str = "",
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """What the dashboard needs to render the live half of the lever rail.

    Runs only levers resolved to a non-``off`` mode, over the sessions already scoped to the
    dashboard's range and agent filter. Cheap and total when nothing is installed, which is
    the common path: one entry-point lookup and an early return.
    """
    t0 = time.monotonic()
    found = _levers()
    base: Dict[str, Any] = {
        "installed": [
            {"id": r.id, "label": getattr(r.lever, "label", r.id),
             "risk": getattr(r.lever, "risk", ""), "dist": r.dist,
             "requires_content": bool(getattr(r.lever, "requires_content", False))}
            for r in found
        ],
        "modes": {},
        "by_lever": {},
        "entries": [],
        "counter": counter_note,
        "elapsed_ms": 0.0,
    }
    if not found:
        base.update(status=STATUS_NO_PACKAGE, note=_STATUS_NOTE[STATUS_NO_PACKAGE])
        return base

    modes = resolve_modes(found, config=config)
    base["modes"] = modes
    active = [r for r in found if modes.get(r.id, MODE_OFF) != MODE_OFF]
    if not active:
        base.update(status=STATUS_ALL_OFF, note=_STATUS_NOTE[STATUS_ALL_OFF])
        return base

    if counter is None:
        counter, counter_note = resolve_counter()
        base["counter"] = counter_note
    if counter is None:
        base.update(
            status=STATUS_NO_COUNTER,
            note=f"{_STATUS_NOTE[STATUS_NO_COUNTER]} ({counter_note})",
        )
        return base

    # The measurement path holds no tool-result bytes, so a lever needing them is refused by
    # `propose_safely` rather than allowed to half-run. That is why a content-requiring lever
    # can be installed, enabled, and still contribute nothing here: it needs the proxy or a
    # hook to supply the text.
    typed = from_corpus_sessions(sessions)
    pairs = []
    for reg in active:
        ctx = LeverContext(
            count_tokens=counter,
            mode=modes[reg.id],
            now=time.time(),
            settings=load_settings(reg.id, config=config),
        )
        for s in typed:
            proposal = propose_safely(reg, s, ctx)
            if proposal is not None:
                pairs.append((s, proposal))

    report = price_all(pairs, ctx) if pairs else None
    if report is not None:
        base["by_lever"] = report.by_lever()
        base["entries"] = [
            {"lever": e.lever, "session": e.session_id, "agent": e.agent,
             "fidelity": e.fidelity, "net_usd": e.net_usd,
             "gross_usd": e.gross_saving_usd, "penalty_usd": e.cache_write_penalty_usd,
             "removed_tokens": e.removed_tokens, "note": e.note}
            for e in report.entries
        ]
        measured = any(e.fidelity == FIDELITY_MEASURED and e.edits for e in report.entries)
    else:
        measured = False

    base["elapsed_ms"] = (time.monotonic() - t0) * 1000.0
    if measured:
        base.update(status=STATUS_MEASURED, note=_STATUS_NOTE[STATUS_MEASURED])
    else:
        # Distinguish "could not count" from "nothing to do" — they look identical on the
        # page and mean opposite things about whether this lever is worth enabling.
        base.update(status=STATUS_NO_CONTENT, note=_STATUS_NOTE[STATUS_NO_CONTENT])
    return base
