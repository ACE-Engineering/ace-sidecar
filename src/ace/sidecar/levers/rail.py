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

Only ``measured`` may put a dollar figure on the page.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ace.sidecar.levers.counter import resolve_counter
from ace.sidecar.levers.ledger import FIDELITY_MEASURED, price_all
from ace.sidecar.levers.protocol import MODE_OFF, LeverContext, TokenCounter
from ace.sidecar.levers.registry import discover, load_settings, propose_safely, resolve_modes
from ace.sidecar.levers.types import from_corpus_sessions

__all__ = ["STATUS_NO_PACKAGE", "STATUS_ALL_OFF", "STATUS_NO_COUNTER", "STATUS_MEASURED",
           "resolve_counter", "rail_payload", "refresh_measured"]

log = logging.getLogger(__name__)

STATUS_NO_PACKAGE = "no_package"
STATUS_ALL_OFF = "all_off"
STATUS_NO_COUNTER = "no_counter"
STATUS_MEASURED = "measured"

_STATUS_NOTE = {
    STATUS_NO_PACKAGE: "no lever package installed — this release measures headroom only",
    STATUS_ALL_OFF: "levers installed, all off — enable one in ~/.ace/config.json",
    STATUS_NO_COUNTER: (
        "no exact token counter configured — the ledger prices nothing it cannot count"
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


def _measured(store: Any, *, since: Optional[float] = None) -> Dict[str, Any]:
    """Aggregated live results from the telemetry store, or ``{}``.

    Tolerant of a store that predates the ``lever_turns`` table, or of no store at all: this
    is an optional column on a dashboard that has to render either way, and an old
    ``~/.ace/telemetry.db`` is the common case immediately after an upgrade.
    """
    if store is None or not hasattr(store, "lever_summary"):
        return {}
    try:
        summary = store.lever_summary(since=since)
    except Exception:
        log.debug("[levers] lever_summary failed", exc_info=True)
        return {}
    return summary if summary.get("by_lever") else {}


def _measured_note(prior_status: Optional[str]) -> str:
    """The measured note, qualified by what discovery currently says.

    Recorded results and installed packages are two independent facts, and they disagree in
    an ordinary way: a developer measures a lever for a week, then uninstalls or disables it.
    Reporting ``no_package`` and dropping the rows would hide a real measurement behind a
    packaging detail; reporting them unqualified would imply the lever is still running.
    Both facts get said.
    """
    if prior_status == STATUS_NO_PACKAGE:
        return (
            _STATUS_NOTE[STATUS_MEASURED]
            + " — from turns already recorded; no lever package is installed now"
        )
    if prior_status == STATUS_ALL_OFF:
        return (
            _STATUS_NOTE[STATUS_MEASURED]
            + " — from turns already recorded; every installed lever is now off"
        )
    return _STATUS_NOTE[STATUS_MEASURED]


def refresh_measured(
    payload: Mapping[str, Any], store: Any, *, since: Optional[float] = None
) -> Dict[str, Any]:
    """A rail payload with its live half re-read from ``store``.

    ``insights._build_payload`` is memoised on a transcript fingerprint, which is exactly
    right for the installed/modes half — that changes when a package is installed, not when a
    turn is proxied. The measured half moves on every turn, so caching it with the rest would
    freeze the one number on the rail that is supposed to be alive. Same treatment
    ``build`` already gives ``live``.

    Returns a copy. The cached payload is shared, and writing the live keys into it is how a
    per-request value ends up served to the next caller.
    """
    out = dict(payload)
    measured = _measured(store, since=since)
    if not measured:
        return out
    out["measured"] = measured
    out["turns_observed"] = measured.get("turns_observed", 0)
    out["status"] = STATUS_MEASURED
    out["note"] = _measured_note(out.get("status"))
    return out


def rail_payload(
    sessions: Sequence[Mapping[str, Any]],
    *,
    counter: Optional[TokenCounter] = None,
    counter_note: str = "",
    credential: Optional[str] = None,
    scheme: str = "api_key",
    store: Any = None,
    since: Optional[float] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """What the dashboard needs to render the live half of the lever rail.

    Runs only levers resolved to a non-``off`` mode, over the sessions already scoped to the
    dashboard's range and agent filter. Cheap and total when nothing is installed, which is
    the common path: one entry-point lookup and an early return.

    ``store`` is the sidecar's :class:`~ace.gateway.local_store.LocalStore`. It carries the
    measured half, recorded turn by turn as levers ran on the proxy path, and reading it is
    what makes a measured result survive the request that produced it.

    ``credential``/``scheme`` are for a caller that holds one — the proxy path, which is the
    only place a credential exists at all under ``no_key: true``. The dashboard renders
    outside any request and passes neither, so it falls back to the environment and usually
    reports :data:`STATUS_NO_COUNTER` with the reason attached. That is the honest state, not
    a degraded one: this half of the rail is measured on proxied turns.
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
        "measured": {},
        "turns_observed": 0,
        "elapsed_ms": 0.0,
    }
    # Read once, up front: measured rows are recorded history and stay true regardless of what
    # is installed or enabled *right now*. Deciding `no_package` before looking would hide a
    # real measurement behind a packaging detail.
    measured_rows = _measured(store, since=since)

    def _terminal(status: str) -> Dict[str, Any]:
        base.update(status=status, note=_STATUS_NOTE[status])
        if measured_rows:
            base["measured"] = measured_rows
            base["turns_observed"] = measured_rows.get("turns_observed", 0)
            base.update(status=STATUS_MEASURED, note=_measured_note(status))
        base["elapsed_ms"] = (time.monotonic() - t0) * 1000.0
        return base

    if not found:
        return _terminal(STATUS_NO_PACKAGE)

    modes = resolve_modes(found, config=config)
    base["modes"] = modes
    active = [r for r in found if modes.get(r.id, MODE_OFF) != MODE_OFF]
    if not active:
        return _terminal(STATUS_ALL_OFF)

    # The measured half is READ, not recomputed. Levers run once, against the real request
    # body, in the proxy's background task (`levers.shadow`); this reads what they recorded.
    #
    # It cannot be derived here instead. The dashboard has transcripts — hashes and sizes,
    # no text — and an exact token delta needs the bytes. Recomputing over history would
    # force a byte-ratio fallback, which is the one thing the ledger refuses to do.
    if measured_rows:
        base["measured"] = measured_rows
        base["turns_observed"] = measured_rows.get("turns_observed", 0)
        base.update(status=STATUS_MEASURED, note=_STATUS_NOTE[STATUS_MEASURED])
        base["elapsed_ms"] = (time.monotonic() - t0) * 1000.0
        return base

    if counter is None:
        counter, counter_note = resolve_counter(credential, scheme)
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
    now = time.time()
    pairs = []
    for reg in active:
        ctx = LeverContext(
            count_tokens=counter,
            mode=modes[reg.id],
            now=now,
            settings=load_settings(reg.id, config=config),
        )
        for s in typed:
            proposal = propose_safely(reg, s, ctx)
            if proposal is not None:
                pairs.append((s, proposal))

    # Priced under a context of its own rather than whichever lever's `ctx` the loop above
    # happened to exit with. The ledger reads only `count_tokens`, so the leaked binding was
    # harmless today — but it silently attributed one lever's `settings` and `mode` to every
    # other lever's pricing, which is exactly the kind of thing that stops being harmless the
    # first time the ledger reads one more field.
    pricing_ctx = LeverContext(count_tokens=counter, now=now)
    report = price_all(pairs, pricing_ctx) if pairs else None
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
        base.update(
            status=STATUS_NO_COUNTER,
            note="levers ran but produced no measurable edit on these sessions",
        )
    return base
