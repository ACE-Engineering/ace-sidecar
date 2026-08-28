"""ace.sidecar.levers.counter — an exact token counter, built from the credential in hand.

Why this module exists at all
-----------------------------
The ledger prices a prompt that was never sent. The baseline side of every counterfactual is
ground truth — the provider's own per-turn counts, read off the transcript — but the proposed
side is text nobody submitted, so its tokens have to be *produced*. Approximating there is
what turns a measured saving back into an estimate, which is the one thing
:mod:`ace.sidecar.levers.ledger` refuses to do. Hence an exact counter, and hence a network
call: for Claude the only exact counter is Anthropic's ``POST /v1/messages/count_tokens``.

Why it takes the credential instead of reading the environment
--------------------------------------------------------------
The previous version sniffed ``ANTHROPIC_API_KEY`` out of ``os.environ``. On the deployment
that matters that variable is empty: the sidecar's own default is ``{"no_key": true}``, and a
Claude Code session on a **subscription** never has an API key at all — it authenticates with
an OAuth token that exists only for the life of a request, in the ``Authorization`` header the
proxy is already relaying.

So the credential is passed in, from whoever has one:

* the proxy turn path hands over the in-flight credential it is about to relay upstream
  (:mod:`ace.gateway.messages`), which is the only path that has one under ``no_key``;
* the dashboard falls back to the environment, for a developer who does export a key.

There is no preflight probe
---------------------------
An earlier plan for this module called for one live call to establish whether a subscription
OAuth token is accepted by the counting endpoint. It isn't needed: the first real count
answers the same question as a side effect, and a dedicated ping only adds a round trip and a
second code path that can disagree with the first.

What *is* needed is that a refusal be remembered and explained. :class:`AnthropicCounter`
latches the first authentication failure, stops calling, and keeps the reason as
:attr:`~AnthropicCounter.note`, so the rail can render ``no_counter — the counting endpoint
rejected this credential (401)`` rather than an unexplained blank. A transport blip is
treated differently and is *not* latched: it costs one edit, not the whole feature.

Presentation is delegated, never re-derived
--------------------------------------------
Building the auth headers here would be a second implementation of a rule this repository
already got wrong once: an OAuth token sent as ``x-api-key`` is rejected, and ``/v1/messages``
additionally requires the ``oauth-2025-04-20`` beta. ``messages_auth.upstream_auth_headers``
owns that rule for the relay, so it owns it here too. The counting endpoint lives under the
same ``/v1/messages`` prefix and takes the same credentials as the route it belongs to.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Mapping, Optional, Tuple

import httpx

from ace.gateway.messages_auth import (
    SCHEME_API_KEY,
    SCHEME_BEARER,
    upstream_auth_headers,
)

__all__ = ["COUNT_TOKENS_PATH", "COUNTABLE_FIELDS", "AnthropicCounter", "resolve_counter"]

log = logging.getLogger(__name__)

# The only request fields ``/v1/messages/count_tokens`` accepts. Everything else on a real
# turn — ``stream``, ``max_tokens``, ``temperature``, ``metadata`` — is rejected as an unknown
# parameter, so a body cannot be forwarded to the counter as-is.
#
# All four listed here contribute tokens and must be kept: dropping ``system`` or ``tools``
# would under-count the prompt by the largest stable part of an agent request. That does not
# matter for a *delta* between two counts taken the same way, but it matters enormously for
# the cross-check against the provider's own reported prompt size.
COUNTABLE_FIELDS = ("model", "messages", "system", "tools", "tool_choice", "thinking")

ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"

# Long enough for a real call, short enough that a hung endpoint cannot stall a dashboard
# render or add itself to a developer's turn latency.
_TIMEOUT_S = 10.0

# A model must be named for the endpoint to answer, and the count is model-family specific.
# Only used when the caller supplies nothing — every real call carries the turn's own model.
_FALLBACK_MODEL = "claude-sonnet-5"

# Statuses that mean "this credential will never work here". Latched. Anything else — a 429,
# a 500, a timeout — is transient and must not disable counting for the whole process.
_FATAL_AUTH_STATUSES = (401, 403)


class AnthropicCounter:
    """Exact token counts from Anthropic, over one credential. Satisfies ``TokenCounter``.

    Callable, and deliberately stateful: the state is the single fact worth remembering
    across calls, which is whether this credential is accepted at all.

    Raises rather than returning a sentinel on failure. The ledger already treats a raising
    counter as "this edit is unmeasurable" and prices nothing for it, which is the correct
    outcome — a counter that returned ``0`` for an uncountable string would silently report
    the entire original as saved.
    """

    __slots__ = ("_credential", "_scheme", "_url", "_client", "_lock", "_dead", "note", "calls")

    def __init__(
        self,
        credential: str,
        scheme: str = SCHEME_API_KEY,
        *,
        base_url: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._credential = credential
        self._scheme = scheme
        self._url = (base_url or ANTHROPIC_DEFAULT_BASE_URL).rstrip("/") + COUNT_TOKENS_PATH
        # Injectable so the suite can drive this exact branch through MockTransport with no
        # live call — the same discipline `install_messages_route` uses for its relay client.
        self._client = client
        self._lock = threading.Lock()
        self._dead: Optional[str] = None
        self.note = "Anthropic /v1/messages/count_tokens"
        self.calls = 0

    @property
    def usable(self) -> bool:
        """False once the credential has been definitively refused."""
        return self._dead is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=_TIMEOUT_S)
        return self._client

    def __call__(self, text: str, *, model: str) -> int:
        """Tokens in one standalone string. The ``TokenCounter`` protocol's shape."""
        return self.count_body(
            {"model": model or _FALLBACK_MODEL,
             "messages": [{"role": "user", "content": text}]}
        )

    def count_body(self, body: Mapping[str, Any]) -> int:
        """Tokens in a whole ``/v1/messages`` request — system prompt and tools included.

        This is what the live shadow path needs. A lever's edit lands inside one tool result
        buried in a long ``messages`` array, and the quantity that matters is what the whole
        prompt would have cost, not what the edited fragment costs on its own: an edit can
        change block boundaries and therefore tokenize differently in place than in isolation.

        Only a *delta* between two bodies counted this way is exact. Comparing one of these
        against the provider's reported ``prompt_tokens`` is a cross-check, not a measurement
        — the two include slightly different scaffolding.
        """
        if self._dead is not None:
            raise RuntimeError(self._dead)

        payload = {k: body[k] for k in COUNTABLE_FIELDS if k in body}
        payload.setdefault("model", _FALLBACK_MODEL)

        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        # The OAuth beta is merged in here exactly as the relay does it; presenting a
        # subscription token without it is the failure this indirection exists to avoid.
        headers.update(upstream_auth_headers(self._credential, self._scheme))

        resp = self._http().post(
            self._url,
            json=payload,
            headers=headers,
            timeout=_TIMEOUT_S,
        )

        if resp.status_code in _FATAL_AUTH_STATUSES:
            # Latch, and say which credential shape was refused. This is the line that turns
            # "the dashboard shows nothing" into an answerable question, and it is the one
            # place the OAuth-vs-API-key outcome is actually established.
            kind = "OAuth token" if self._scheme == SCHEME_BEARER else "API key"
            with self._lock:
                self._dead = (
                    f"the counting endpoint rejected this {kind} ({resp.status_code}) — "
                    f"exact counts need a credential it accepts"
                )
                self.note = self._dead
            log.warning("[levers] %s", self._dead)
            raise RuntimeError(self._dead)

        # Not latched: a rate limit or a 5xx says nothing about the credential, and disabling
        # measurement for the process because one call was throttled would be a bug that
        # looks exactly like the feature not working.
        resp.raise_for_status()

        n = int((resp.json() or {}).get("input_tokens", -1))
        if n < 0:
            raise RuntimeError("counting endpoint returned no input_tokens")
        with self._lock:
            self.calls += 1
        return n


def resolve_counter(
    credential: Optional[str] = None,
    scheme: str = SCHEME_API_KEY,
    *,
    base_url: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Tuple[Optional[AnthropicCounter], str]:
    """An exact counter and where its credential came from, or ``(None, reason)``.

    Exactness is the whole requirement, so only the model vendor's own counter is offered.
    ``tiktoken`` is deliberately not a fallback: it is OpenAI's BPE and merely a *proxy* for
    anything else, and a proxy here turns a measured saving into an estimate wearing a dollar
    sign. Neither is ``bytes / 4`` — see ``strategies.BYTES_PER_TOKEN``, where the real
    measured ratio on agent tool output is closer to 2.8 and the 4.0 everyone reaches for sits
    at the 99th percentile of the distribution.

    Returning ``None`` is an ordinary outcome and not a failure. It costs the live column and
    leaves the simulated headroom rail exactly as it is.
    """
    if not credential:
        # No in-flight credential: the dashboard path, rendered outside any request. An
        # exported key is the only thing that can serve it.
        env_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        env_tok = (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip()
        if env_key:
            credential, scheme = env_key, SCHEME_API_KEY
        elif env_tok:
            credential, scheme = env_tok, SCHEME_BEARER
        else:
            return None, (
                "no credential available — this sidecar runs on `no_key: true`, so exact "
                "counts come from the token a proxied turn relays, or from an exported "
                "ANTHROPIC_API_KEY"
            )

    counter = AnthropicCounter(credential, scheme, base_url=base_url, client=client)
    kind = "relayed OAuth token" if scheme == SCHEME_BEARER else "API key"
    return counter, f"Anthropic /v1/messages/count_tokens via {kind}"
