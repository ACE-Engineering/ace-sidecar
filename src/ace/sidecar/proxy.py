"""ace.sidecar.proxy — 100% local, zero-config proxy for Claude Code & Anthropic API.

Features:
- Intercepts `POST /v1/messages` on loopback (127.0.0.1).
- In-flight context optimization: tool-result head+tail truncation, read dedup, prose compression,
  and prompt-cache TTL (300s) idle compaction.
- Real-time streaming SSE passthrough via non-blocking UsageTee.
- 100% locally hosted: zero telemetry phone-home, zero remote tracking, zero billing.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from ace.sidecar.compression.engine import ContextOptimizer
from ace.sidecar.routing.router import SidecarRouter

log = logging.getLogger("ace.sidecar.proxy")
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"


class StreamUsageTracker:
    """Tee that observes SSE tokens on the fly without delaying stream chunks."""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.model: Optional[str] = None
        self._buf = b""

    def feed(self, chunk: bytes) -> None:
        try:
            self._buf += chunk
            while b"\n\n" in self._buf:
                event, self._buf = self._buf.split(b"\n\n", 1)
                for line in event.split(b"\n"):
                    if line.startswith(b"data:"):
                        data_str = line[5:].strip()
                        try:
                            payload = json.loads(data_str)
                        except Exception:
                            continue
                        if isinstance(payload, dict):
                            self._handle_payload(payload)
        except Exception:
            pass

    def _handle_payload(self, payload: Dict[str, Any]) -> None:
        etype = payload.get("type")
        if etype == "message_start":
            msg = payload.get("message") or {}
            self.model = msg.get("model") or self.model
            u = msg.get("usage") or {}
            self.input_tokens = int(u.get("input_tokens") or 0)
            self.cache_read_tokens = int(u.get("cache_read_input_tokens") or 0)
            self.cache_write_tokens = int(u.get("cache_creation_input_tokens") or 0)
        elif etype == "message_delta":
            u = payload.get("usage") or {}
            if "output_tokens" in u:
                self.output_tokens = int(u.get("output_tokens") or 0)


def create_sidecar_app(
    optimizer: Optional[ContextOptimizer] = None,
    router: Optional[SidecarRouter] = None,
    upstream_url: Optional[str] = None,
    api_key: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> FastAPI:
    """Build the standalone local sidecar FastAPI application."""
    app = FastAPI(title="ACE Local Sidecar", version="0.1.0")
    opt = optimizer or ContextOptimizer()
    rtr = router
    base_url = upstream_url or os.environ.get("ANTHROPIC_BASE_URL", ANTHROPIC_DEFAULT_BASE_URL)
    _client = client

    session_stats = {
        "turns_processed": 0,
        "total_saved_bytes": 0,
        "total_saved_tokens": 0,
        "tool_results_truncated": 0,
        "reads_deduped": 0,
        "prose_turns_compressed": 0,
        "idle_compactions": 0,
        "routing": rtr.stats if rtr else None,
    }

    def get_http_client() -> httpx.AsyncClient:
        nonlocal _client
        if _client is None:
            _client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
        return _client

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {
            "status": "ok",
            "service": "ace-sidecar",
            "version": "0.1.0",
            "host": "127.0.0.1",
            "mode": "loopback-local",
            "upstream": base_url,
            "compression_enabled": True,
            "routing_enabled": rtr is not None,
            "stats": session_stats,
        }

    @app.get("/api/stats")
    async def stats() -> Dict[str, Any]:
        return {"status": "ok", "stats": session_stats}

    @app.get("/dashboard", response_class=HTMLResponse)
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(range: str = "30d", agent: str = "all", privacy: bool = False, theme: str = "auto") -> Any:
        try:
            from ace.sidecar.dashboard_render import render
            from ace.sidecar.insights import DEFAULT_RANGE, RANGES, build

            key = range if range in RANGES else DEFAULT_RANGE
            store = None
            if not os.environ.get("ACE_SIDECAR_NO_TELEMETRY"):
                try:
                    from ace.gateway.local_store import LocalStore
                    db_path = os.environ.get("ACE_SIDECAR_TELEMETRY_DB", os.path.expanduser("~/.ace/telemetry.db"))
                    if os.path.exists(db_path):
                        store = LocalStore(db_path)
                except Exception:
                    pass

            html = render(build(store=store, range_key=key, agent=agent), theme=theme)
            if privacy:
                privacy_style = """
                <style>
                  .st .v, .st .n, td.num, .rec-saving, .lever-headroom, .calcbox pre {
                    filter: blur(8px) !important;
                    user-select: none !important;
                    transition: filter 0.2s;
                  }
                  .st .v:hover, .st .n:hover, td.num:hover {
                    filter: blur(0px) !important;
                  }
                </style>
                """
                html = html.replace("</head>", f"{privacy_style}</head>")
                html = html.replace("Local only", "🔒 PRIVACY MODE · PII MASKED")
            return HTMLResponse(html)
        except Exception as e:
            return HTMLResponse(f"<h1>Dashboard Error</h1><pre>{e}</pre>")

    @app.post("/v1/messages")
    async def handle_messages(request: Request) -> Response:
        # Loopback security check: only accept 127.0.0.1 or localhost peers
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "localhost", "::1", "testclient"):
            return JSONResponse(
                status_code=403,
                content={"type": "error", "error": {"type": "permission_error", "message": "ACE Sidecar only accepts local loopback connections."}},
            )

        raw_body = await request.body()
        try:
            payload = json.loads(raw_body)
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={"type": "error", "error": {"type": "invalid_request_error", "message": f"Malformed JSON: {e}"}},
            )

        # Apply ContextOptimizer
        opt_res = opt.optimize_request(payload)
        out_payload = opt_res.optimized_payload

        # Apply Model Routing
        route_decision = None
        if rtr:
            headers_dict = dict(request.headers)
            out_payload, route_decision = rtr.route_request(out_payload, headers=headers_dict)
            if route_decision.anchor_updated or route_decision.target_model != route_decision.original_model:
                log.info(
                    "[ACE sidecar] Model routed: %s -> %s (action=%s, reason=%s)",
                    route_decision.original_model,
                    route_decision.target_model,
                    route_decision.action.value,
                    route_decision.reason,
                )

        session_stats["turns_processed"] += 1
        session_stats["total_saved_bytes"] += opt_res.saved_bytes
        session_stats["total_saved_tokens"] += opt_res.saved_tokens_est
        session_stats["tool_results_truncated"] += opt_res.tool_results_truncated
        session_stats["reads_deduped"] += opt_res.reads_deduped
        session_stats["prose_turns_compressed"] += opt_res.prose_turns_compressed
        if opt_res.idle_compacted:
            session_stats["idle_compactions"] += 1

        if opt_res.saved_bytes > 0 or opt_res.idle_compacted:
            log.info(
                "[ACE sidecar] Optimized request: saved %d bytes (~%d tokens). IdleCompacted=%s",
                opt_res.saved_bytes,
                opt_res.saved_tokens_est,
                opt_res.idle_compacted,
            )

        # Prepare outbound headers
        forward_headers = {
            "content-type": "application/json",
            "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        }
        if "anthropic-beta" in request.headers:
            forward_headers["anthropic-beta"] = request.headers["anthropic-beta"]

        auth_key = (
            request.headers.get("x-api-key")
            or api_key
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if auth_key:
            forward_headers["x-api-key"] = auth_key

        upstream_url_full = base_url.rstrip("/") + "/v1/messages"
        out_bytes = json.dumps(out_payload).encode("utf-8")

        # Streamed request
        if payload.get("stream"):
            http_c = get_http_client()
            tracker = StreamUsageTracker()

            try:
                upstream_ctx = http_c.stream(
                    "POST",
                    upstream_url_full,
                    content=out_bytes,
                    headers=forward_headers,
                )
                upstream_resp = await upstream_ctx.__aenter__()
            except Exception as e:
                return JSONResponse(
                    status_code=502,
                    content={"type": "error", "error": {"type": "api_error", "message": f"Upstream relay failed: {e}"}},
                )

            if upstream_resp.status_code >= 400:
                body = await upstream_resp.aread()
                await upstream_ctx.__aexit__(None, None, None)
                return Response(
                    content=body,
                    status_code=upstream_resp.status_code,
                    media_type=upstream_resp.headers.get("content-type", "application/json"),
                )

            async def stream_generator() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream_resp.aiter_bytes():
                        if chunk:
                            tracker.feed(chunk)
                            yield chunk
                finally:
                    await upstream_ctx.__aexit__(None, None, None)
                    log.info(
                        "[ACE sidecar] Stream finished: in=%d out=%d cache_read=%d cache_write=%d",
                        tracker.input_tokens,
                        tracker.output_tokens,
                        tracker.cache_read_tokens,
                        tracker.cache_write_tokens,
                    )

            resp_headers = {
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
                "x-ace-saved-bytes": str(opt_res.saved_bytes),
                "x-ace-saved-tokens": str(opt_res.saved_tokens_est),
            }
            if route_decision:
                resp_headers["x-ace-routed-model"] = route_decision.target_model
                resp_headers["x-ace-original-model"] = route_decision.original_model
                resp_headers["x-ace-routing-action"] = route_decision.action.value
                resp_headers["x-ace-routing-reason"] = route_decision.reason

            return StreamingResponse(
                stream_generator(),
                status_code=200,
                media_type="text/event-stream",
                headers=resp_headers,
            )

        # Non-streamed request
        http_c = get_http_client()
        try:
            resp = await http_c.post(
                upstream_url_full,
                content=out_bytes,
                headers=forward_headers,
            )
            non_stream_headers = {
                "x-ace-saved-bytes": str(opt_res.saved_bytes),
                "x-ace-saved-tokens": str(opt_res.saved_tokens_est),
            }
            if route_decision:
                non_stream_headers["x-ace-routed-model"] = route_decision.target_model
                non_stream_headers["x-ace-original-model"] = route_decision.original_model
                non_stream_headers["x-ace-routing-action"] = route_decision.action.value
                non_stream_headers["x-ace-routing-reason"] = route_decision.reason
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
                headers=non_stream_headers,
            )
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"type": "error", "error": {"type": "api_error", "message": f"Upstream relay failed: {e}"}},
            )

    return app
