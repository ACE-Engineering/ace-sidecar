"""ace.sidecar.levers.registry — discovery, mode resolution, and failure isolation.

Why discovery is by entry point
-------------------------------
This package defines what a lever *is*; it deliberately contains none. Implementations ship
in a separate distribution and register themselves::

    [project.entry-points."ace.sidecar.levers"]
    trajectory_compaction = "ace_skills.compaction:TrajectoryCompaction"

Nothing here names a lever, imports one, or fails without one. That is the property being
bought: this repository stays a measurement product that gains optimizations when a package
providing them is present, and the package providing them needs no change here to land.

A missing lever package is the ordinary case, not an error state. :func:`discover` returns
an empty tuple and every caller keeps working — the dashboard renders its measured rail
exactly as it does today.

The rule this module enforces
-----------------------------
**Presence supplies availability. It never supplies consent.** An installed lever resolves
to ``off`` unless the developer's own ``~/.ace/config.json`` says otherwise, and ``on``
must be typed per lever. Nothing here defaults a lever to acting on a live session, and no
future default should: the sidecar sits in front of a real coding session, and the cost of
being wrong is a silent corruption three turns later that the developer has no way to
attribute.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from ace.sidecar.levers.protocol import (
    MODE_OFF,
    MODE_SHADOW,
    MODES,
    Lever,
    LeverContext,
    Proposal,
)
from ace.sidecar.levers.types import Session

__all__ = [
    "ENTRY_POINT_GROUP",
    "CONFIG_PATH",
    "RegisteredLever",
    "discover",
    "resolve_modes",
    "load_settings",
    "propose_safely",
]

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "ace.sidecar.levers"

# The same file ``ace.cli`` reads. Kept as a literal rather than imported: ``cli`` builds the
# app and would import this package, and a cycle for one path string is a poor trade.
CONFIG_PATH = os.path.expanduser("~/.ace/config.json")


@dataclass(frozen=True, slots=True)
class RegisteredLever:
    """A discovered lever plus where it came from.

    ``dist`` is carried so the dashboard can say which package supplied a lever. A developer
    seeing an optimization act on their session is entitled to know what installed it.
    """

    lever: Lever
    dist: str = ""

    @property
    def id(self) -> str:
        return getattr(self.lever, "id", "")


def discover(group: str = ENTRY_POINT_GROUP) -> Tuple[RegisteredLever, ...]:
    """Every lever advertised by an installed distribution.

    Each entry point is loaded independently and a broken one is skipped with a log line
    rather than raised: one bad third-party package must not stop ``ace up``. The same
    reasoning applies to an object that loads but does not satisfy :class:`Lever` — it is
    dropped here, where the message can name the entry point, instead of failing later at a
    call site that cannot.
    """
    try:
        from importlib.metadata import entry_points
    except Exception:  # pragma: no cover - importlib.metadata is stdlib on 3.12
        return ()

    found: list[RegisteredLever] = []
    seen: set[str] = set()
    try:
        eps: Iterable[Any] = entry_points(group=group)
    except Exception:
        log.debug("lever entry-point lookup failed", exc_info=True)
        return ()

    for ep in eps:
        try:
            obj = ep.load()
        except Exception:
            log.warning("lever entry point %r failed to load; skipping", ep.name)
            continue
        # Both a class and a ready instance are accepted. A stateless lever is naturally a
        # class; one holding a loaded model artifact is naturally an instance already built
        # by the providing package.
        try:
            candidate = obj() if isinstance(obj, type) else obj
        except Exception:
            log.warning("lever %r failed to instantiate; skipping", ep.name)
            continue
        if not isinstance(candidate, Lever):
            log.warning("lever %r does not satisfy the Lever protocol; skipping", ep.name)
            continue
        lever_id = getattr(candidate, "id", "") or ep.name
        if lever_id in seen:
            log.warning("duplicate lever id %r; keeping the first", lever_id)
            continue
        seen.add(lever_id)
        dist = ""
        try:
            dist = ep.dist.name if ep.dist is not None else ""
        except Exception:
            pass
        found.append(RegisteredLever(lever=candidate, dist=dist))
    return tuple(found)


def _read_config(path: Optional[str] = None) -> Mapping[str, Any]:
    """``~/.ace/config.json``, or an empty mapping. A missing or unreadable file is not an error."""
    try:
        with open(path or CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, Mapping) else {}
    except Exception:
        return {}


def _lever_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = config.get("levers")
    return raw if isinstance(raw, Mapping) else {}


def resolve_modes(
    levers: Iterable[RegisteredLever],
    *,
    config: Optional[Mapping[str, Any]] = None,
    config_path: Optional[str] = None,
) -> Dict[str, str]:
    """``{lever_id: mode}`` for every discovered lever.

    Two spellings are accepted under ``levers`` in the config, because the short one is what
    people actually type::

        {"levers": {"trajectory_compaction": "shadow"}}
        {"levers": {"trajectory_compaction": {"mode": "on", "keep_recent_images": 2}}}

    An unrecognised mode string resolves to ``off``, not to the default. A typo'd ``"On"``
    that silently became ``shadow`` would be tolerable; one that silently became ``on`` would
    not, and the only rule that cannot get that backwards is to refuse the value outright.
    """
    cfg = config if config is not None else _read_config(config_path)
    per_lever = _lever_config(cfg)
    out: Dict[str, str] = {}
    for reg in levers:
        entry = per_lever.get(reg.id)
        if isinstance(entry, Mapping):
            raw = entry.get("mode", MODE_OFF)
        elif isinstance(entry, str):
            raw = entry
        elif entry is True:
            # A bare `true` is an enablement, and the safe reading of "enabled" is the mode
            # that changes nothing about the request.
            raw = MODE_SHADOW
        else:
            raw = MODE_OFF
        mode = str(raw).lower()
        if mode not in MODES:
            log.warning("lever %r has unknown mode %r; treating as off", reg.id, raw)
            mode = MODE_OFF
        out[reg.id] = mode
    return out


def load_settings(
    lever_id: str,
    *,
    config: Optional[Mapping[str, Any]] = None,
    config_path: Optional[str] = None,
) -> Mapping[str, Any]:
    """This lever's own configuration block, minus ``mode``, ready for :class:`LeverContext`."""
    cfg = config if config is not None else _read_config(config_path)
    entry = _lever_config(cfg).get(lever_id)
    if not isinstance(entry, Mapping):
        return {}
    return {k: v for k, v in entry.items() if k != "mode"}


def propose_safely(
    reg: RegisteredLever, session: Session, ctx: LeverContext
) -> Optional[Proposal]:
    """Run one lever, returning ``None`` where it raised.

    The measurement path calls every lever over a developer's entire history, which is the
    widest input any of this code sees and the likeliest place for a third-party lever to
    meet a session shape it did not expect. One such session must cost that lever's row, not
    the dashboard.

    A lever declaring ``requires_content`` is refused outright on a measure-only session
    rather than allowed to raise :class:`~ace.sidecar.levers.types.ContentUnavailable` part
    way through — a partial proposal is worse than none, because the ledger cannot tell it
    from a complete one.
    """
    if getattr(reg.lever, "requires_content", False) and not any(
        call.has_content for _, _, call in session.iter_calls()
    ):
        return None
    try:
        proposal = reg.lever.propose(session, ctx)
    except Exception:
        log.warning("lever %r raised on session %r; skipping", reg.id, session.id, exc_info=True)
        return None
    if not isinstance(proposal, Proposal):
        log.warning("lever %r returned %s; skipping", reg.id, type(proposal))
        return None
    return proposal
