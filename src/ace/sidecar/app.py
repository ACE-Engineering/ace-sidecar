"""ace.sidecar — the local ACE sidecar app (P0-5).

The thing Claude Code points at. One route, one process, no cloud.

Why this is not ``gateway.serve.build_app()``
---------------------------------------------
The cloud gateway builds a fleet, a merit-order dispatcher, a semantic cache, a router, a
telemetry repository, dashboards, and a BYOK control plane — none of which a developer running
a local proxy has, wants, or should have to configure. It also *requires* an upstream
(``OPENAI_API_KEY`` or ``ACE_FLEET_YAML``) and refuses to boot without one, wrong for a sidecar
whose only upstream is Anthropic.

So the sidecar mounts ``install_messages_route`` on a bare FastAPI app and nothing else — a
seam that has existed since P0-1 so this would be possible without dragging the gateway in; it
takes an app and a config, not ``create_app``'s ~40 parameters.

Security posture
----------------
The sidecar always runs in **loopback-trust** auth mode (``messages_auth``): no ACE developer
key, provider key from local config. That mode independently requires a real loopback peer and
refuses anything carrying proxy headers, so binding to a non-loopback address produces 403s
rather than silently becoming an open relay. The CLI additionally refuses to bind off-loopback
without an explicit flag — belt and braces.

Every optimization lever is off. Phase 0 is a measurement release.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Optional

log = logging.getLogger("ace.sidecar")


def _iso_day(ts: Optional[float]) -> Optional[str]:
    """Epoch -> YYYY-MM-DD, or None. Dates only: a shared report needs no clock times."""
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def build_sidecar_app(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    accountant: Any = None,
    client: Any = None,
    capture: Any = None,
):
    """A FastAPI app serving only ``POST /v1/messages``, plus ``GET /healthz``.

    ``client`` injects an ``httpx.AsyncClient`` so tests can drive the real app through
    ``MockTransport`` without a live call.
    """
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    from ace.gateway.messages import MessagesConfig, install_messages_route
    from ace.gateway.messages_auth import MODE_LOOPBACK, AuthConfig

    app = FastAPI(title="ACE sidecar", version="0")

    cfg = MessagesConfig.from_env()
    if base_url:
        cfg = MessagesConfig(base_url=base_url, timeout_s=cfg.timeout_s)

    auth_env = AuthConfig.from_env()
    # Mode is forced, not read from the environment: this process IS the local sidecar, and
    # a stray ACE_MESSAGES_AUTH=dev_key would leave it demanding a developer key that a local
    # install has no way to issue.
    auth = AuthConfig(
        mode=MODE_LOOPBACK, local_api_key=api_key or auth_env.local_api_key
    )

    install_messages_route(
        app,
        config=cfg,
        auth_config=auth,
        accountant=accountant,
        capture=capture,
        client=client,
    )

    @app.get("/dashboard", response_class=HTMLResponse)
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(range: str = "30d", agent: str = "all") -> Any:
        from ace.sidecar.dashboard_render import render
        from ace.sidecar.insights import DEFAULT_RANGE, RANGES, build

        key = range if range in RANGES else DEFAULT_RANGE
        return HTMLResponse(
            render(
                build(
                    store=accountant,
                    capture=capture_summary(),
                    range_key=key,
                    agent=agent,
                )
            )
        )

    @app.get("/api/report")
    async def report(range: str = "30d", agent: str = "all") -> dict:
        """Scrubbed, shareable summary — aggregates only, no per-session detail."""
        from ace.sidecar.insights import DEFAULT_RANGE, RANGES, build

        key = range if range in RANGES else DEFAULT_RANGE
        d = build(store=accountant, capture=capture_summary(), range_key=key, agent=agent)
        h = d["historical"]
        # A shared report has to carry its own dates. "30d" means nothing to whoever opens
        # it a week later, and every per-month figure here is extrapolated from this span.
        sp = d.get("span") or {}
        tb, pk = d.get("time") or {}, d.get("parked") or {}
        return {
            "range": key,
            "covers": {
                "from": _iso_day(sp.get("first_ts")),
                "to": _iso_day(sp.get("last_ts")),
                "days": round(sp.get("days") or 0.0, 1),
                "window_full": not sp.get("partial"),
            },
            "turns": h.get("turns"),
            "sessions": h.get("sessions"),
            "cost_usd_list_price": round(h.get("cost_usd", 0.0), 2),
            "cache_saved_usd": round(h.get("cache_saved_usd", 0.0), 2),
            "cache_read_share": round(h.get("cache_share", 0.0), 4),
            "peak_context_tokens": h.get("peak_context"),
            "scorecards": d.get("scorecards"),
            "agent_breakdown": d.get("agent_breakdown"),
            # The clock, alongside the money. Hours only: a shared report needs the shape of
            # where the time went, not a timeline of when someone was at their desk. The live
            # alarm is deliberately absent — it is about this moment, not this span.
            "session_time_hours": {
                "wall_clock": round((tb.get("accounted_s") or 0.0) / 3600.0, 1),
                "active": round((tb.get("active_s") or 0.0) / 3600.0, 1),
                "idle": round((tb.get("idle_s") or 0.0) / 3600.0, 1),
                "parked_on_approval": round((pk.get("total_s") or 0.0) / 3600.0, 1),
                "parked_events": pk.get("events"),
            },
            "note": (
                "List-price valuation of local Claude Code sessions. Strategy figures are "
                "simulations with stated assumptions, not measurements. Parked-on-approval "
                "time is an upper bound, not a realised saving: a transcript cannot tell a "
                "human who would have returned sooner from one who was away regardless. No "
                "prompts, paths or session identifiers are included."
            ),
        }

    @app.get("/api/stats")
    async def stats(range: str = "30d", agent: str = "all") -> dict:
        from ace.sidecar.insights import DEFAULT_RANGE, RANGES, build

        key = range if range in RANGES else DEFAULT_RANGE
        return build(
            store=accountant,
            capture=capture_summary(),
            range_key=key,
            agent=agent,
        )

    @app.get("/metrics")
    async def prometheus_metrics(range: str = "all", agent: str = "all") -> Any:
        """Prometheus text exposition format endpoint for metrics scraping."""
        from fastapi.responses import Response
        from ace.sidecar.insights import build, format_prometheus_metrics

        d = build(
            store=accountant,
            capture=capture_summary(),
            range_key=range if range in ("24h", "7d", "30d", "all") else "all",
            agent=agent,
        )
        return Response(
            content=format_prometheus_metrics(d),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.post("/api/skills/install")

    async def install_skill(payload: dict) -> dict:
        """One-click local skill installation into .agents/skills/<skill_id>/SKILL.md."""
        from ace.sidecar.skill_miner import install_local_skill

        skill_id = payload.get("skill_id") or payload.get("id") or ""
        skill_md = payload.get("skill_md") or ""
        workspace = payload.get("workspace_dir") or os.getcwd()

        if not skill_id or not skill_md:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400, detail="skill_id and skill_md are required"
            )

        return install_local_skill(workspace, skill_id, skill_md)

    def capture_summary() -> dict:
        """Prompt composition from the most recent captured turn, if capturing.

        This is what surfaced the tool-definition finding: definitions render first, so
        unused ones sit at the head of the prefix and are re-read on every turn.
        """
        path = getattr(capture, "path", None)
        if not path or not os.path.exists(path):
            return {}
        try:
            import json as _json

            last = None
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        rec = _json.loads(line)
                        if rec.get("request_bytes", 0) > 10_000:
                            last = rec
            if not last:
                return {}
            req = last.get("request") or {}
            tools = req.get("tools") or []
            used = set()
            for m in req.get("messages") or []:
                c = m.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            used.add(b.get("name"))
            total = len(_json.dumps(tools))
            unused = sum(
                len(_json.dumps(t))
                for t in tools
                if (t.get("name") or t.get("type")) not in used
            )
            return {
                "tools_defined": len(tools),
                "tools_used": len(used),
                "tool_bytes_total": total,
                "tool_bytes_unused": unused,
                "system_bytes": len(_json.dumps(req.get("system"))),
                "messages_bytes": len(_json.dumps(req.get("messages"))),
            }
        except Exception:  # pragma: no cover - dashboard extra, never load-bearing
            return {}

    @app.get("/healthz")
    async def healthz() -> dict:
        # Deliberately reports whether a key is present, never the key.
        return {
            "status": "ok",
            "mode": auth.mode,
            "upstream": cfg.base_url,
            "provider_key": "configured" if auth.local_api_key else "missing",
            "levers": [],
            "capture": getattr(capture, "path", None),
            "captured_turns": getattr(capture, "count", 0),
        }

    return app
