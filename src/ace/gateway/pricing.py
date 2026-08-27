"""ace.gateway.pricing — what one Anthropic turn cost, with the cache tiers priced separately.

**Phase 0 / P0-6.** Turns a turn's token counts into dollars for the ``/v1/messages`` path.

Why this is its own module rather than the proxy's ``price_table``
------------------------------------------------------------------
The gateway's existing price table is a flat ``model -> (in_per_1k, out_per_1k)`` pair. That
shape structurally cannot express what coding traffic costs, because an Anthropic turn bills
input at **three different rates at once**:

* **fresh input** — full rate
* **cache read** — ~0.1x (``cached_in`` in the market catalog)
* **cache write** — 1.25x at the 5-minute TTL, 2x at 1 hour

An agentic coding loop resends a large stable prefix every turn, so on a warm session the
*majority* of input tokens bill at the 0.1x rate. Collapsing that into one input price
overstates spend severalfold and — worse for Phase 1 — makes the cache saving invisible,
which is the single number the whole coding thesis rests on (see
``docs/plan_docs/2026-07-27-coding-optimization-layer-phase0.md`` section 2.1).

Where the rates come from
-------------------------
``data/model_market/models/<id>.yaml`` is the single source of truth: each Anthropic offering
declares ``in`` / ``out`` / ``cached_in`` in USD per 1M tokens, vendor-confirmed and dated.
This module reads that catalog rather than carrying a second hardcoded table that would
silently drift from it.

**Cache writes are the one rate the catalog does not carry**, because they are not an
independent price — they are a fixed multiple of the input rate, set by the TTL you asked
for. So they are derived here rather than added as a catalog field that could disagree with
``in``.

Reading the ledger honestly
---------------------------
``input_tokens`` from Anthropic **already excludes** both cached buckets — it is the fresh
remainder, not the total. Total prompt size is ``input + cache_read + cache_write``. Summing
naively double-counts; subtracting to "correct" it under-reports. Both mistakes are easy and
neither is visible in the output, which is why :class:`CostBreakdown` keeps the three input
buckets separate all the way to the caller.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

log = logging.getLogger("ace.gateway.pricing")

# Anthropic's cache-write premium, as a multiple of the model's fresh-input rate. Fixed by
# the requested TTL, identical across models — which is exactly why it is derived from `in`
# here rather than duplicated per-model in the catalog.
CACHE_WRITE_MULTIPLIER_5M = 1.25
CACHE_WRITE_MULTIPLIER_1H = 2.0


def _resolve_catalog_root() -> str:
    """Locate ``data/model_market``, working from an installed package as well as a checkout.

    This used to be the bare relative string ``"data/model_market"``, which resolves against
    the **current working directory** — so it only found the catalog when the process happened
    to be launched from the repo root. Every other cwd (which is to say: every real use of an
    installed ``ace up``, since a developer runs it from their own project) silently found no
    rates and recorded every turn as UNPRICED at $0.00. A cost tool that reports zero is worse
    than one that fails, so the path is now derived from this module's own location.

    Order matters. The checkout copy wins over the packaged one because in a source tree the
    catalog is the thing being edited, and an editable install must not serve a stale build
    artifact instead of the file the developer just changed.
    """
    env = os.environ.get("ACE_MODEL_MARKET_ROOT")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))  # src/ace/gateway
    candidates = (
        # repo checkout: src/ace/gateway -> up three -> <repo>/data/model_market
        os.path.join(here, os.pardir, os.pardir, os.pardir, "data", "model_market"),
        # wheel: catalog shipped as package data at ace/data/model_market
        os.path.join(here, os.pardir, "data", "model_market"),
        # legacy cwd-relative, kept last so an unusual layout still has a chance
        "data/model_market",
    )
    for cand in candidates:
        if os.path.isdir(os.path.join(cand, "models")):
            return os.path.normpath(cand)
    return "data/model_market"


_CATALOG_ROOT = _resolve_catalog_root()


def catalog_root() -> str:
    """The resolved ``data/model_market`` root.

    Exposed so that catalog-adjacent data — ``feeds/`` in particular, see
    :mod:`ace.gateway.llm_router.feeds` — is found by the *same* order documented in
    :func:`_resolve_catalog_root` rather than by a second copy of it that drifts. Returns the
    value resolved once at import; the resolution is a filesystem probe and the answer cannot
    change under a running process.
    """
    return _CATALOG_ROOT


# Claude Code's placeholder model id for messages it generates locally without calling the
# API. Not a provider model, so it has no catalog entry and never will — warning about it
# trains the reader to ignore a warning that is load-bearing for real models.
_NON_PROVIDER_MODELS = frozenset({"<synthetic>"})

# model id -> Rates (or None when the catalog has no Anthropic offering for it). Negative
# results are cached too: an unpriced model must not re-read and re-parse a missing file on
# every single turn of an agentic loop.
_RATE_CACHE: Dict[str, Optional["Rates"]] = {}


@dataclass(frozen=True)
class Rates:
    """Per-1M-token USD rates for one model, as declared by the market catalog."""

    model: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    # Where this rate came from and when it was last checked against the vendor. Carried so
    # a figure built on it can cite it: a dashboard that shows a dollar number and cannot say
    # which price list produced it is asking to be trusted rather than checked.
    source: str = ""
    as_of: str = ""

    def cache_write_per_mtok(self, ttl: str = "5m") -> float:
        """Cache-write rate for a TTL. Derived from the input rate — see module docstring."""
        mult = CACHE_WRITE_MULTIPLIER_1H if ttl == "1h" else CACHE_WRITE_MULTIPLIER_5M
        return self.input_per_mtok * mult


@dataclass(frozen=True)
class CostBreakdown:
    """One turn's cost, with the three input buckets kept apart.

    They are reported separately rather than summed because the *split* is the product
    signal: ``cache_read_usd`` against ``cache_read_if_uncached_usd`` is the money prompt
    caching saved on this turn, and that comparison is unrecoverable from a single total.
    """

    model: str
    input_usd: float = 0.0
    output_usd: float = 0.0
    cache_read_usd: float = 0.0
    cache_write_usd: float = 0.0
    # Counterfactual: what the cache-read tokens WOULD have cost at the fresh-input rate.
    # The saving is this minus cache_read_usd.
    cache_read_if_uncached_usd: float = 0.0
    # False when the model has no catalog entry: every figure above is 0.0 and must be
    # reported as "unpriced", never as "free". A silent zero looks like a cost win.
    priced: bool = True

    @property
    def total_usd(self) -> float:
        return (
            self.input_usd
            + self.output_usd
            + self.cache_read_usd
            + self.cache_write_usd
        )

    @property
    def cache_savings_usd(self) -> float:
        """Money prompt caching saved on this turn (>= 0)."""
        return max(0.0, self.cache_read_if_uncached_usd - self.cache_read_usd)


def _load_rates(model: str) -> Optional[Rates]:
    """Read one model's Anthropic offering out of the market catalog.

    Deliberately does NOT go through ``ModelMarket.load()``: that reads every model,
    provider, and prior in the catalog and requires the full directory layout. The P0-5 local
    sidecar needs one model's price, not the routing market — so this reads the single file
    and stays independent of the router's presence.
    """
    candidates = [
        model,
        model.replace(".", "-"),
        model.replace("-", "."),
    ]
    path = None
    for cand in candidates:
        p = os.path.join(_CATALOG_ROOT, "models", f"{cand}.yaml")
        if os.path.exists(p):
            path = p
            break

    if not path or not os.path.exists(path):
        if "gemini" in model.lower():
            # Fallback rates for Gemini models (per 1M tokens)
            is_pro = "pro" in model.lower()
            p_in = 1.25 if is_pro else 1.50 if "3.6" in model or "3-6" in model else 0.075
            p_out = 5.00 if is_pro else 7.50 if "3.6" in model or "3-6" in model else 0.30
            return Rates(
                model=model,
                input_per_mtok=p_in,
                output_per_mtok=p_out,
                cache_read_per_mtok=p_in * 0.1,
                source="google_default",
                as_of="2026-08-01",
            )
        return None
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            spec = yaml.safe_load(fh) or {}
        # First pass: try to find an anthropic or google offering
        for offering in spec.get("offerings") or ():
            provider = offering.get("provider")
            p = offering.get("pricing") or {}
            if p.get("kind") != "per_token":
                continue
            if p.get("unit", "usd_per_1m") != "usd_per_1m":
                log.warning(
                    "[pricing] %s: unsupported price unit %r", model, p.get("unit")
                )
                continue
            p_in = float(p.get("in", 0))
            return Rates(
                model=model,
                input_per_mtok=p_in,
                output_per_mtok=float(p.get("out", 0)),
                cache_read_per_mtok=float(
                    p.get(
                        "cached_in",
                        p_in * 0.25
                        if "gemini" in model.lower()
                        else p_in * 0.1
                        if ("openai" in str(provider or "").lower() or "codex" in model.lower())
                        else p_in,
                    )
                ),
                source=str(offering.get("source") or provider or ""),
                as_of=str(offering.get("as_of") or ""),
            )
    except Exception:  # pragma: no cover - a malformed catalog must not break serving
        log.warning("[pricing] failed to read rates for %s", model, exc_info=True)

    if "gemini" in model.lower():
        # Fallback rates for Gemini models (per 1M tokens)
        is_pro = "pro" in model.lower()
        p_in = 1.25 if is_pro else 0.075
        p_out = 5.00 if is_pro else 0.30
        return Rates(
            model=model,
            input_per_mtok=p_in,
            output_per_mtok=p_out,
            cache_read_per_mtok=p_in * 0.25,
            source="google_default",
            as_of="2026-08-01",
        )
    if "codex" in model.lower() or "gpt-5" in model.lower():
        # Fallback rates for Codex/GPT-5 models (per 1M tokens)
        is_mini = "mini" in model.lower() or "nano" in model.lower()
        p_in = 0.25 if is_mini else 1.75
        p_out = 2.00 if is_mini else 14.00
        return Rates(
            model=model,
            input_per_mtok=p_in,
            output_per_mtok=p_out,
            cache_read_per_mtok=p_in * 0.1,
            source="openai_default",
            as_of="2026-07-23",
        )
    return None


def rates_for(model: str) -> Optional[Rates]:
    """Rates for a model, or None when the catalog has no Anthropic offering for it."""
    if not model:
        return None
    if model not in _RATE_CACHE:
        _RATE_CACHE[model] = _load_rates(model)
        if _RATE_CACHE[model] is None and model not in _NON_PROVIDER_MODELS:
            # Loud once per model, not once per request — an unpriced model on a coding
            # workload means the dashboard is silently under-reporting real spend.
            log.warning(
                "[pricing] no Anthropic offering for %r in %s — turns on this model will be "
                "recorded as UNPRICED (cost 0.0), not as free",
                model,
                _CATALOG_ROOT,
            )
    return _RATE_CACHE[model]


def reset_cache() -> None:
    """Drop the memoized rates. For tests and for a catalog reload."""
    _RATE_CACHE.clear()


def cost_for(
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> CostBreakdown:
    """Price one turn.

    ``input_tokens`` is Anthropic's own field, which already **excludes** the cached buckets
    (see module docstring) — pass it through unmodified; do not subtract the cache counts.
    """
    rates = rates_for(model)
    if rates is None:
        return CostBreakdown(model=model, priced=False)

    m = 1e6
    return CostBreakdown(
        model=model,
        input_usd=input_tokens / m * rates.input_per_mtok,
        output_usd=output_tokens / m * rates.output_per_mtok,
        cache_read_usd=cache_read_tokens / m * rates.cache_read_per_mtok,
        cache_write_usd=(
            cache_write_5m_tokens / m * rates.cache_write_per_mtok("5m")
            + cache_write_1h_tokens / m * rates.cache_write_per_mtok("1h")
        ),
        cache_read_if_uncached_usd=cache_read_tokens / m * rates.input_per_mtok,
    )
