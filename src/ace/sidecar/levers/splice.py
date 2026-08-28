"""ace.sidecar.levers.splice — editing the outbound body without re-serializing it.

The problem this solves
-----------------------
A lever that actually applies has to change the bytes going upstream, and the obvious way —
``json.loads``, edit, ``json.dumps`` — is the one thing ``ace.gateway.messages`` exists to
forbid. Not because JSON key order is sacred (the server parses before it tokenizes, so key
order and whitespace normalize away) but because a round trip through an intermediate
representation *loses fields it has no slot for*. Anthropic's ``cache_control`` markers and
extended-thinking ``signature`` values are exactly that kind of field, and losing either is
expensive:

* drop ``cache_control`` and no breakpoint is declared, so nothing is cached at all;
* drop or alter a thinking ``signature`` and upstream rejects the turn.

So this module never parses the body into Python and re-emits it. It locates the byte range of
one tool result's ``content`` value inside the original buffer and replaces **that span only**.
Every other byte — the markers, the signatures, the whitespace, the key order — is the
original, because it was never touched.

Why a byte splice is also the only *cheap* edit
------------------------------------------------
Anthropic's prompt cache is a prefix cache matched by hash. A difference at position *N*
invalidates everything from *N* to the end, and that prefix is then re-written at 1.25x (5m)
or 2x (1h) against a 0.1x read — a **12.5x** swing. On the reference corpus a turn carries
~161,587 cached tokens, so busting the prefix costs ~$0.37 while the truncation lever earns
~$0.0035 a turn. Break even needs the cache intact on more than 99 turns in 100.

Which yields the hard rule this module enforces, and refuses to proceed without:

    **Never splice at or before the last declared cache breakpoint.**

:func:`last_breakpoint_offset` finds where that is, and :func:`plan` drops any edit that lands
earlier. It is a byte-offset comparison against the caller's own ``cache_control`` markers, so
it is checkable rather than argued — unlike "this lever only touches the tail", which is a
property of the lever's intent rather than of the bytes actually sent.

What this module does not do
----------------------------
It does not decide *what* to edit. Levers propose, ``shadow`` resolves positions, and this
applies. It also refuses rather than degrades: an edit it cannot place exactly is dropped and
reported, because a mis-placed splice corrupts a request that a developer's agent is waiting
on, and a lever's saving is never worth that.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ace.sidecar.levers.protocol import Edit

__all__ = [
    "SpliceRefused",
    "Splice",
    "last_breakpoint_offset",
    "find_result_content_span",
    "plan",
    "apply_splices",
]

log = logging.getLogger(__name__)

# The marker Anthropic reads to place a cache breakpoint. Matched as raw bytes rather than
# parsed, because the whole point here is to never parse.
_BREAKPOINT = b'"cache_control"'
_TOOL_USE_ID = b'"tool_use_id"'
_CONTENT_KEY = b'"content"'

_WS = b" \t\r\n"


class SpliceRefused(RuntimeError):
    """A splice could not be placed safely. Always caught; never fatal to a request."""


@dataclass(frozen=True, slots=True)
class Splice:
    """One byte range in the original body, and what replaces it."""

    start: int
    end: int
    replacement: bytes
    tool_use_id: str = ""
    reason: str = ""

    @property
    def delta_bytes(self) -> int:
        return len(self.replacement) - (self.end - self.start)


def last_breakpoint_offset(raw: bytes) -> int:
    """Byte offset just past the last ``cache_control`` marker, or ``-1`` if there is none.

    Everything at or before this offset is, or may become, a cached prefix. Editing there
    invalidates it. Everything after is content this turn introduced, which no cache holds
    yet — the region where an edit is free.

    Deliberately a naive byte scan. A parser would be more precise about markers appearing
    inside string literals, and would also be a parse — the operation this module exists to
    avoid. The error is one-sided and safe: a spurious match can only push the boundary
    *later*, refusing edits that would have been fine. It can never permit one that is not.
    """
    return raw.rfind(_BREAKPOINT)


def _skip_ws(raw: bytes, i: int) -> int:
    while i < len(raw) and raw[i] in _WS:
        i += 1
    return i


def _scan_string(raw: bytes, i: int) -> int:
    """End offset (exclusive) of the JSON string starting at ``raw[i] == '"'``."""
    if i >= len(raw) or raw[i] != 0x22:
        raise SpliceRefused("expected a string")
    i += 1
    while i < len(raw):
        c = raw[i]
        if c == 0x5C:  # backslash — skip the escaped char, including \" and \\
            i += 2
            continue
        if c == 0x22:
            return i + 1
        i += 1
    raise SpliceRefused("unterminated string")


def _scan_value(raw: bytes, i: int) -> int:
    """End offset (exclusive) of the JSON value starting at ``i``.

    Handles the shapes a tool result's ``content`` can take: a string, or an array of blocks.
    Objects and scalars are supported so the scanner stays total rather than special-cased.
    """
    i = _skip_ws(raw, i)
    if i >= len(raw):
        raise SpliceRefused("value ended early")
    c = raw[i]
    if c == 0x22:
        return _scan_string(raw, i)
    if c in (0x5B, 0x7B):  # [ or {
        close = 0x5D if c == 0x5B else 0x7D
        depth = 0
        while i < len(raw):
            c = raw[i]
            if c == 0x22:
                i = _scan_string(raw, i)
                continue
            if c in (0x5B, 0x7B):
                depth += 1
            elif c in (0x5D, 0x7D):
                depth -= 1
                if depth == 0:
                    if c != close:
                        raise SpliceRefused("mismatched bracket")
                    return i + 1
            i += 1
        raise SpliceRefused("unterminated container")
    # number, true, false, null
    j = i
    while j < len(raw) and raw[j] not in b",}] \t\r\n":
        j += 1
    if j == i:
        raise SpliceRefused("empty value")
    return j


def find_result_content_span(raw: bytes, tool_use_id: str) -> Tuple[int, int]:
    """Byte range of the ``content`` value of the tool result answering ``tool_use_id``.

    Anchored on the id rather than on position. A tool_use_id is unique within a body and is
    the same thing that ties a result to its call, so it survives anything that reorders or
    renumbers blocks — which a positional anchor would not.

    Raises :class:`SpliceRefused` rather than guessing. Every failure mode here means the body
    is not shaped the way the caller believed, and acting on that belief is how a request gets
    corrupted.
    """
    needle = json.dumps(tool_use_id).encode()
    at = -1
    scan = 0
    while True:
        k = raw.find(_TOOL_USE_ID, scan)
        if k < 0:
            break
        j = _skip_ws(raw, k + len(_TOOL_USE_ID))
        if j < len(raw) and raw[j] == 0x3A:  # ':'
            j = _skip_ws(raw, j + 1)
            end = _scan_string(raw, j)
            if raw[j:end] == needle:
                at = k
                break
        scan = k + len(_TOOL_USE_ID)
    if at < 0:
        raise SpliceRefused(f"tool_use_id {tool_use_id!r} not found in the body")

    # `content` lives in the same object. Search forward from the id, and bail at the object's
    # end so a neighbouring block's content is never picked up by mistake.
    obj_end = _object_end(raw, at)
    k = raw.find(_CONTENT_KEY, at, obj_end)
    if k < 0:
        # Anthropic allows content before tool_use_id; look backwards within the object too.
        obj_start = _object_start(raw, at)
        k = raw.find(_CONTENT_KEY, obj_start, at)
        if k < 0:
            raise SpliceRefused("no content key on this tool_result")
    j = _skip_ws(raw, k + len(_CONTENT_KEY))
    if j >= len(raw) or raw[j] != 0x3A:
        raise SpliceRefused("malformed content key")
    j = _skip_ws(raw, j + 1)
    return j, _scan_value(raw, j)


def _object_start(raw: bytes, i: int) -> int:
    depth = 0
    while i >= 0:
        c = raw[i]
        if c == 0x7D:
            depth += 1
        elif c == 0x7B:
            if depth == 0:
                return i
            depth -= 1
        i -= 1
    return 0


def _object_end(raw: bytes, i: int) -> int:
    depth = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == 0x22:
            i = _scan_string(raw, i)
            continue
        if c == 0x7B:
            depth += 1
        elif c == 0x7D:
            if depth <= 1:
                return i + 1
            depth -= 1
        i += 1
    return n


def plan(
    raw: bytes,
    edits: Sequence[Edit],
    call_ids: Mapping[Tuple[int, int], str],
    *,
    replacements: Mapping[Tuple[int, int], str],
) -> Tuple[List[Splice], List[str]]:
    """Turn lever edits into byte ranges, dropping any that is not provably prefix-safe.

    ``replacements`` carries the already-computed new text per ``(turn, call)`` — produced by
    the same code that builds the shadow counterfactual, so an applied edit is byte-identical
    to the one that was measured. Measuring one thing and applying another is the failure this
    seam is arranged to prevent.

    Returns ``(splices, refusals)``. Refusals are reported, never silently dropped: a lever
    whose every edit was refused must not look like a lever that found nothing.
    """
    boundary = last_breakpoint_offset(raw)
    out: List[Splice] = []
    refused: List[str] = []

    for e in edits:
        key = (e.turn_index, e.call_index)
        tid = call_ids.get(key)
        new_text = replacements.get(key)
        if not tid or new_text is None:
            refused.append(f"{key}: no tool_use_id or replacement text")
            continue
        try:
            start, end = find_result_content_span(raw, tid)
        except SpliceRefused as exc:
            refused.append(f"{key}: {exc}")
            continue

        # THE guard. A byte-offset comparison against the caller's own breakpoints, not a
        # claim about the lever's intent.
        if start <= boundary:
            refused.append(
                f"{key}: content at byte {start} is at or before the last cache breakpoint "
                f"({boundary}) — editing there invalidates the cached prefix"
            )
            continue

        out.append(Splice(start, end, json.dumps(new_text).encode(), tid, e.reason))

    # Right to left, so each range stays valid as earlier ones are rewritten.
    out.sort(key=lambda s: -s.start)
    return out, refused


def apply_splices(raw: bytes, splices: Sequence[Splice]) -> bytes:
    """Apply non-overlapping splices to ``raw``, verifying the result before returning it.

    Three checks, because this output is what a developer's agent actually receives:

    * the spliced body still parses — a corrupted request is worse than an unoptimized one;
    * every byte before the first splice is unchanged, which is the cache-safety property
      stated as an assertion rather than a comment;
    * ranges do not overlap, which would silently produce nonsense.

    Raises :class:`SpliceRefused` on any failure. The caller sends the ORIGINAL bytes when
    that happens.
    """
    if not splices:
        return raw

    ordered = sorted(splices, key=lambda s: s.start)
    for a, b in zip(ordered, ordered[1:]):
        if b.start < a.end:
            raise SpliceRefused("overlapping splices")

    out = bytearray(raw)
    for s in sorted(splices, key=lambda x: -x.start):
        out[s.start:s.end] = s.replacement
    new = bytes(out)

    first = ordered[0].start
    if new[:first] != raw[:first]:  # pragma: no cover - defensive; slicing cannot do this
        raise SpliceRefused("prefix changed — refusing to send")
    try:
        json.loads(new)
    except Exception as exc:
        raise SpliceRefused(f"spliced body no longer parses: {exc}") from exc
    return new
