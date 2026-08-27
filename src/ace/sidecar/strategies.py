"""ace.sidecar.strategies — the optimization simulation, importable by the dashboard.

The same model as ``scripts/optimize_strategies.py``, moved into the package so the sidecar
dashboard scores a developer's **own** local sessions rather than the committed corpus. One
implementation, two callers — a second copy would drift from the first the moment a lever
changed.

Model
-----
Each tool result is an object with a birth turn, a byte size, and a live-until turn. Baseline
is ``bytes x (session_end - birth)`` — "byte-turns", the quantity cache-read cost is
proportional to, because content resident in context is re-read on every later turn.

Levers narrow a result's life or its size, applied **per result, first match wins**, so two
levers can never suppress the same bytes twice.

Two families, and they are not interchangeable:

* **volume levers** remove tokens — they help both a cost bill and a token cap;
* **accounting levers** (mutation, TTL) convert *price* without sending less — worth real
  money, and worth exactly nothing against a cap.

Conflating those is the single easiest way to overstate this product.
"""

from __future__ import annotations

import collections
import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Byte-equivalents per token, for converting the byte-turns `simulate` counts into the
# tokens and dollars `score` reports.
#
# Measured, not assumed. Against 4,512 text-only, single-tool-call results drawn from the
# local Claude Code corpus, the observed characters-per-token distribution is:
#
#     p10 1.51   p25 1.84   median 2.16   p75 2.41   p90 2.64   p95 2.82   p99 4.34
#
# The previous value of 4.0 sat at the **99th percentile** of that distribution — not a
# central estimate but its extreme tail. 4.0 is the familiar figure for English prose; agent
# tool output is code, JSON, logs, diffs and file paths, which tokenize far denser. The same
# mistake, in the same direction, is recorded in ``ace.gateway.tokenizer``: a long-context
# gate computed at ~1.33 tokens/word on content that is really up to 3.3.
#
# The measurement is derived per turn as ``(prompt[i+1] - prompt[i]) - output[i]``, which is
# ground truth on both sides but is contaminated upward on the token leg by anything else
# that entered the prompt between the two turns (injected reminders, re-read context). That
# contamination can only *reduce* the observed ratio, so the honest estimate lives in the
# upper tail rather than at the median. 2.8 is that tail (~p95).
#
# Direction of the correction matters: `score` divides by this constant, so a LOWER value
# reports MORE tokens and more dollars. Moving 4.0 -> 2.8 raises every byte-turn headroom
# figure on the rail by ~1.43x. That is the direction that warrants caution, which is why
# the value chosen is the conservative end of the measured band and not its median.
#
# COUPLED: ``insights._CHARS_PER_TOKEN`` must hold the same value. `_measure` converts an
# image's known token count *into* byte-equivalents by multiplying by it, and the division
# here converts back — so images round-trip exactly when the two agree and are mispriced by
# their ratio when they do not. See :func:`_check_image_bridge`.
BYTES_PER_TOKEN = 2.8
POINTER_BYTES = 120
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")
TTL_SECONDS = 3600.0

RISK_NONE, RISK_LOW, RISK_MED, RISK_HIGH = "NONE", "LOW", "MEDIUM", "HIGH"

LEVER_RISK = {
    "read_dedup": (RISK_NONE, "byte-identical content, write-gated — provable"),
    "supersede": (
        RISK_LOW,
        "the older run of the identical call is stale by definition",
    ),
    "age_out": (RISK_HIGH, "quality cost unmeasured — no session was replayed aged"),
    # Demoted from HIGH on measurement, not on argument: splitting the recovered value by
    # whether the output's head carries a diagnostic puts 90% of it in `sed`/`cat` dumps,
    # `grep` roll-ups and `git diff`s. Keeping head+tail and exempting the diagnostic class
    # takes that 90% at close to zero risk.
    "truncate": (
        RISK_LOW,
        "90% of the value is dumps, not diagnostics; keep head+tail",
    ),
    "mutation_fix": (
        RISK_NONE + "*",
        "*only if the mutating field is safely normalisable",
    ),
    "ttl_keepalive": (RISK_NONE, "purely economic; the model sees nothing different"),
}


@dataclass
class Strategy:
    name: str
    read_dedup: bool = True
    supersede: bool = True
    age_out: Optional[int] = None
    truncate: Optional[int] = None
    truncate_tools: Tuple[str, ...] = ("Bash",)
    ttl_keepalive: bool = False
    mutation_fix: bool = False
    tier: str = ""
    note: str = ""


# The tiers the analysis settled on. Enterprise includes the accounting levers because its
# objective is dollars; User excludes them because they remove no tokens.
ENTERPRISE_TIERS = [
    Strategy(
        "enterprise",
        tier="SAFE",
        ttl_keepalive=True,
        mutation_fix=True,
        note="nothing the model sees changes shape",
    ),
    Strategy(
        "enterprise",
        tier="BALANCED",
        age_out=400,
        ttl_keepalive=True,
        mutation_fix=True,
        note="adds a 400-turn memory window",
    ),
    Strategy(
        "enterprise",
        tier="AGGRESSIVE",
        age_out=100,
        truncate=1024,
        ttl_keepalive=True,
        mutation_fix=True,
        note="short memory + truncation; needs a quality eval",
    ),
]
USER_TIERS = [
    Strategy("user", tier="SAFE", note="provably redundant content only"),
    Strategy(
        "user",
        tier="BALANCED",
        age_out=200,
        truncate=2048,
        note="200-turn memory + 2KB Bash cap",
    ),
    Strategy(
        "user",
        tier="AGGRESSIVE",
        age_out=50,
        truncate=512,
        note="50-turn memory + 512B cap — the sweep maximum",
    ),
]


# Each volume lever alone, at the operating point the measurement recommends, so the four
# can be ranked against each other. These are *standalone* — every lever is scored against
# the full corpus with the others switched off, so the shares overlap and must not be summed.
# Composed, non-overlapping totals are what the tier scorecards in § 04/05 are for.
#
# The operating points are the defensible ones from
# ``docs/analysis_docs/2026-07-29-phase2-context-lever-headroom.md`` § 6, not the maximal ones: age-out at
# 400 turns rather than 100 (which is worth 2.5x more and carries unmeasured quality risk),
# and a 512-token Bash cap rather than 256.
STANDALONE = [
    (
        "age-out",
        "age_out",
        Strategy("standalone", read_dedup=False, supersede=False, age_out=400),
        "drop tool results older than 400 turns",
        "at 100 turns it is worth ~2.5x this, at HIGH unmeasured risk",
    ),
    (
        # `Strategy.truncate` is a *byte* cap (the tier notes above read "2KB", "512B"), so
        # the 512-**token** operating point the analysis recommends is 2048 bytes here.
        "bash truncate",
        "truncate",
        Strategy(
            "standalone",
            read_dedup=False,
            supersede=False,
            truncate=int(512 * BYTES_PER_TOKEN),
        ),
        "cap Bash output at 512 tokens, head+tail",
        "90% of the value is dumps and listings, not diagnostics",
    ),
    (
        "supersede",
        "supersede",
        Strategy("standalone", read_dedup=False, supersede=True),
        "drop a result the identical later call made stale",
        "mostly screenshot loops re-shooting a page that already moved",
    ),
    (
        "read de-dup",
        "read_dedup",
        Strategy("standalone", read_dedup=True, supersede=False),
        "drop a read whose bytes are provably already resident",
        "97.6% of repeat reads ask for a different line range and are not duplicates",
    ),
]


def standalone_levers(
    sessions: List[Dict[str, Any]],
    acct: Dict[str, float],
    rates_for,
    billed: float = 0.0,
) -> List[Dict[str, Any]]:
    """The four volume levers, each scored alone, ranked by what it is worth.

    This is the ranking question — "which lever should we build first" — and it is not
    answerable from the tier scorecards, where a lever's contribution depends on which
    levers preceded it under first-match-wins.
    """
    out: List[Dict[str, Any]] = []
    for label, key, strat, detail, caveat in STANDALONE:
        scored = score(sessions, strat, acct, rates_for)
        hit = next((x for x in scored["levers"] if x["name"] == key), None)
        usd = hit["usd"] if hit else 0.0
        out.append(
            {
                "name": key,
                "label": label,
                "usd": usd,
                "tokens": hit["tokens"] if hit else 0.0,
                "share": (usd / billed) if billed else 0.0,
                "risk": LEVER_RISK.get(key, ("", ""))[0],
                "why": LEVER_RISK.get(key, ("", ""))[1],
                "detail": detail,
                "caveat": caveat,
            }
        )
    out.sort(key=lambda x: -x["usd"])
    for i, lv in enumerate(out, 1):
        lv["rank"] = i
    return out


@dataclass
class _Result:
    turn: int
    tool: str
    target: Optional[str]
    size: int
    sig: str = ""  # hash of the whole tool input, so line ranges differentiate
    digest: str = ""  # hash of the result content, so redundancy is provable
    live_until: int = 1 << 30
    kept: int = -1
    reason: str = ""

    def effective(self) -> int:
        return self.size if self.kept < 0 else self.kept


def simulate(session: Dict[str, Any], s: Strategy) -> Dict[str, float]:
    """Byte-turns saved per lever, for one session."""
    turns = session["turns"]
    n = len(turns)
    results: List[_Result] = []
    writes: Dict[str, List[int]] = collections.defaultdict(list)
    for i, t in enumerate(turns):
        for call in t.get("calls") or []:
            name = call.get("name") or "?"
            tgt = call.get("target")
            if name in WRITE_TOOLS and tgt:
                writes[tgt].append(i)
            if call.get("result_bytes"):
                results.append(
                    _Result(
                        turn=i,
                        tool=name,
                        target=tgt,
                        size=call["result_bytes"],
                        sig=call.get("sig") or "",
                        digest=call.get("digest") or "",
                    )
                )
    for r in results:
        r.live_until = n
    saved: Dict[str, float] = collections.Counter()

    if s.read_dedup:
        # Verified de-dup: the bytes must *provably* already be resident. An earlier read
        # of the same path is not enough — 97.6% of repeat reads in the measured corpus ask
        # for a different line range and carry new content, so keying on the path alone
        # scores a paginated walk as duplication and overstates the lever by two orders of
        # magnitude ($36.88 vs $0.33). Requiring an equal content digest is what makes the
        # claim checkable. Where no digest was captured the lever declines to fire.
        prior: Dict[str, List[_Result]] = collections.defaultdict(list)
        for r in results:
            if r.tool != "Read" or not r.target:
                continue
            written = writes.get(r.target, ())
            if r.digest:
                for cand in reversed(prior[r.target]):
                    if any(cand.turn < w <= r.turn for w in written):
                        continue  # the file changed between the two reads
                    if cand.digest and cand.digest == r.digest:
                        gain = (r.size - POINTER_BYTES) * (n - r.turn)
                        if gain > 0:
                            r.kept = POINTER_BYTES
                            r.reason = "read_dedup"
                            saved["read_dedup"] += gain
                        break
            prior[r.target].append(r)

    if s.supersede:
        # Identity is the whole tool input, not the target: running the *identical* call
        # again is what makes the earlier output stale. Re-reading a different slice of the
        # same file supersedes nothing.
        groups: Dict[str, List[_Result]] = collections.defaultdict(list)
        for r in results:
            if r.sig and not r.reason:
                groups[r.sig].append(r)
        for g in groups.values():
            for a, b in zip(g, g[1:]):
                if a.live_until > b.turn:
                    saved["supersede"] += a.effective() * (a.live_until - b.turn)
                    a.live_until = b.turn
                    a.reason = a.reason or "supersede"

    if s.age_out:
        for r in results:
            cut = min(r.live_until, r.turn + s.age_out)
            if cut < r.live_until:
                saved["age_out"] += r.effective() * (r.live_until - cut)
                r.live_until = cut
                r.reason = r.reason or "age_out"

    if s.truncate:
        for r in results:
            if r.tool in s.truncate_tools and r.effective() > s.truncate:
                saved["truncate"] += (r.effective() - s.truncate) * (
                    r.live_until - r.turn
                )
                r.kept = s.truncate
                r.reason = r.reason or "truncate"

    return dict(saved)


def _ts(v: Optional[str]) -> Optional[datetime.datetime]:
    if not v:
        return None
    try:
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def accounting(sessions: List[Dict[str, Any]], rates_for) -> Dict[str, float]:
    """Mutation and TTL levers, priced. Neither removes a token.

    Detected from usage alone: if the prefix survives turn N then
    ``read(N+1) ~= read(N) + write(N)``. A shortfall means tokens fell out of cache, and the
    elapsed time and context delta say why.
    """
    out: Dict[str, float] = collections.Counter()
    for sess in sessions:
        T = sess["turns"]
        for a, b in zip(T, T[1:]):
            expected = a["cache_read_input_tokens"] + a["cache_creation_input_tokens"]
            if expected < 10_000:
                continue
            short = expected - b["cache_read_input_tokens"]
            if short <= expected * 0.10:
                continue
            r = rates_for(b["model"])
            if not r:
                continue
            ta, tb = _ts(a.get("ts")), _ts(b.get("ts"))
            gap = (tb - ta).total_seconds() if (ta and tb) else 0.0
            ctx_a = expected + a["input_tokens"]
            ctx_b = (
                b["cache_read_input_tokens"]
                + b["cache_creation_input_tokens"]
                + b["input_tokens"]
            )
            # Avoiding a rewrite means paying the READ rate instead of the WRITE rate.
            delta = short / 1e6 * (r.cache_write_per_mtok("1h") - r.cache_read_per_mtok)
            if gap > TTL_SECONDS:
                out["ttl_keepalive"] += delta
            elif ctx_b < ctx_a * 0.7:
                out["compaction_excluded"] += delta  # working as intended, not waste
            else:
                out["mutation_fix"] += delta
    return dict(out)


_BRIDGE_CHECKED = False


def _check_image_bridge() -> None:
    """Warn once if ``insights._CHARS_PER_TOKEN`` has drifted from :data:`BYTES_PER_TOKEN`.

    The two constants are a matched pair, not two opinions about the same quantity.
    ``insights._measure`` prices an image at its real token count and multiplies by
    ``_CHARS_PER_TOKEN`` purely to keep one unit flowing through the pipeline; the division in
    :func:`score` undoes it. Any value works so long as both sides use the SAME one — and when
    they diverge, every image-bearing result is mispriced by exactly their ratio, silently,
    with no error and no visible symptom beyond a lever's number moving.

    Checked lazily rather than at import: ``insights`` imports this module, so a module-level
    import here would close the cycle.
    """
    global _BRIDGE_CHECKED
    if _BRIDGE_CHECKED:
        return
    _BRIDGE_CHECKED = True
    try:
        from ace.sidecar.insights import _CHARS_PER_TOKEN
    except Exception:
        return
    if abs(float(_CHARS_PER_TOKEN) - BYTES_PER_TOKEN) > 1e-9:
        import logging

        logging.getLogger(__name__).warning(
            "[strategies] insights._CHARS_PER_TOKEN=%s != BYTES_PER_TOKEN=%s — image-bearing "
            "tool results are mispriced by %.2fx. Set them to the same value.",
            _CHARS_PER_TOKEN,
            BYTES_PER_TOKEN,
            float(_CHARS_PER_TOKEN) / BYTES_PER_TOKEN,
        )


def score(
    sessions: List[Dict[str, Any]],
    s: Strategy,
    acct: Dict[str, float],
    rates_for,
) -> Dict[str, Any]:
    """One strategy over a corpus: dollars and tokens, per lever."""
    tokens: Dict[str, float] = collections.Counter()
    usd: Dict[str, float] = collections.Counter()
    for sess in sessions:
        counts = collections.Counter(t["model"] for t in sess["turns"])
        rate = 0.0
        for model, _ in counts.most_common():
            r = rates_for(model)
            if r:
                rate = r.cache_read_per_mtok
                break
        for lever, byte_turns in simulate(sess, s).items():
            _check_image_bridge()
            tok = byte_turns / BYTES_PER_TOKEN
            tokens[lever] += tok
            usd[lever] += tok / 1e6 * rate
    if s.mutation_fix:
        usd["mutation_fix"] += acct.get("mutation_fix", 0.0)
    if s.ttl_keepalive:
        usd["ttl_keepalive"] += acct.get("ttl_keepalive", 0.0)
    return {
        "tier": s.tier,
        "note": s.note,
        "levers": [
            {
                "name": k,
                "usd": usd.get(k, 0.0),
                "tokens": tokens.get(k, 0.0),
                "risk": LEVER_RISK.get(k, ("", ""))[0],
                "why": LEVER_RISK.get(k, ("", ""))[1],
            }
            for k in sorted(usd, key=lambda x: -usd[x])
        ],
        "usd_total": sum(usd.values()),
        "tokens_total": sum(tokens.values()),
    }
