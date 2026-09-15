"""ace.sidecar.routing.router — Sidecar routing coordinator, configuration binder, and metrics."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ace.sidecar.routing.config import DeploymentRoutingConfig, load_routing_config
from ace.sidecar.routing.engine import ModelRouter
from ace.sidecar.routing.types import RouteDecision

log = logging.getLogger("ace.sidecar.routing")


class SidecarRouter:
    """Coordinator for model routing inside the local loopback proxy."""

    def __init__(
        self,
        config: Optional[DeploymentRoutingConfig] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        self.config = config or load_routing_config(config_path)
        self.router = ModelRouter(config=self.config)
        self.stats = {
            "turns_routed": 0,
            "anchor_hits": 0,
            "escalations": 0,
            "overrides": 0,
            "sensitivity_alarms": 0,
            "models_selected": {},
            "estimated_dollars_saved": 0.0,
        }

    def extract_session_id(self, headers: Optional[Dict[str, str]], payload: Dict[str, Any]) -> str:
        """Derive a stable session ID from client headers or initial conversation prefix."""
        hdrs = headers or {}
        for k in ("x-session-id", "x-claude-session-id", "x-conversation-id", "session-id"):
            if k in hdrs and hdrs[k]:
                return hdrs[k]

        # Use system prompt hash or first message hash as stable session ID
        sys_prompt = str(payload.get("system") or "")
        msgs = payload.get("messages") or []
        first_content = ""
        if msgs and isinstance(msgs[0], dict):
            first_content = str(msgs[0].get("content") or "")

        seed = (sys_prompt[:200] + ":" + first_content[:200]).encode("utf-8")
        return "sess_" + hashlib.sha256(seed).hexdigest()[:16]

    def route_request(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict[str, Any], RouteDecision]:
        """Route request turn, mutate payload model if needed, and update statistics."""
        session_id = self.extract_session_id(headers, payload)
        mutated_payload, decision = self.router.route_turn(payload, session_id=session_id)

        self.stats["turns_routed"] += 1
        model = decision.target_model
        self.stats["models_selected"][model] = self.stats["models_selected"].get(model, 0) + 1

        action_name = decision.action.value
        if action_name == "maintain_anchor":
            self.stats["anchor_hits"] += 1
        elif action_name == "escalate":
            self.stats["escalations"] += 1
        elif action_name == "explicit_override":
            self.stats["overrides"] += 1
        elif action_name == "sensitivity_alarm":
            self.stats["sensitivity_alarms"] += 1

        # Calculate estimated savings if down-routed from standard Sonnet 3.7 ($3.00/1M) to Haiku 3.5 ($0.80/1M)
        if decision.original_model.startswith("claude-3-7-sonnet") and decision.target_model.startswith("claude-3-5-haiku"):
            tokens_est = int(payload.get("max_tokens") or 2000)
            saved = (tokens_est / 1_000_000) * (3.00 - 0.80)
            self.stats["estimated_dollars_saved"] += round(saved, 5)

        return mutated_payload, decision
