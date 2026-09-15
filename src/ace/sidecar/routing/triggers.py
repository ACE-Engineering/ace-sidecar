"""ace.sidecar.routing.triggers — Pluggable triggering abstraction layer for model routing."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import List, Optional

from ace.sidecar.routing.config import TriggerConfig
from ace.sidecar.routing.types import (
    SessionState,
    TriggerAction,
    TriggerResult,
    TurnContext,
)


class RoutingTrigger(ABC):
    """Base interface for all routing triggers."""

    @abstractmethod
    def evaluate(self, context: TurnContext, session: SessionState) -> TriggerResult:
        """Evaluate whether this trigger should cause an intervention."""
        pass


class ExplicitPromptTrigger(RoutingTrigger):
    """Detects explicit natural language model directives in user prompt."""

    DEFAULT_PATTERNS = [
        re.compile(r"(?:use|switch to|route to)\s+([a-zA-Z0-9\.\-_]+)", re.IGNORECASE),
        re.compile(r"/model\s+([a-zA-Z0-9\.\-_]+)", re.IGNORECASE),
        re.compile(r"@([a-zA-Z0-9\.\-_]+)", re.IGNORECASE),
    ]

    def __init__(self, custom_patterns: Optional[List[str]] = None) -> None:
        self.patterns = (
            [re.compile(p, re.IGNORECASE) for p in custom_patterns]
            if custom_patterns
            else self.DEFAULT_PATTERNS
        )

    def evaluate(self, context: TurnContext, session: SessionState) -> TriggerResult:
        prompt = context.last_user_prompt
        if not prompt:
            return TriggerResult(action=TriggerAction.MAINTAIN_ANCHOR, reason="Empty prompt")

        for pat in self.patterns:
            m = pat.search(prompt)
            if m:
                model_hint = m.group(1).lower().strip()
                if model_hint in ("opus", "sonnet", "haiku", "claude-3-opus", "claude-3-5-sonnet", "claude-3-7-sonnet", "claude-3-5-haiku"):
                    return TriggerResult(
                        action=TriggerAction.EXPLICIT_OVERRIDE,
                        reason=f"Explicit user model override matching pattern: {m.group(0)}",
                        target_model_hint=model_hint,
                    )
                if model_hint.startswith("claude") or model_hint.startswith("anthropic"):
                    return TriggerResult(
                        action=TriggerAction.EXPLICIT_OVERRIDE,
                        reason=f"Explicit user model override matching pattern: {m.group(0)}",
                        target_model_hint=model_hint,
                    )

        if "think deeply" in prompt.lower() or "extended thinking" in prompt.lower():
            return TriggerResult(
                action=TriggerAction.ESCALATE,
                reason="User explicitly requested deep thinking",
                target_model_hint="claude-3-7-sonnet-20250219",
                metadata={"enable_thinking": True},
            )

        return TriggerResult(action=TriggerAction.MAINTAIN_ANCHOR, reason="No explicit user command")


class SensitivityEscalationTrigger(RoutingTrigger):
    """Triggers immediate intervention if newly touched data exceeds session clearance."""

    def evaluate(self, context: TurnContext, session: SessionState) -> TriggerResult:
        if context.predicted_sensitivity > session.anchored_sensitivity:
            return TriggerResult(
                action=TriggerAction.SENSITIVITY_ALARM,
                reason=(
                    f"Turn introduced {context.predicted_sensitivity.name} data, exceeding "
                    f"current session clearance ({session.anchored_sensitivity.name})"
                ),
                metadata={"sensitivity_level": context.predicted_sensitivity.value},
            )
        return TriggerResult(action=TriggerAction.MAINTAIN_ANCHOR, reason="Sensitivity within clearance")


class ComplexityJumpTrigger(RoutingTrigger):
    """Triggers escalation when task complexity delta exceeds configured threshold."""

    def __init__(self, delta_threshold: float = 0.40, cooldown_turns: int = 1) -> None:
        self.delta_threshold = delta_threshold
        self.cooldown_turns = cooldown_turns

    def evaluate(self, context: TurnContext, session: SessionState) -> TriggerResult:
        delta = context.predicted_complexity - session.anchored_complexity
        if delta >= self.delta_threshold and session.turns_since_last_switch >= self.cooldown_turns:
            return TriggerResult(
                action=TriggerAction.ESCALATE,
                reason=(
                    f"Task complexity jumped by +{delta:.2f} "
                    f"(from {session.anchored_complexity:.2f} to {context.predicted_complexity:.2f}, "
                    f"threshold: {self.delta_threshold:.2f})"
                ),
                metadata={"delta_complexity": delta},
            )
        return TriggerResult(action=TriggerAction.MAINTAIN_ANCHOR, reason="Complexity stable")


class ConsecutiveFailureTrigger(RoutingTrigger):
    """Triggers escalation after repeated tool / test execution failures."""

    def __init__(self, failure_threshold: int = 2) -> None:
        self.failure_threshold = failure_threshold

    def evaluate(self, context: TurnContext, session: SessionState) -> TriggerResult:
        if session.consecutive_tool_failures >= self.failure_threshold:
            return TriggerResult(
                action=TriggerAction.ESCALATE,
                reason=(
                    f"Agent encountered {session.consecutive_tool_failures} consecutive failures "
                    f"(threshold: {self.failure_threshold}); escalating to flagship reasoning model"
                ),
                metadata={"consecutive_failures": session.consecutive_tool_failures},
            )
        return TriggerResult(action=TriggerAction.MAINTAIN_ANCHOR, reason="Failure count below threshold")


class CacheExpiryTrigger(RoutingTrigger):
    """Triggers re-evaluation if prompt cache expired due to idle inactivity (>300s)."""

    def __init__(self, idle_seconds_threshold: float = 300.0) -> None:
        self.idle_seconds_threshold = idle_seconds_threshold

    def evaluate(self, context: TurnContext, session: SessionState) -> TriggerResult:
        if session.idle_seconds >= self.idle_seconds_threshold:
            return TriggerResult(
                action=TriggerAction.INITIALIZE,
                reason=(
                    f"Prompt cache expired after {session.idle_seconds:.1f}s idle "
                    f"(threshold: {self.idle_seconds_threshold}s); re-evaluating cost-optimal destination"
                ),
                metadata={"idle_seconds": session.idle_seconds},
            )
        return TriggerResult(action=TriggerAction.MAINTAIN_ANCHOR, reason="Prompt cache is warm")


class TriggerPipeline:
    """Composite pipeline executing configured triggers in priority order."""

    TRIGGER_REGISTRY = {
        "explicit_prompt": ExplicitPromptTrigger,
        "sensitivity_escalation": SensitivityEscalationTrigger,
        "complexity_jump": ComplexityJumpTrigger,
        "consecutive_failures": ConsecutiveFailureTrigger,
        "cache_expiry": CacheExpiryTrigger,
    }

    def __init__(self, triggers: Optional[List[RoutingTrigger]] = None) -> None:
        self.triggers: List[RoutingTrigger] = triggers if triggers is not None else [
            SensitivityEscalationTrigger(),
            ExplicitPromptTrigger(),
            ConsecutiveFailureTrigger(),
            ComplexityJumpTrigger(),
            CacheExpiryTrigger(),
        ]

    @classmethod
    def from_configs(cls, configs: List[TriggerConfig]) -> "TriggerPipeline":
        """Instantiate pipeline from declarative TriggerConfig list."""
        triggers: List[RoutingTrigger] = []
        for cfg in configs:
            if not cfg.enabled:
                continue
            trigger_cls = cls.TRIGGER_REGISTRY.get(cfg.type)
            if trigger_cls is None:
                continue
            triggers.append(trigger_cls(**cfg.params))
        return cls(triggers)

    def evaluate_turn(self, context: TurnContext, session: SessionState) -> TriggerResult:
        """Evaluate all triggers in order; first intervention wins. Defaults to MAINTAIN_ANCHOR."""
        # 1. Check priority override/security triggers even on turn 1
        for trigger in self.triggers:
            if isinstance(trigger, (ExplicitPromptTrigger, SensitivityEscalationTrigger)):
                res = trigger.evaluate(context, session)
                if res.action != TriggerAction.MAINTAIN_ANCHOR:
                    return res

        # 2. Turn 1 always triggers INITIALIZE if no priority trigger fired
        if session.turn_count == 0 or not session.anchored_model:
            return TriggerResult(
                action=TriggerAction.INITIALIZE,
                reason="Session initialization: determining initial destination model",
            )

        # 3. Evaluate all active triggers in priority order
        for trigger in self.triggers:
            result = trigger.evaluate(context, session)
            if result.action != TriggerAction.MAINTAIN_ANCHOR:
                return result

        # 4. Default: maintain session stickiness
        return TriggerResult(
            action=TriggerAction.MAINTAIN_ANCHOR,
            reason="All triggers nominal; preserving session model anchor and prompt cache",
        )
