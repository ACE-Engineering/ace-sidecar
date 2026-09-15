"""ace.sidecar.routing — Local model routing package for ACE Sidecar and Claude CLI."""

from __future__ import annotations

from ace.sidecar.routing.config import (
    DEFAULT_CLAUDE_ALLOWED_MODELS,
    DEFAULT_CLAUDE_PATTERNS,
    DeploymentRoutingConfig,
    ModelSpec,
    ProfileConfig,
    TriggerConfig,
    load_routing_config,
)
from ace.sidecar.routing.engine import ModelRouter
from ace.sidecar.routing.router import SidecarRouter
from ace.sidecar.routing.sensitivity import SensitivityClassifier
from ace.sidecar.routing.triggers import (
    CacheExpiryTrigger,
    ComplexityJumpTrigger,
    ConsecutiveFailureTrigger,
    ExplicitPromptTrigger,
    RoutingTrigger,
    SensitivityEscalationTrigger,
    TriggerPipeline,
)
from ace.sidecar.routing.types import (
    RouteDecision,
    SensitivityLevel,
    SessionState,
    TriggerAction,
    TriggerResult,
    TurnContext,
)

__all__ = [
    "DEFAULT_CLAUDE_ALLOWED_MODELS",
    "DEFAULT_CLAUDE_PATTERNS",
    "CacheExpiryTrigger",
    "ComplexityJumpTrigger",
    "ConsecutiveFailureTrigger",
    "DeploymentRoutingConfig",
    "ExplicitPromptTrigger",
    "ModelRouter",
    "ModelSpec",
    "ProfileConfig",
    "RouteDecision",
    "RoutingTrigger",
    "SensitivityClassifier",
    "SensitivityEscalationTrigger",
    "SensitivityLevel",
    "SessionState",
    "SidecarRouter",
    "TriggerAction",
    "TriggerConfig",
    "TriggerPipeline",
    "TriggerResult",
    "TurnContext",
    "load_routing_config",
]
