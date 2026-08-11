"""ace.gateway.messages_auth — who may call ``/v1/messages``, and whose key pays.

**Phase 0 / P0-3.** Replaces P0-1's minimum-to-move-bytes auth, and retires the
``ACE_MESSAGES=on`` flag that was standing in for it.

Two modes, one explicit choice
------------------------------
``dev_key`` (the default)
    Cloud behaviour, identical to ``/v1/chat/completions``: the caller presents an ACE
    developer key, and the provider key comes from that tenant's encrypted vendor-key store
    unless they pass one per-request.

``loopback``
    The P0-5 local sidecar: no ACE key exists or is wanted. Claude Code holds a dummy token,
    ACE holds the real Anthropic key in local config, and nothing leaves the machine.

The threat this module exists to prevent
----------------------------------------
The obvious implementation of loopback trust — *"trust the request if it came from
127.0.0.1"* — is **wrong, and dangerously so**. A gateway behind nginx, Caddy, or any
same-host reverse proxy sees **every** request arrive from 127.0.0.1. Inferring trust from
the peer address alone would therefore turn a perfectly ordinary cloud deployment into an
open relay — spending whatever key the deployment holds, for anyone who can reach the proxy.
That is precisely the exposure the ``ACE_MESSAGES`` flag was a stopgap for, so re-introducing
it here would defeat the point of P0-3.

So trust is **declared, never inferred**, and three independent conditions must all hold:

1. the operator explicitly set ``ACE_MESSAGES_AUTH=loopback`` — a deployment cannot fall
   into this mode by accident;
2. the request's real peer address is a loopback address — so even in local mode, an
   off-box caller is refused;
3. the request carries **no proxy headers** (``x-forwarded-for`` / ``x-real-ip`` /
   ``forwarded``) — their presence proves a hop we cannot see, which means condition 2 is
   describing the proxy rather than the client.

Condition 3 is what makes condition 2 mean anything. Without it, "peer is loopback" and
"client is local" are not the same statement.

Fail-closed corollary: ``dev_key`` mode with no BYOK context configured cannot verify
anything, so it refuses every request rather than waving them through.

Provider-key precedence (both modes)
------------------------------------
    request header  ->  the mode's stored key  ->  refuse

Per-request always wins, so a caller can override without touching stored config, and the
zero-knowledge path (send your own key, we never persist it) keeps working. The stored key
is the tenant's encrypted vendor key in ``dev_key`` mode and local config in ``loopback``.

**Credentials are never logged.** Not at debug, not in an error body, not in telemetry. The
only place a key appears is the outbound ``x-api-key`` header. Pinned by
``test_no_credential_ever_reaches_logs``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

log = logging.getLogger("ace.gateway.messages_auth")

MODE_DEV_KEY = "dev_key"
MODE_LOOPBACK = "loopback"

ANTHROPIC_PROVIDER = "anthropic"

# How a credential is presented to Anthropic. These are NOT interchangeable: an OAuth token
# sent as `x-api-key` is rejected, and so is an API key that arrives alongside a stray
# Authorization header (the API refuses requests carrying both).
SCHEME_API_KEY = "api_key"
SCHEME_BEARER = "bearer"

# Required on /v1/messages when the credential is an OAuth token. Other endpoints tolerate
# its absence; this one does not, so a Claude Code session on a subscription fails without it.
OAUTH_BETA = "oauth-2025-04-20"

# Anthropic OAuth access tokens carry this prefix; API keys use `sk-ant-api...`. Used only to
# decide whether the OAuth beta needs repairing — never to decide whether to forward.
_OAUTH_TOKEN_PREFIX = "sk-ant-oat"

# Headers that prove the request passed through a proxy. Their presence means the socket
# peer is that proxy, not the client — so a loopback peer address says nothing about who
# actually sent the request. See the module docstring.
_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host")


@dataclass(frozen=True)
class AuthConfig:
    """How this deployment authenticates ``/v1/messages``."""

    mode: str = MODE_DEV_KEY
    # The sidecar's stored Anthropic key. Only consulted in loopback mode — a cloud
    # deployment must never serve traffic from a process-wide key, which is exactly the
    # open-relay shape P0-3 exists to close.
    local_api_key: Optional[str] = None

    @classmethod
    def from_env(cls) -> "AuthConfig":
        mode = (os.environ.get("ACE_MESSAGES_AUTH", MODE_DEV_KEY) or "").strip().lower()
        if mode not in (MODE_DEV_KEY, MODE_LOOPBACK):
            # An unrecognized value must not silently become the permissive mode.
            log.warning(
                "[messages-auth] unknown ACE_MESSAGES_AUTH=%r — falling back to %r",
                mode,
                MODE_DEV_KEY,
            )
            mode = MODE_DEV_KEY
        return cls(
            mode=mode,
            local_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() or None,
        )


@dataclass(frozen=True)
class AuthError:
    status: int
    type: str
    message: str


@dataclass(frozen=True)
class AuthResult:
    """Either a usable credential or the error to return. Never both."""

    api_key: Optional[str] = None
    # How to present `api_key` upstream. Defaults to api_key because every stored credential
    # (local config, vendor-key store) is an API key; only a per-request bearer changes it.
    scheme: str = SCHEME_API_KEY
    tenant_id: Optional[str] = None
    dev_key_id: Optional[str] = None
    error: Optional[AuthError] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.api_key)


def _peer_host(request) -> Optional[str]:
    client = getattr(request, "client", None)
    return getattr(client, "host", None) if client is not None else None


def is_loopback_peer(request) -> bool:
    """True when the socket peer is a loopback address.

    Fails closed on anything unparseable or absent — an ASGI server that does not report a
    client address must not be read as "local".
    """
    host = _peer_host(request)
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def has_proxy_headers(headers) -> bool:
    """True when the request shows evidence of an intermediary hop."""
    return any(headers.get(h) for h in _PROXY_HEADERS)


def loopback_trust_denial(request) -> Optional[AuthError]:
    """Why loopback trust does NOT apply to this request, or None when it does.

    Split out from :func:`authenticate` so the two refusal reasons stay individually
    testable — they are different attacks and collapsing them into one boolean would make a
    regression in either invisible.
    """
    if not is_loopback_peer(request):
        return AuthError(
            403,
            "permission_error",
            "this ACE sidecar accepts loopback connections only; "
            f"request came from {_peer_host(request) or 'an unknown address'}.",
        )
    if has_proxy_headers(request.headers):
        return AuthError(
            403,
            "permission_error",
            "request carries proxy headers, so its origin cannot be verified as local. "
            "Run the sidecar without a reverse proxy, or use ACE_MESSAGES_AUTH=dev_key.",
        )
    return None


def request_credential(headers) -> Tuple[Optional[str], str]:
    """The credential this request carries, **and how it was presented**.

    Returns ``(token, scheme)``. The scheme matters as much as the token: Claude Code on an
    API key sends ``x-api-key``, but Claude Code on a **subscription** authenticates with
    OAuth and sends ``Authorization: Bearer`` instead. Anthropic rejects an OAuth token
    presented as ``x-api-key``, so a proxy that normalises everything to one header breaks
    every subscription user — which is exactly what this used to do.

    The rule is therefore: **preserve the presentation**. We are a relay, not an
    authenticator; the client already knows which scheme its credential belongs to.
    """
    passthrough = (headers.get("x-api-key") or "").strip()
    if passthrough:
        return passthrough, SCHEME_API_KEY
    authz = (headers.get("authorization") or "").strip()
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
        if token:
            return token, SCHEME_BEARER
    return None, SCHEME_API_KEY


def request_provider_key(headers) -> Optional[str]:
    """Just the token. Kept for callers that do not care how it arrived."""
    return request_credential(headers)[0]


def is_oauth_token(token: Optional[str]) -> bool:
    """Whether a credential is an Anthropic OAuth access token rather than an API key."""
    return bool(token) and token.startswith(_OAUTH_TOKEN_PREFIX)


def upstream_auth_headers(
    token: str, scheme: str, *, inbound_beta: Optional[str] = None
) -> Dict[str, str]:
    """Build the outbound auth headers for one credential.

    Exactly **one** auth header is emitted — Anthropic rejects a request carrying both
    ``x-api-key`` and ``Authorization``.

    When the credential is an OAuth token, ``/v1/messages`` also requires the
    ``oauth-2025-04-20`` beta. Claude Code sends it itself, but it is repaired here when
    absent so a hand-rolled client or an older Claude Code does not fail with an opaque 401.
    The repair is **merged** into whatever betas the caller already asked for, never
    substituted for them — dropping a caller's beta would silently downgrade the request.
    """
    headers: Dict[str, str] = {}
    if scheme == SCHEME_BEARER:
        headers["authorization"] = f"Bearer {token}"
        if is_oauth_token(token):
            betas = [b.strip() for b in (inbound_beta or "").split(",") if b.strip()]
            if OAUTH_BETA not in betas:
                betas.append(OAUTH_BETA)
            headers["anthropic-beta"] = ",".join(betas)
    else:
        headers["x-api-key"] = token
    return headers


# Every prefix an ACE developer key can carry. BOTH are live, which is the whole reason this
# is a tuple in one place rather than a literal at each call site:
#
# * ``ace_dev_`` — what ace-fleet MINTS. Every key a customer holds looks like this
#   (`mint-dev-key.functions.ts`), so it is the one that matters in production.
# * ``ace_tok_`` — env-seeded keys (``ACE_BYOK_DEV_KEYS``) and this repo's test fixtures.
#
# Checking only ``ace_tok_`` was a real defect with two faces. On ``/v1/messages`` it meant a
# customer's own key, presented as ``x-api-key``, was not recognized as ours and was FORWARDED
# TO ANTHROPIC as a provider credential — leaking an ACE tenant token to a third party and
# earning a confusing 401. On the ingress shims it meant the advertised SDK drop-in 401'd for
# every real key, while the tests passed because their fixtures spell it the other way.
#
# BOTH ARE PERMANENT. This is a decision, not a migration waiting to happen: ``ace_tok_`` is
# not deprecated and no date is coming for it. Retiring a prefix would break every deployment
# whose keys are seeded through ``ACE_BYOK_DEV_KEYS`` — environments nobody is asking to
# change — in exchange for tidiness worth less than the breakage. Anything that tests a
# credential's provenance reads THIS tuple; nothing re-spells either value inline.
ACE_DEV_KEY_PREFIXES = ("ace_dev_", "ace_tok_")


def looks_like_ace_dev_key(value: Optional[str]) -> bool:
    """Whether a credential is an ACE developer key rather than a provider key.

    Wherever a single header can carry either — ``Authorization: Bearer`` here, ``x-api-key``
    on the Anthropic surface, ``api-key`` on the Azure ones — this is the test that separates
    them, and every such place must use THIS function. The failure mode when they disagree is
    not a 500 but a silent misroute: our token sent to a provider, or a customer's key treated
    as one and the request refused.
    """
    return bool(value) and value.startswith(ACE_DEV_KEY_PREFIXES)


# Retained for callers that predate the rename; the name was private and the behaviour is
# identical, so this is a pure alias rather than a compatibility shim with its own rules.
_looks_like_ace_dev_key = looks_like_ace_dev_key


async def authenticate(request, *, config: AuthConfig, byok=None) -> AuthResult:
    """Authenticate one ``/v1/messages`` request and resolve the key that pays for it."""
    if config.mode == MODE_LOOPBACK:
        return _authenticate_loopback(request, config)
    return await _authenticate_dev_key(request, byok)


def _authenticate_loopback(request, config: AuthConfig) -> AuthResult:
    denial = loopback_trust_denial(request)
    if denial is not None:
        return AuthResult(error=denial)

    # Precedence: this request's own credential, then the sidecar's stored one.
    api_key, scheme = request_credential(request.headers)
    if _looks_like_ace_dev_key(api_key):
        # A local sidecar has no dev-key registry to check this against, and forwarding it
        # to Anthropic would leak an ACE token to the provider. Ignore it and fall through
        # to local config — which is the configuration that is actually meant to pay.
        api_key, scheme = None, SCHEME_API_KEY
    if not api_key:
        # A stored key is an API key by configuration, whatever the caller happened to send.
        api_key, scheme = config.local_api_key, SCHEME_API_KEY
    if not api_key:
        return AuthResult(
            error=AuthError(
                401,
                "authentication_error",
                "no Anthropic credential: set ANTHROPIC_API_KEY where the ACE sidecar runs, "
                "or send x-api-key with the request.",
            )
        )
    return AuthResult(api_key=api_key, scheme=scheme)


async def _authenticate_dev_key(request, byok) -> AuthResult:
    if byok is None:
        # Cannot verify anyone, so serve no one. The alternative — treating "no registry" as
        # "no check" — is how a gateway becomes an open relay.
        return AuthResult(
            error=AuthError(
                503,
                "api_error",
                "this deployment cannot authenticate /v1/messages: no developer-key "
                "registry is configured. Set ACE_BYOK, or run in loopback mode "
                "(ACE_MESSAGES_AUTH=loopback) for a local sidecar.",
            )
        )

    from ace.gateway.proxy import _dev_key_error_message, _resolve_dev

    dev, reason = await _resolve_dev(byok, request.headers)
    if dev is None:
        return AuthResult(
            error=AuthError(401, "authentication_error", _dev_key_error_message(reason))
        )

    tenant_id = dev.tenant_id
    dev_key_id = getattr(dev, "dev_key_id", None)

    # Per-request provider key wins — the zero-knowledge path. The ACE key rides the same
    # Authorization header, so it must not be mistaken for the provider credential.
    api_key = request.headers.get("x-api-key") or None
    if api_key:
        api_key = api_key.strip() or None
    if _looks_like_ace_dev_key(api_key):
        api_key = None
    if not api_key:
        try:
            api_key = byok.vendor_keys.get_key(tenant_id, ANTHROPIC_PROVIDER)
        except Exception:  # pragma: no cover - a store fault must not read as "no key"
            log.warning("[messages-auth] vendor key lookup failed", exc_info=True)
            return AuthResult(
                error=AuthError(
                    503,
                    "api_error",
                    "could not read the stored Anthropic key for this org.",
                )
            )
    if not api_key:
        return AuthResult(
            error=AuthError(
                402,
                "authentication_error",
                "onboarding incomplete: no Anthropic key stored for this org. Store one via "
                "POST /api/v1/vendor_key/create, or send x-api-key with the request.",
            )
        )
    return AuthResult(api_key=api_key, tenant_id=tenant_id, dev_key_id=dev_key_id)


def describe(config: AuthConfig) -> Tuple[str, str]:
    """``(summary, warning)`` for the boot log. ``warning`` is empty when nothing is amiss."""
    if config.mode == MODE_LOOPBACK:
        return (
            "loopback-trust (no ACE developer key required; 127.0.0.1 only)",
            (
                ""
                if config.local_api_key
                else "no ANTHROPIC_API_KEY set — callers must send their own x-api-key"
            ),
        )
    return ("ACE developer key required (same as /v1/chat/completions)", "")
