"""ace.sidecar.benchmarks — a reference number beside each metric, and where it came from.

A figure with nothing beside it cannot be acted on. "$0.06 per turn" is not high or low until
something says what else it could have been, and the dashboard has been asking readers to
supply that comparison from memory.

The rule this module exists to enforce
---------------------------------------
**Every reference carries its provenance, and a metric with no grounded reference says so
rather than showing a number.**

That is not caution for its own sake. This repository already keeps a provenance ledger for
model capability claims (``data/model_market/benchmarks.yaml``), where ``source: ""`` means
"nobody has pointed at the leaderboard this came from yet" and a staleness gate nags until
someone does. The same standard applies here, and it rules out the most tempting content on a
page like this: an "industry average cost per turn", or an "optimal number of conversation
turns for coding quality". Both would be inventions. Neither is measured anywhere this process
can see, and a fabricated benchmark is worse than a blank one — a reader cannot tell them
apart, and the blank one at least prompts the right question.

The three kinds of reference that ARE grounded
-----------------------------------------------
``SELF``       the developer's own distribution, computed from the sessions in scope. The
               strongest available comparison and the only one that is definitionally about
               this machine: "you are at the 90th percentile of your own sessions" is
               actionable and cannot be contested.

``PEER``       another agent on this same machine over the same window. Claude Code against
               Antigravity against Codex is a controlled comparison — one developer, one
               period, one corpus — which is more than most published benchmarks can say.

``PUBLISHED``  a provider-stated constant, carried with the URL and date already recorded in
               ``ace.gateway.pricing`` (cache reads at 0.1x, writes at 1.25x/2x). These are
               contractual, not empirical, so they are exact.

``MEASURED``   a figure this repository derived and documented, with the derivation kept next
               to the number: the 2.8 bytes/token constant measured over 4,512 tool results,
               and the truncation re-read rate measured over the local transcript corpus.

``NONE``       no grounded reference exists. Rendered as such, with what it would take to
               establish one — which is a task, not an apology.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "SELF", "PEER", "PUBLISHED", "MEASURED", "NONE",
    "Reference", "references", "percentile",
]

SELF = "self"
PEER = "peer"
PUBLISHED = "published"
MEASURED = "measured"
NONE = "none"

# Where a MEASURED reference was derived, so the number can be chased to its working. Kept
# beside the values rather than in prose: a citation that is not attached to the figure it
# supports gets separated from it by the first refactor.
_PRICING_DOC = "ace.gateway.pricing (provider-published, carries source + as_of)"
_BPT_DOC = "ace.sidecar.strategies.BYTES_PER_TOKEN — measured over 4,512 tool results"
_REREAD_DOC = "ace.sidecar.levers.shadow.observe_revisits — measured on the local corpus"


@dataclass(frozen=True, slots=True)
class Reference:
    """One metric's comparison point, and the receipt for it.

    ``value`` is ``None`` for :data:`NONE` references. Callers must render that as an absence,
    never as a zero — a benchmark of 0 is a claim, and the whole point here is not to make one.
    """

    kind: str
    label: str = ""
    value: Optional[float] = None
    unit: str = ""
    source: str = ""
    as_of: str = ""
    note: str = ""
    # p25 / p50 / p90 where the reference is a distribution rather than a point.
    band: Optional[Sequence[float]] = None

    @property
    def grounded(self) -> bool:
        return self.kind != NONE and self.value is not None

    def verdict(self, actual: Optional[float], lower_is_better: bool = True) -> str:
        """Where ``actual`` sits against the band. Empty when there is nothing to say."""
        if not self.band or actual is None:
            return ""
        p25, p50, p90 = self.band[0], self.band[1], self.band[2]
        if actual <= p25:
            return "best quartile" if lower_is_better else "bottom quartile"
        if actual <= p50:
            return "better than median" if lower_is_better else "below median"
        if actual <= p90:
            return "worse than median" if lower_is_better else "above median"
        return "worst decile" if lower_is_better else "top decile"


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile. Deliberately not interpolated.

    An interpolated percentile invents a value that no session actually had, which is wrong
    for a reference the reader is invited to go and find in their own history.
    """
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return 0.0
    idx = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return float(xs[idx])


def _band(values: Sequence[float]) -> Optional[List[float]]:
    # Below this the quartiles are noise: with six sessions, "p90" is the second-largest.
    if len(values) < 12:
        return None
    return [percentile(values, 0.25), percentile(values, 0.50), percentile(values, 0.90)]


def _self_ref(values: Sequence[float], unit: str, what: str) -> Reference:
    band = _band(values)
    if band is None:
        return Reference(
            NONE, "no reference",
            note=(
                f"{len(values)} {what} in scope — too few for a distribution. Widen the "
                f"range, or keep working and this fills in."
            ),
        )
    return Reference(
        SELF, "your median", value=band[1], unit=unit, band=band,
        source=f"your own {what} in this scope",
        note=(
            f"p25 {band[0]:,.4g} · p50 {band[1]:,.4g} · p90 {band[2]:,.4g}, across "
            f"{len(values):,} {what} on this machine."
        ),
    )


def _peer_ref(agents: Sequence[Mapping[str, Any]], key: str, unit: str) -> Reference:
    """The same metric on another agent, same machine, same window.

    A controlled comparison: one developer, one period, one corpus. Only offered when a
    second agent actually ran here — a peer reference invented from a single data point is
    just the number again.
    """
    rows = [
        (a.get("label") or a.get("agent") or "?", float(a[key]))
        for a in agents
        if a.get(key) not in (None, 0)
    ]
    if len(rows) < 2:
        return Reference(NONE, "no peer", note="only one agent ran on this machine in scope.")
    rows.sort(key=lambda kv: kv[1])
    best_label, best = rows[0]
    return Reference(
        PEER, f"best agent here ({best_label})", value=best, unit=unit,
        source="this machine, same window",
        note=" · ".join(f"{lbl} {val:,.4g}" for lbl, val in rows),
    )


def _from_pctl(pctl: Mapping[str, Any], keys=("p25", "p50", "p99")) -> Optional[List[float]]:
    """A band out of one of `fleet`'s percentile dicts.

    The dashboard's own summaries carry p25/p50/p99 rather than p90 — read the keys that are
    actually there instead of assuming a shape, which is how a reference silently becomes
    zero.
    """
    vals = [pctl.get(k) for k in keys]
    if any(v is None for v in vals):
        return None
    return [float(v) for v in vals]


def references(d: Mapping[str, Any]) -> Dict[str, Reference]:
    """A reference for every metric the dashboard shows one for.

    Keyed by the tile's own label so the renderer can look one up without a mapping table.
    Absences are returned explicitly rather than omitted: a missing key renders as a tile with
    no reference, which is indistinguishable from a tile whose reference is "none exists" —
    and those are different statements.
    """
    out: Dict[str, Reference] = {}
    f = d.get("fleet") or {}

    # -- distribution-backed, from the developer's own sessions ---------------------------
    band = _from_pctl(f.get("cost_per_session") or {}, ("p50", "p50", "p99"))
    cps = f.get("cost_per_session") or {}
    if cps.get("p50") is not None:
        out["cost_per_session [6]"] = Reference(
            SELF, "your median session", value=float(cps["p50"]), unit="$",
            band=[float(cps["p50"]), float(cps["p50"]), float(cps.get("p99") or 0)],
            source="your own sessions in this scope",
            note=(
                f"median ${cps['p50']:,.2f} · p99 ${cps.get('p99') or 0:,.2f} · "
                f"max ${cps.get('max') or 0:,.2f}. The mean is ${cps.get('mean') or 0:,.2f} — "
                "far above the median, so a few long sessions carry the bill."
            ),
        )

    ctx = f.get("context") or {}
    if ctx.get("p50") is not None:
        out["tokens_in [1]"] = Reference(
            SELF, "your median request", value=float(ctx["p50"]), unit="tok",
            band=[float(ctx.get("p25") or 0), float(ctx["p50"]), float(ctx.get("p99") or 0)],
            source="your own API requests in this scope",
            note=(
                f"context carried per request — p25 {ctx.get('p25') or 0:,.0f} · "
                f"p50 {ctx['p50']:,.0f} · p99 {ctx.get('p99') or 0:,.0f} · "
                f"max {ctx.get('max') or 0:,.0f}."
            ),
        )

    rps = f.get("requests_per_session") or {}
    if rps.get("p50") is not None:
        out["api_requests [2]"] = Reference(
            SELF, "your median session", value=float(rps["p50"]), unit="req",
            band=[float(rps.get("p25") or 0), float(rps["p50"]), float(rps.get("p99") or 0)],
            source="your own sessions in this scope",
            note=(
                f"requests per session — p25 {rps.get('p25') or 0:,.0f} · "
                f"p50 {rps['p50']:,.0f} · p99 {rps.get('p99') or 0:,.0f}."
            ),
        )

    # -- published, contractual ------------------------------------------------------------
    out["list_price_cost"] = Reference(
        PUBLISHED, "cache read = 0.1x input", value=0.1, unit="x",
        source=_PRICING_DOC, as_of=_price_as_of(),
        note=(
            "Anthropic bills a cache read at 0.1x the input rate, a 5-minute cache write at "
            "1.25x and a one-hour write at 2.0x. Contractual, not empirical. It also caps "
            "every volume lever on this page: tokens removed from a cached prefix save a "
            "tenth of what fresh ones would."
        ),
    )

    # -- peer: the same metric on another agent, same machine, same window -----------------
    agents = d.get("agent_breakdown") or {}
    rows = []
    if isinstance(agents, dict):
        for a in agents.values():
            turns, cost = a.get("turns") or 0, a.get("cost_usd") or 0.0
            if turns and cost:
                rows.append((a.get("label") or a.get("agent_type") or "?", cost / turns))
    if len(rows) >= 2:
        rows.sort(key=lambda kv: kv[1])
        out["cost_per_turn [3]"] = Reference(
            PEER, f"cheapest agent here ({rows[0][0]})", value=rows[0][1], unit="$",
            source="this machine, same window",
            note=(
                "A controlled comparison — one developer, one period, one corpus: "
                + " · ".join(f"{lbl} ${v:,.4f}/turn" for lbl, v in rows)
                + ". Different agents do different work, so this bounds the spread rather "
                  "than naming a target."
            ),
        )

    # -- the one a reader most wants, and the one nobody has measured -----------------------
    out["conversation_turns [3]"] = Reference(
        NONE, "no published reference",
        note=(
            "There is no grounded figure for how many turns a coding session should run "
            "before quality degrades — none this process can verify, and an invented one "
            "would be indistinguishable from a measured one on this page. Establishing it "
            "needs a replay eval: the same task at varying context lengths, scored. "
            f"For scale, your own sessions run {f.get('requests_per_handback') or 0:,.1f} "
            "API requests per turn."
        ),
    )

    # -- measured here, with the derivation attached ----------------------------------------
    out["tokens_out [1]"] = Reference(
        MEASURED, "2.8 bytes/token", value=2.8, unit="B",
        source=_BPT_DOC,
        note=(
            "Agent tool output tokenizes far denser than the familiar 4.0 for English prose: "
            "measured p50 2.16, p95 2.82 over 4,512 tool results. The 4.0 everyone reaches "
            "for sits at the 99th percentile of that distribution, so byte-based estimates "
            "built on it understate volume by ~1.4x."
        ),
    )

    return out


def _price_as_of() -> str:
    try:
        from ace.gateway.pricing import rates_for

        r = rates_for("claude-sonnet-5")
        return r.as_of if r else ""
    except Exception:
        return ""
