"""ace.sidecar.routing.engine — Multi-dimensional model routing engine with session stickiness."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ace.sidecar.routing.config import DeploymentRoutingConfig, ProfileConfig
from ace.sidecar.routing.sensitivity import SensitivityClassifier
from ace.sidecar.routing.triggers import TriggerPipeline
from ace.sidecar.routing.types import (
    RouteDecision,
    SessionState,
    TriggerAction,
    TurnContext,
)


class ModelRouter:
    """Core multi-dimensional model router with session anchoring and parameter adaptation."""

    def __init__(
        self,
        config: Optional[DeploymentRoutingConfig] = None,
        sensitivity_classifier: Optional[SensitivityClassifier] = None,
    ) -> None:
        self.config = config or DeploymentRoutingConfig()
        self.sensitivity = sensitivity_classifier or SensitivityClassifier()
        self.sessions: Dict[str, SessionState] = {}
        self._init_pipeline()

    def _init_pipeline(self) -> None:
        profile = self.active_profile
        self.pipeline = TriggerPipeline.from_configs(profile.triggers)

    @property
    def active_profile(self) -> ProfileConfig:
        return self.config.profiles.get(self.config.active_profile) or ProfileConfig()

    def get_or_create_session(self, session_id: str) -> SessionState:
        now = time.monotonic()
        if session_id not in self.sessions:
            self.sessions[session_id] = SessionState(
                session_id=session_id,
                last_turn_timestamp=now,
                idle_seconds=0.0,
            )
        else:
            sess = self.sessions[session_id]
            sess.idle_seconds = max(0.0, now - sess.last_turn_timestamp)
            sess.last_turn_timestamp = now
        return self.sessions[session_id]

    def extract_context(self, payload: Dict[str, Any]) -> TurnContext:
        """Extract turn telemetry, prompt text, touched paths, and complexity from payload."""
        messages: List[Dict[str, Any]] = payload.get("messages") or []
        requested_model = str(payload.get("model") or "")
        prompt_tokens = int(payload.get("max_tokens") or 1000)

        last_user_prompt = ""
        recent_tool_names: List[str] = []
        touched_file_paths: List[str] = []

        for msg in messages[-6:]:
            role = msg.get("role")
            content = msg.get("content")

            if role == "user":
                if isinstance(content, str):
                    last_user_prompt = content
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                last_user_prompt = str(item.get("text") or "")

            elif role == "assistant" and isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        tool_name = str(item.get("name") or "")
                        recent_tool_names.append(tool_name)
                        inp = item.get("input") or {}
                        for k in ("path", "file_path", "filename", "file"):
                            if k in inp and isinstance(inp[k], str):
                                touched_file_paths.append(inp[k])

        path_matches = re.findall(r"(?:[a-zA-Z0-9_\-\.]+/)+[a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9]+", last_user_prompt)
        touched_file_paths.extend(path_matches)

        complexity = self._estimate_complexity(last_user_prompt, recent_tool_names)
        sensitivity = self.sensitivity.classify(last_user_prompt, touched_file_paths)

        thinking_spec = payload.get("thinking")
        requires_thinking = isinstance(thinking_spec, dict) and thinking_spec.get("type") == "enabled"

        return TurnContext(
            last_user_prompt=last_user_prompt,
            recent_tool_names=recent_tool_names,
            touched_file_paths=touched_file_paths,
            prompt_tokens=prompt_tokens,
            predicted_complexity=complexity,
            predicted_sensitivity=sensitivity,
            requires_thinking=requires_thinking,
            requested_model=requested_model,
        )

    def _estimate_complexity(self, prompt: str, tool_names: List[str]) -> float:
        """Lightweight heuristic complexity estimator (0.0 = simple, 1.0 = hard)."""
        prompt_lower = prompt.lower()

        explore_tools = {"glob", "grep", "search", "read", "view", "ls"}
        is_explore_tool = any(any(e in t.lower() for e in explore_tools) for t in tool_names)
        explore_keywords = ["where is", "find ", "grep ", "search for", "list ", "locate ", "what is", "how do i", "grep for"]
        is_explore_prompt = any(kw in prompt_lower for kw in explore_keywords)

        if (is_explore_tool or is_explore_prompt) and len(prompt) < 250:
            return 0.25

        reasoning_keywords = ["architect", "concurrency", "deadlock", "race condition", "refactor all", "design system"]
        if any(kw in prompt_lower for kw in reasoning_keywords):
            return 0.90

        edit_tools = {"edit", "write", "replace", "patch"}
        is_editing = any(any(e in t.lower() for e in edit_tools) for t in tool_names)
        if is_editing:
            return 0.70

        if len(prompt) > 500 or prompt.count("\n") > 5:
            return 0.80

        return 0.45

    def route_turn(
        self,
        payload: Dict[str, Any],
        session_id: str = "default-session",
    ) -> Tuple[Dict[str, Any], RouteDecision]:
        """Execute the routing pipeline on a turn, mutating the payload if routed."""
        if not self.config.enabled:
            model = str(payload.get("model") or "")
            return payload, RouteDecision(
                target_model=model,
                original_model=model,
                reason="Routing disabled",
                action=TriggerAction.MAINTAIN_ANCHOR,
            )

        try:
            session = self.get_or_create_session(session_id)
            context = self.extract_context(payload)
            orig_model = context.requested_model

            for msg in (payload.get("messages") or [])[-2:]:
                if msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("is_error"):
                                session.consecutive_tool_failures += 1

            trigger_res = self.pipeline.evaluate_turn(context, session)

            target_model = orig_model
            anchor_updated = False

            if trigger_res.action == TriggerAction.MAINTAIN_ANCHOR and session.anchored_model:
                target_model = session.anchored_model

            elif trigger_res.action == TriggerAction.EXPLICIT_OVERRIDE and trigger_res.target_model_hint:
                spec = self.config.get_model_spec(trigger_res.target_model_hint)
                candidate = spec.id if spec else trigger_res.target_model_hint
                if self.config.is_model_allowed(candidate):
                    target_model = candidate
                    session.anchored_model = target_model
                    session.turns_since_last_switch = 0
                    anchor_updated = True
                else:
                    target_model = session.anchored_model or orig_model

            elif trigger_res.action == TriggerAction.SENSITIVITY_ALARM:
                tier_key = context.predicted_sensitivity.name.lower()
                profile = self.active_profile
                candidate = profile.sensitivity_tier_models.get(tier_key, profile.authoring_model)
                if self.config.is_model_allowed(candidate):
                    target_model = candidate
                    session.anchored_model = target_model
                    session.anchored_sensitivity = context.predicted_sensitivity
                    session.turns_since_last_switch = 0
                    anchor_updated = True
                else:
                    target_model = orig_model

            else:
                profile = self.active_profile
                if trigger_res.action == TriggerAction.ESCALATE or context.predicted_complexity >= 0.8:
                    candidate = profile.reasoning_model
                elif context.predicted_complexity <= 0.35:
                    candidate = profile.exploration_model
                else:
                    candidate = profile.authoring_model

                if self.config.is_model_allowed(candidate):
                    target_model = candidate
                    session.anchored_model = target_model
                    session.anchored_complexity = context.predicted_complexity
                    session.anchored_sensitivity = context.predicted_sensitivity
                    session.turns_since_last_switch = 0
                    anchor_updated = True
                else:
                    target_model = orig_model

            session.turn_count += 1
            session.turns_since_last_switch += 1
            if session.consecutive_tool_failures > 0 and trigger_res.action == TriggerAction.ESCALATE:
                session.consecutive_tool_failures = 0

            mutated_payload = dict(payload)
            mutated_payload["model"] = target_model
            stripped_params: List[str] = []
            param_overrides: Dict[str, Any] = {}

            spec = self.config.get_model_spec(target_model)
            if spec:
                if "thinking" in mutated_payload and not spec.supports_extended_thinking:
                    del mutated_payload["thinking"]
                    stripped_params.append("thinking")

                profile = self.active_profile
                if (
                    target_model == profile.reasoning_model
                    and profile.enable_thinking_on_reasoning
                    and spec.supports_extended_thinking
                    and (trigger_res.action == TriggerAction.ESCALATE or context.predicted_complexity >= 0.8)
                ):
                    if "thinking" not in mutated_payload:
                        thinking_budget = min(profile.thinking_budget, 16000)
                        mutated_payload["thinking"] = {
                            "type": "enabled",
                            "budget_tokens": thinking_budget,
                        }
                        param_overrides["thinking"] = mutated_payload["thinking"]

                if "max_tokens" in mutated_payload:
                    max_tok = int(mutated_payload["max_tokens"])
                    if max_tok > spec.max_output_tokens:
                        mutated_payload["max_tokens"] = spec.max_output_tokens
                        param_overrides["max_tokens"] = spec.max_output_tokens

            decision = RouteDecision(
                target_model=target_model,
                original_model=orig_model,
                reason=trigger_res.reason,
                action=trigger_res.action,
                anchor_updated=anchor_updated,
                parameter_overrides=param_overrides,
                stripped_parameters=stripped_params,
            )
            return mutated_payload, decision

        except Exception as e:
            orig_model = str(payload.get("model") or "")
            return payload, RouteDecision(
                target_model=orig_model,
                original_model=orig_model,
                reason=f"Routing fail-safe fallback: {e}",
                action=TriggerAction.MAINTAIN_ANCHOR,
            )
