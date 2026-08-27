"""ace.sidecar.levers — the public contract optimization modules are written against.

This package contains no optimizations. It defines the normalized session model every lever
reads (:mod:`~ace.sidecar.levers.types`), what a lever is allowed to do
(:mod:`~ace.sidecar.levers.protocol`), and how installed ones are found
(:mod:`~ace.sidecar.levers.registry`). Implementations ship separately and register through
the ``ace.sidecar.levers`` entry-point group.

Three properties are worth stating once, because everything here follows from them.

**One lever, every agent.** Levers read the corpus shape that ``insights._scan``,
``_scan_antigravity`` and ``_scan_codex`` already agree on, never a provider's wire format.
Supporting a fourth coding agent is a scanner, not a lever rewrite.

**Measurement is universal; actuation is not.** Scoring runs off transcripts, so it works
for every agent the sidecar can read, with no proxy and no hooks. Rewriting bytes needs a
write path, and only some agents have one. ``Lever.requires_content`` is where a lever
declares which half it needs, and the registry refuses the mismatch rather than degrading.

**Levers propose; the ledger prices.** No lever returns a dollar figure. Pricing happens
once, in :mod:`~ace.sidecar.levers.ledger`, where the tokenizer and the rate catalog live —
which is what keeps a saving auditable and keeps provider-specific cache economics out of a
lever that is meant to be provider-neutral. The ledger prices nothing it cannot count
exactly, and it ranks levers rather than totalling them.
"""

from ace.sidecar.levers.ledger import (
    FIDELITY_MEASURED,
    FIDELITY_UNMEASURABLE,
    FIDELITY_UNPRICED,
    EditCost,
    LedgerEntry,
    LedgerReport,
    price_all,
    price_proposal,
)
from ace.sidecar.levers.protocol import (
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    MODES,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_NONE,
    Edit,
    EditKind,
    Lever,
    LeverContext,
    Proposal,
    TokenCounter,
)
from ace.sidecar.levers.registry import (
    CONFIG_PATH,
    ENTRY_POINT_GROUP,
    RegisteredLever,
    discover,
    load_settings,
    propose_safely,
    resolve_modes,
)
from ace.sidecar.levers.types import (
    ContentRef,
    ContentUnavailable,
    Session,
    ToolCall,
    Turn,
    Usage,
    from_corpus_session,
    from_corpus_sessions,
)

__all__ = [
    # types
    "Session",
    "Turn",
    "ToolCall",
    "Usage",
    "ContentRef",
    "ContentUnavailable",
    "from_corpus_session",
    "from_corpus_sessions",
    # protocol
    "Lever",
    "LeverContext",
    "Proposal",
    "Edit",
    "EditKind",
    "TokenCounter",
    "MODE_OFF",
    "MODE_SHADOW",
    "MODE_ON",
    "MODES",
    "RISK_NONE",
    "RISK_LOW",
    "RISK_MEDIUM",
    "RISK_HIGH",
    # registry
    "discover",
    "resolve_modes",
    "load_settings",
    "propose_safely",
    "RegisteredLever",
    "ENTRY_POINT_GROUP",
    "CONFIG_PATH",
    # ledger
    "price_proposal",
    "price_all",
    "LedgerEntry",
    "LedgerReport",
    "EditCost",
    "FIDELITY_MEASURED",
    "FIDELITY_UNMEASURABLE",
    "FIDELITY_UNPRICED",
]
