"""ace.gateway.messages — Anthropic-native ``POST /v1/messages``, byte-faithful.

**Phase 0 / P0-1.** The gateway's second inference route, and the first one a coding agent
(Claude Code) can actually talk to: it speaks Anthropic's Messages schema natively and
relays to an Anthropic upstream **without translating anything**.

Why this is a separate module and not another branch of ``proxy.py``
--------------------------------------------------------------------
``proxy.py``'s ``_direct_provider_passthrough`` already relays to Anthropic — but it
``resp.json()``-parses, translates through the OpenAI shape, and re-serializes. That is
exactly what this path must NOT do, for two reasons that are specific to coding traffic
(see ``docs/plan_docs/2026-07-27-coding-optimization-layer-phase0.md`` §2.1 and §3.3):

* **Prompt-cache economics.** Anthropic cache reads cost ~0.1×, and an agentic loop resends
  a large stable prefix every turn. Re-serializing a body — even losslessly — can reorder
  keys or change whitespace, which changes the prefix and **busts the cache**. A mutation
  that trims tokens but invalidates a 0.1× cache read makes the request several times more
  expensive, not cheaper.
* **Thinking-block signatures.** Extended-thinking blocks carry a signature verified against
  the original bytes. Any dict round-trip re-serializes them and upstream rejects the turn.

Hence the rule this module exists to enforce:

    **Parse to decide, never parse to forward.**

The request body is forwarded as the exact bytes we received. It IS parsed — but only a
throwaway copy, and only to answer two questions (is this streaming? which model?). The
parsed form is never what gets sent.

Scope (deliberately narrow)
---------------------------
Every ACE lever is off on this path: no semantic cache, no prompt compression, no PII
redaction, no model routing, no merit-order dispatch. Phase 0 is a *measurement* release —
it earns the right to mutate later by first proving it can relay without mutating.

Streaming (P0-2) relays incrementally and observes usage on the way past — see
:func:`_relay_stream` and :class:`_UsageTee`. The tee exists because Anthropic reports usage
*mid-stream* rather than in a final chunk, so accounting cannot wait for a finished body.

Not here yet, by design:

* **auth / key resolution** — P0-3 owns the real story (loopback-trust for the local
  sidecar, ACE dev keys for cloud). What is here is the minimum to move bytes.
* **usage persistence** — P0-6. Usage is now *recovered* and handed to an optional
  ``on_usage`` sink, but nothing writes it to the telemetry spine yet.

No live provider call is made anywhere in this module's tests — the network hop is driven
with ``httpx.MockTransport`` (see ``tests/test_messages_passthrough.py``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple
from uuid import uuid4

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ace.gateway.messages_auth import (
    MODE_LOOPBACK,
    AuthConfig,
    authenticate,
    upstream_auth_headers,
)
from ace.gateway.pricing import CostBreakdown, cost_for

log = logging.getLogger("ace.gateway.messages")

ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
MESSAGES_PATH = "/v1/messages"

# Request headers forwarded verbatim to Anthropic. An allowlist rather than a denylist:
# a header we have not thought about should not reach the provider by default.
#
# `anthropic-beta` matters more than it looks — Claude Code negotiates features through it
# (fine-grained tool streaming, context management, and whatever ships next). Dropping it
# silently downgrades the request rather than failing it, which is the worst failure mode:
# the session still works, just worse and more expensive.
_FORWARD_REQUEST_HEADERS = (
    "anthropic-version",
    "anthropic-beta",
    "content-type",
)

# Response headers relayed back to the caller.
#
# Deliberately EXCLUDES content-length and content-encoding: httpx has already decoded the
# body by the time we see `.content`, so relaying the upstream's framing headers would
# describe bytes we are not sending. Starlette recomputes both.
_RELAY_RESPONSE_HEADERS = (
    "content-type",
    "request-id",
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-tokens-reset",
    "retry-after",
)

# Marks which ACE path served a response. Phase 0 ships no levers, so this always reports
# the passthrough — it exists so the P0-4 conformance suite can assert the fidelity path was
# actually taken, rather than inferring it from a body that happens to look right.
_PATH_MARKER_HEADER = "x-ace-path"
_PATH_MARKER = "messages-passthrough"

# The telemetry `surface` value for this route. Underscored, unlike the hyphenated header
# marker above — it sits in a column beside `openai_shim` / `anthropic_shim` / `azure_shim`
# and has to be spelled like them for a group-by to read naturally. Same string this row's
# `tier` has always carried.
#
# Duplicated from `execute_api.SURFACE_MESSAGES_PASSTHROUGH` on purpose: importing that module
# here pulls in `ace.model` and breaks the standalone-sidecar boundary that lets P0-5 mount
# this route without the fleet engine (tests/test_sidecar_standalone_boundary.py fails on
# exactly that). The copy is pinned by tests/test_telemetry_surface.py, which asserts this row
# carries the value execute_api publishes — so the two cannot drift silently.
_SURFACE = "messages_passthrough"


@dataclass(frozen=True)
class MessagesConfig:
    """Wiring for the Anthropic-native relay.

    Transport only. Credentials moved to :class:`ace.gateway.messages_auth.AuthConfig` in
    P0-3 — two configs each owning one concern, rather than a provider key reachable from
    two places that can disagree.
    """

    base_url: str = ANTHROPIC_DEFAULT_BASE_URL
    # Coding agents run long turns (extended thinking + multi-tool). The gateway's 60s
    # passthrough timeout is far too aggressive here; a cut-off turn costs the full input
    # spend and returns nothing.
    timeout_s: float = 600.0

    @classmethod
    def from_env(cls) -> "MessagesConfig":
        return cls(
            base_url=(
                os.environ.get("ACE_ANTHROPIC_BASE_URL", "").strip()
                or ANTHROPIC_DEFAULT_BASE_URL
            ),
            timeout_s=float(os.environ.get("ACE_MESSAGES_TIMEOUT_S", "600")),
        )


def _error(status: int, err_type: str, message: str) -> JSONResponse:
    """An Anthropic-shaped error envelope.

    Clients parse provider errors structurally. Emitting FastAPI's ``{"detail": ...}`` here
    would make an ACE-originated failure unreadable to a client that only knows Anthropic's
    shape, so our own errors wear the same envelope the upstream's do.
    """
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": err_type, "message": message}},
        headers={_PATH_MARKER_HEADER: _PATH_MARKER},
    )


def _peek(raw: bytes) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse a THROWAWAY copy of the body, to decide only. Never used for forwarding.

    Returns ``(parsed, error_message)``. The parsed dict answers "is this streaming?" and
    "which model?"; ``raw`` remains the single source of truth for what goes on the wire.
    """
    try:
        parsed = json.loads(raw)
    except Exception as e:
        return None, f"request body is not valid JSON: {e}"
    if not isinstance(parsed, dict):
        return None, "request body must be a JSON object"
    return parsed, None


@dataclass
class StreamUsage:
    """Token usage recovered from a streamed turn, for P0-6 accounting.

    Anthropic reports usage across TWO events rather than one final chunk:
    ``message_start`` carries the input side (including the cache counters),
    ``message_delta`` carries the running output count. Neither alone is the answer, which
    is why a streamed turn needs a stateful observer rather than a look at the last chunk.

    The cache fields are the point. ``cache_read_input_tokens`` billed at ~0.1× is the
    signal Phase 1 exists to measure and Phase 2's flagship lever exists to protect — a
    release that streams but cannot see its own cache hit rate would be measurement
    theatre. (``anthropic_adapter`` drops exactly these two fields; see module docstring.)
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    # Per-TTL split of cache_creation_input_tokens, present only when the turn used a
    # non-default TTL (Anthropic reports it under `usage.cache_creation`). The two TTLs bill
    # at different multiples of the input rate — 1.25x at 5m, 2x at 1h — so a turn that
    # wrote a 1h entry costs 60% more to cache than the flat total suggests.
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    # Set when the stream carried a mid-stream `error` event. An upstream that fails after
    # `message_start` returns HTTP 200 and then errors inside the body, so status code alone
    # would book a failed turn as a success.
    error_type: Optional[str] = None
    # Which wire path produced this. Carried so accounting and capture can distinguish a
    # streamed turn from a buffered one without re-deriving it.
    streamed: bool = False

    @property
    def billable_input_tokens(self) -> int:
        """Fresh input only — what is charged at the full rate.

        Anthropic's ``input_tokens`` already EXCLUDES cached reads, so this is deliberately
        not a subtraction. Named explicitly because "input_tokens minus cache_read" is the
        obvious-looking mistake, and it under-reports spend.
        """
        return self.input_tokens

    @property
    def total_prompt_tokens(self) -> int:
        """Every input token the turn carried, cached or not.

        The number to compare against a context-window budget — ``input_tokens`` alone is
        just the fresh remainder and on a warm agentic session is a small fraction of what
        was actually sent.
        """
        return (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def split_cache_writes(self) -> Tuple[int, int]:
        """``(5m_tokens, 1h_tokens)`` for pricing.

        Anthropic only emits the per-TTL breakdown when a non-default TTL is in play, so an
        absent split means the whole write was at the 5-minute default — attribute it there
        rather than dropping it, or a cached turn prices as if it never wrote.
        """
        if self.cache_creation_5m_tokens or self.cache_creation_1h_tokens:
            return self.cache_creation_5m_tokens, self.cache_creation_1h_tokens
        return self.cache_creation_input_tokens, 0

    def cost(self) -> "CostBreakdown":
        """Price this turn from the market catalog. See :mod:`ace.gateway.pricing`.

        ``input_tokens`` is passed through UNMODIFIED. Anthropic already excludes the cached
        buckets from it, so "correcting" it by subtracting cache_read under-reports spend —
        an easy bug with no visible symptom. Pinned by
        ``test_stream_usage_cost_does_not_subtract_cache_from_input``.
        """
        write_5m, write_1h = self.split_cache_writes()
        return cost_for(
            model=self.model or "",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_input_tokens,
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
        )


def usage_from_payload(payload: Dict[str, Any]) -> StreamUsage:
    """Recover usage from a NON-streaming Messages response body.

    The streaming path has :class:`_UsageTee`; this is its counterpart, so both paths feed
    accounting through one shape and P0-6 does not have two divergent notions of what a turn
    cost. Best-effort and never raises, for the same reason the tee never raises.
    """
    usage = StreamUsage()
    try:
        usage.model = payload.get("model")
        usage.stop_reason = payload.get("stop_reason")
        u = payload.get("usage") or {}
        usage.input_tokens = int(u.get("input_tokens") or 0)
        usage.output_tokens = int(u.get("output_tokens") or 0)
        usage.cache_read_input_tokens = int(u.get("cache_read_input_tokens") or 0)
        usage.cache_creation_input_tokens = int(
            u.get("cache_creation_input_tokens") or 0
        )
        _apply_cache_creation_split(usage, u)
        if payload.get("type") == "error":
            usage.error_type = (payload.get("error") or {}).get(
                "type"
            ) or "unknown_error"
    except (
        Exception
    ):  # pragma: no cover - defensive; accounting never breaks a response
        log.debug("[messages] failed to read usage from payload", exc_info=True)
    return usage


def _apply_cache_creation_split(usage: StreamUsage, u: Dict[str, Any]) -> None:
    """Pull the per-TTL cache-write breakdown out of ``usage.cache_creation`` if present."""
    creation = u.get("cache_creation")
    if isinstance(creation, dict):
        usage.cache_creation_5m_tokens = int(
            creation.get("ephemeral_5m_input_tokens") or 0
        )
        usage.cache_creation_1h_tokens = int(
            creation.get("ephemeral_1h_input_tokens") or 0
        )


class _UsageTee:
    """Extracts usage from an SSE stream **without touching the bytes being relayed**.

    The rule from the module docstring applies with more force here than anywhere else:
    *parse to decide, never parse to forward*. This class only ever sees a copy. It has no
    way to influence the relayed stream — ``feed()`` returns nothing — which is the property
    that makes it safe by construction rather than by careful coding.

    It must also never raise. Fidelity outranks accounting: a malformed or unrecognized
    event should cost us a usage number, never the developer's turn. Every parse is
    best-effort and every failure is swallowed.

    Chunk boundaries do not align with event boundaries, so partial events are buffered
    until the ``\\n\\n`` terminator arrives.
    """

    # An SSE event that never terminates would otherwise grow this buffer without bound.
    # Anthropic events are small (a tool-use block is the largest realistic case); 1 MiB is
    # far above that and far below anything that threatens the process.
    _MAX_BUFFER = 1024 * 1024

    def __init__(self) -> None:
        self.usage = StreamUsage()
        self._buf = b""
        self._overflowed = False

    def feed(self, chunk: bytes) -> None:
        """Observe a relayed chunk. Returns nothing, by design."""
        try:
            self._buf += chunk
            if len(self._buf) > self._MAX_BUFFER:
                # Give up on accounting rather than on the stream. The relay is unaffected.
                self._buf = b""
                self._overflowed = True
                return
            while b"\n\n" in self._buf:
                event, self._buf = self._buf.split(b"\n\n", 1)
                self._handle_event(event)
        except (
            Exception
        ):  # pragma: no cover - defensive: accounting must never break relay
            log.debug("[messages] usage tee failed on a chunk", exc_info=True)

    def _handle_event(self, event: bytes) -> None:
        for line in event.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            try:
                payload = json.loads(line[5:].strip())
            except Exception:
                continue  # a `ping` or any non-JSON data line — not an error
            if isinstance(payload, dict):
                self._apply(payload)

    def _apply(self, payload: Dict[str, Any]) -> None:
        etype = payload.get("type")
        if etype == "message_start":
            msg = payload.get("message") or {}
            self.usage.model = msg.get("model") or self.usage.model
            u = msg.get("usage") or {}
            self.usage.input_tokens = int(u.get("input_tokens") or 0)
            self.usage.cache_read_input_tokens = int(
                u.get("cache_read_input_tokens") or 0
            )
            self.usage.cache_creation_input_tokens = int(
                u.get("cache_creation_input_tokens") or 0
            )
            _apply_cache_creation_split(self.usage, u)
            # message_start can already carry output_tokens; message_delta supersedes it.
            self.usage.output_tokens = int(u.get("output_tokens") or 0)
        elif etype == "message_delta":
            u = payload.get("usage") or {}
            if "output_tokens" in u:
                # Cumulative, not incremental — assign rather than accumulate.
                self.usage.output_tokens = int(u.get("output_tokens") or 0)
            delta = payload.get("delta") or {}
            if delta.get("stop_reason"):
                self.usage.stop_reason = delta["stop_reason"]
        elif etype == "error":
            err = payload.get("error") or {}
            self.usage.error_type = err.get("type") or "unknown_error"


def _report_usage(
    usage: StreamUsage,
    on_usage: Optional[Callable[[StreamUsage], None]],
    *,
    streamed: bool,
) -> None:
    """Log one turn's cost and hand it to the accounting sink. Never raises.

    The log line reports the cache saving explicitly rather than leaving it to be derived:
    ``cache_read`` tokens billed at ~0.1x are the number Phase 1 exists to measure, and an
    operator watching the log should be able to see the lever working without joining
    against a price table.
    """
    usage.streamed = streamed
    cost = usage.cost()
    log.info(
        "[messages] %s model=%s in=%d out=%d cache_read=%d cache_write=%d stop=%s %s%s",
        "stream" if streamed else "turn",
        usage.model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens,
        usage.cache_creation_input_tokens,
        usage.stop_reason,
        (
            f"cost=${cost.total_usd:.6f} saved=${cost.cache_savings_usd:.6f}"
            if cost.priced
            else "cost=UNPRICED"
        ),
        f" error={usage.error_type}" if usage.error_type else "",
    )
    if on_usage is not None:
        try:
            on_usage(usage)
        except Exception:  # pragma: no cover - a sink must never break the response
            log.debug("[messages] on_usage sink raised", exc_info=True)


async def _relay_stream(
    *,
    client: httpx.AsyncClient,
    url: str,
    raw: bytes,
    headers: Dict[str, str],
    timeout_s: float,
    model_hint: Optional[str],
    on_usage: Optional[Callable[[StreamUsage], None]],
) -> Response:
    """Relay an Anthropic SSE stream incrementally, observing usage on the way past.

    Two things make this harder than the non-streaming path, and both are why the P0-1
    buffer-then-re-emit fallback was rejected rather than reused:

    * **The stream must stay incremental.** Claude Code renders tool calls as
      ``input_json_delta`` fragments arrive. Buffering the whole response and re-emitting it
      produces a byte-identical result that is still *wrong* — the agent sits blank and then
      jumps, and long turns look like hangs.
    * **Usage is only visible mid-stream** (see :class:`StreamUsage`), so accounting has to
      ride along with the relay rather than inspect a finished body.

    The upstream connection is opened INSIDE the response generator, so it is bound to the
    generator's lifetime: if the client disconnects, Starlette closes the generator, and
    ``async with`` tears the upstream connection down with it. Opening it outside and
    handing the response object to a generator is the shape that leaks sockets on disconnect.
    """
    tee = _UsageTee()

    # Upstream failures on a streaming request are NOT SSE — they are a normal JSON error
    # body. We therefore have to discover the status before committing to a StreamingResponse,
    # because a 200 SSE response and a 429 JSON response need different shapes downstream.
    # `first` holds whatever we had to consume to learn that.
    try:
        ctx = client.stream(
            "POST",
            url,
            content=raw,  # NOT json= — see module docstring.
            headers=headers,
            timeout=timeout_s,
        )
        upstream = await ctx.__aenter__()
    except httpx.TimeoutException:
        return _error(
            504, "timeout_error", f"upstream timed out after {timeout_s:.0f}s"
        )
    except httpx.HTTPError as e:
        return _error(502, "api_error", f"upstream request failed: {e}")

    if upstream.status_code >= 400:
        try:
            body = await upstream.aread()
        finally:
            await ctx.__aexit__(None, None, None)
        out = {
            k: upstream.headers[k]
            for k in _RELAY_RESPONSE_HEADERS
            if k in upstream.headers
        }
        out[_PATH_MARKER_HEADER] = _PATH_MARKER
        log.info(
            "[messages] stream model=%s status=%d (error before stream)",
            model_hint,
            upstream.status_code,
        )
        return Response(
            content=body,
            status_code=upstream.status_code,
            headers=out,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    async def _body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                if not chunk:
                    continue
                # Observe a COPY, then yield the original untouched. The tee cannot alter
                # `chunk` — feed() returns nothing — so fidelity holds by construction.
                tee.feed(chunk)
                yield chunk
        finally:
            # Runs on normal completion AND on client disconnect (GeneratorExit), so the
            # upstream socket is always released and usage is always reported — a turn the
            # developer cancelled still cost real input tokens.
            await ctx.__aexit__(None, None, None)
            u = tee.usage
            if u.model is None:
                u.model = model_hint
            _report_usage(u, on_usage, streamed=True)

    out = {
        k: upstream.headers[k] for k in _RELAY_RESPONSE_HEADERS if k in upstream.headers
    }
    out[_PATH_MARKER_HEADER] = _PATH_MARKER
    # Proxies and browsers buffer text/event-stream by default, which would defeat the whole
    # point of relaying incrementally.
    out["cache-control"] = "no-cache"
    out["x-accel-buffering"] = "no"
    return StreamingResponse(
        _body(),
        status_code=upstream.status_code,
        headers=out,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


class _LocalRequestLog:
    """The telemetry row shape, for a sidecar that has no telemetry package.

    ``ace.observability.telemetry.RequestLog`` is the cloud gateway's row and does not exist
    in this distribution — the sidecar was extracted from that tree without it. The import
    was unconditional, it raised ``ModuleNotFoundError`` on **every** turn, and the caller
    catches ``Exception`` around accounting so that nothing must ever cost a developer their
    response. The result was silent: ``~/.ace/telemetry.db`` stayed empty, ``store.summary()``
    returned zeros, and the dashboard's live panel reported no spend on a sidecar that was
    relaying traffic correctly the whole time.

    A permissive attribute bag rather than a fixed dataclass, because the consumer
    (``LocalStore.record_log``) reads by ``getattr`` with defaults, and pinning a field list
    here would create a second definition to keep in step with the cloud one.
    """

    __slots__ = ("__dict__",)

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_LocalRequestLog({self.__dict__!r})"


def _request_log_class():
    """The cloud gateway's ``RequestLog`` when this tree has one, else the local stand-in.

    Resolved per call and not cached: the import is cheap once Python has it in
    ``sys.modules``, and caching a negative result would defeat a deployment that adds the
    telemetry package later.
    """
    try:
        from ace.observability.telemetry import RequestLog

        return RequestLog
    except Exception:
        return _LocalRequestLog


def usage_to_request_log(
    usage: StreamUsage,
    *,
    request_id: str,
    tenant_id: Optional[str] = None,
    session_id: Optional[str] = None,
    key_id: str = "",
    ttft_ms: float = 0.0,
):
    """Project a turn onto the gateway's telemetry row (P0-6).

    Two fields are deliberately NOT what they look like:

    * ``tokens_in`` carries Anthropic's ``input_tokens``, the **fresh** remainder — the
      cached buckets are their own columns. Summing all three gives the real prompt size.
    * ``cache_read_input_tokens`` / ``cache_write_input_tokens`` are the PROVIDER's prompt
      cache, not ACE's semantic cache (``cache_hit`` / ``cache_served``, left False here —
      no ACE lever ran on this path in Phase 0). See the RequestLog field comments.
    """
    RequestLog = _request_log_class()

    cost = usage.cost()
    write_5m, write_1h = usage.split_cache_writes()
    return RequestLog(
        request_id=request_id,
        org_id=tenant_id or "",
        key_id=key_id,
        team=tenant_id or "",
        model=usage.model or "",
        # Left as-is deliberately. This is the wrong column for the fact — `tier` means the
        # FLEET tier everywhere else — but it is the value this row has always carried, and
        # any dashboard or query that already groups relay traffic does it by this string.
        # `surface` below is the correct home; changing `tier` now would silently empty
        # whatever is reading it. Worth reconciling once nothing does.
        tier=_SURFACE,
        tokens_in=usage.input_tokens,
        tokens_out=usage.output_tokens,
        cost_usd=cost.total_usd,
        # A mid-stream error yields HTTP 200 with a failed turn inside — status must reflect
        # the turn, not the transport, or the dashboard books it as a success.
        status=f"error_{usage.error_type}" if usage.error_type else "ok",
        cache_tier=None,
        ttft_ms=ttft_ms,
        finished_at=time.time(),
        provider="anthropic",
        channel="direct",
        upstream_called=True,
        tenant_id=tenant_id,
        session_id=session_id,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_write_input_tokens=write_5m + write_1h,
        # The relay baseline. These rows are what the engine-backed surfaces get compared
        # AGAINST — same provider, same traffic shape, no levers — so the comparison the
        # Anthropic shim exists to justify is a group-by on this column.
        #
        surface=_SURFACE,
    )


def _levers_usage(usage: StreamUsage):
    """Project this route's usage record onto the provider-neutral one levers read.

    A deliberate narrowing, not a copy. ``levers.types.Usage`` carries a TTL *breakdown*
    rather than Anthropic's ``ephemeral_5m``/``ephemeral_1h`` field names, because the
    cache-write premium is a provider property and a lever tuned against one provider's cache
    economics gives wrong answers on another. Doing the translation here keeps the wire
    vocabulary on this side of the seam.
    """
    from ace.sidecar.levers.types import Usage

    write_5m, write_1h = usage.split_cache_writes()
    by_ttl = {k: v for k, v in (("5m", write_5m), ("1h", write_1h)) if v}
    return Usage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=write_5m + write_1h,
        cache_write_by_ttl=by_ttl,
    )


def install_messages_route(
    app,
    *,
    config: Optional[MessagesConfig] = None,
    client: Optional[httpx.AsyncClient] = None,
    on_usage: Optional[Callable[[StreamUsage], None]] = None,
    accountant=None,
    capture=None,
    auth_config: Optional["AuthConfig"] = None,
    byok=None,
    shadow=None,
) -> None:
    """Mount ``POST /v1/messages`` on ``app``.

    Self-contained on purpose: it takes an app and a config rather than threading through
    ``proxy.create_app``'s ~40-parameter factory — keeping the seam this thin is what lets
    the P0-5 local sidecar mount this route alone, without the cloud gateway's
    cache/router/telemetry stack.

    ``client`` injects an ``httpx.AsyncClient`` so the P0-4 conformance suite can drive this
    exact production branch through ``MockTransport`` with no live call.

    ``shadow`` is an optional :class:`ace.sidecar.levers.shadow.ShadowRunner`. It is the one
    place this module does anything a Phase 0 relay did not, and it was designed to be
    unable to violate the fidelity invariant:

    * it never sees ``raw`` — only ``parsed``, the throwaway copy this route already makes to
      decide streaming and model, so there is no object shared with what goes upstream;
    * it runs **after** the response has been served, on a worker thread, so a counting round
      trip cannot land in the developer's turn latency;
    * it is skipped entirely — one cached entry-point lookup — when no lever package is
      installed, which is the ordinary state.

    It also supplies the credential problem's only solution: under ``no_key: true`` the
    relayed token is the sole credential that can reach the counting endpoint, and it exists
    only for the life of this request.
    """
    cfg = config or MessagesConfig.from_env()
    auth_cfg = auth_config or AuthConfig.from_env()
    _client: Optional[httpx.AsyncClient] = client

    def _get_client() -> httpx.AsyncClient:
        nonlocal _client
        if _client is None:
            _client = httpx.AsyncClient()
        return _client

    @app.post(MESSAGES_PATH)
    async def anthropic_messages(request: Request) -> Response:
        # THE fidelity invariant: these bytes, and only these bytes, go upstream.
        raw = await request.body()

        parsed, parse_err = _peek(raw)
        if parse_err is not None:
            return _error(400, "invalid_request_error", parse_err)
        assert parsed is not None  # narrowed by parse_err is None

        # ------------------------------------------------------------------------------
        # ACTUATION. The one place the fidelity invariant above is deliberately relaxed,
        # and it is relaxed by SPLICING, never by re-serializing: `levers.splice` replaces
        # the byte range of one tool result's content inside `raw` and leaves every other
        # byte — cache_control markers, thinking signatures, key order — as it arrived,
        # because it never parses them out.
        #
        # Three properties make this safe enough to sit on a developer's hot path:
        #   * it is skipped entirely unless a lever is resolved to `on`, which is a word
        #     that has to be typed per lever in ~/.ace/config.json;
        #   * a splice at or before the last cache_control breakpoint is REFUSED, checked
        #     as a byte offset rather than argued from the lever's intent;
        #   * any doubt at all returns the original bytes. `raw` is only rebound on a
        #     verified result that still parses.
        if shadow is not None:
            try:
                if shadow.actuating:
                    new_raw, act = shadow.actuate(raw, parsed)
                    if new_raw is not None:
                        log.info(
                            "[messages] actuated: %d splice(s), %d bytes removed, levers=%s",
                            act.get("spliced"), act.get("saved_bytes"), act.get("levers"),
                        )
                        raw = new_raw
                    elif act.get("refused"):
                        log.debug("[messages] actuation refused: %s", act["refused"])
            except Exception:  # pragma: no cover - never costs a turn
                log.warning("[messages] actuation raised; relaying unmodified", exc_info=True)

        auth = await authenticate(request, config=auth_cfg, byok=byok)
        if not auth.ok:
            err = auth.error
            return _error(err.status, err.type, err.message)
        api_key = auth.api_key

        headers = {
            k: v
            for k, v in ((h, request.headers.get(h)) for h in _FORWARD_REQUEST_HEADERS)
            if v
        }
        # Present the credential the way it arrived. A subscription Claude Code session
        # authenticates with OAuth over `Authorization: Bearer`, and Anthropic rejects an
        # OAuth token forwarded as `x-api-key` — normalising to one header breaks every
        # subscription user. `upstream_auth_headers` also merges the OAuth beta that
        # /v1/messages requires, without dropping betas the caller asked for.
        headers.update(
            upstream_auth_headers(
                api_key,
                auth.scheme,
                inbound_beta=request.headers.get("anthropic-beta"),
            )
        )
        # Anthropic requires this; Claude Code always sends it, but a hand-rolled client or
        # a curl smoke test may not, and a missing version is a confusing 400 from upstream.
        headers.setdefault("anthropic-version", "2023-06-01")
        headers.setdefault("content-type", "application/json")

        # Per-turn accounting identity. `session_id` is what makes a multi-turn agentic run
        # aggregate as ONE unit instead of N unrelated rows — the difference between "this
        # session cost $2.40" and a scatter of per-turn numbers nobody can add up. Claude
        # Code does not send a session header today, so this is None unless a caller
        # supplies one; deriving a stable session identity from the request itself is
        # Phase 1 work (the cache prefix is the natural candidate).
        req_id = request.headers.get("x-request-id") or f"msg-{uuid4().hex[:12]}"
        session_id = request.headers.get("x-ace-session")
        # Tenant comes from the RESOLVED developer key, never from a caller-supplied
        # header — attributing spend on an x-ace-team a client can set at will would let any
        # caller bill another org's dashboard. The header is honoured only in loopback mode,
        # where there is no dev key and no cross-tenant billing to protect.
        tenant_id = auth.tenant_id or (
            request.headers.get("x-ace-team")
            if auth_cfg.mode == MODE_LOOPBACK
            else None
        )
        t_start = time.monotonic()

        def _capture(status: int, streamed: bool, usage=None) -> None:
            if capture is None:
                return
            capture.record(
                raw_request=raw,
                headers=request.headers,
                status=status,
                streamed=streamed,
                usage=usage,
                request_id=req_id,
            )

        def _sink(usage: StreamUsage) -> None:
            _capture(200, usage.streamed, usage)
            if accountant is not None:
                try:
                    accountant.record_log(
                        usage_to_request_log(
                            usage,
                            request_id=req_id,
                            tenant_id=tenant_id,
                            session_id=session_id,
                            ttft_ms=(time.monotonic() - t_start) * 1000.0,
                        )
                    )
                except (
                    Exception
                ):  # pragma: no cover - telemetry never breaks a response
                    log.debug("[messages] accountant.record_log failed", exc_info=True)
            if on_usage is not None:
                on_usage(usage)
            _shadow(usage)

        def _shadow(usage: StreamUsage) -> None:
            """Hand this turn to the levers, detached. Never touches the served response.

            Ordered last in `_sink` on purpose: accounting is the thing that must not be lost,
            and a lever package is third-party code. Anything that goes wrong past this point
            costs a measurement, never a turn.
            """
            if shadow is None or not shadow.enabled:
                return
            try:
                from ace.sidecar.levers.counter import resolve_counter

                # The in-flight credential, adopted once. `set_counter` keeps the first one
                # for the life of the process so a refusal is remembered instead of re-asked
                # on every turn.
                if shadow.counter is None and api_key:
                    counter, _ = resolve_counter(api_key, auth.scheme)
                    shadow.set_counter(counter)

                import asyncio

                asyncio.get_running_loop().create_task(
                    shadow.observe_async(
                        parsed,
                        _levers_usage(usage),
                        model=usage.model or parsed.get("model") or "",
                        request_id=req_id,
                        session_id=session_id,
                    )
                )
            except Exception:  # pragma: no cover - a shadow run never surfaces
                log.debug("[messages] shadow lever run could not start", exc_info=True)

        url = cfg.base_url.rstrip("/") + MESSAGES_PATH

        if parsed.get("stream"):
            return await _relay_stream(
                client=_get_client(),
                url=url,
                raw=raw,
                headers=headers,
                timeout_s=cfg.timeout_s,
                model_hint=parsed.get("model"),
                on_usage=_sink,
            )

        try:
            resp = await _get_client().post(
                url,
                content=raw,  # NOT json=parsed — that would re-serialize. See module docstring.
                headers=headers,
                timeout=cfg.timeout_s,
            )
        except httpx.TimeoutException:
            return _error(
                504, "timeout_error", f"upstream timed out after {cfg.timeout_s:.0f}s"
            )
        except httpx.HTTPError as e:
            return _error(502, "api_error", f"upstream request failed: {e}")

        # Relay verbatim, success or failure. An upstream error body is already in the
        # client's native shape and carries detail we would only lose by rewriting it.
        out = {k: resp.headers[k] for k in _RELAY_RESPONSE_HEADERS if k in resp.headers}
        out[_PATH_MARKER_HEADER] = _PATH_MARKER

        # Account for the turn. Parsing the response here is reading, not forwarding — the
        # relayed bytes below are still `resp.content`, untouched.
        if 200 <= resp.status_code < 300:
            try:
                usage = usage_from_payload(json.loads(resp.content))
                if usage.model is None:
                    usage.model = parsed.get("model")
                _report_usage(usage, _sink, streamed=False)
            except (
                Exception
            ):  # pragma: no cover - never fail a served response on accounting
                log.debug("[messages] usage accounting failed", exc_info=True)
        else:
            log.info(
                "[messages] model=%s status=%d bytes_in=%d bytes_out=%d",
                parsed.get("model"),
                resp.status_code,
                len(raw),
                len(resp.content),
            )
            _capture(resp.status_code, False)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=out,
            media_type=resp.headers.get("content-type", "application/json"),
        )
