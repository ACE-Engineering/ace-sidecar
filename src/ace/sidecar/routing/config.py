"""ace.sidecar.routing.config — Declarative configuration models and file loader for model routing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from ace.sidecar.routing.types import SensitivityLevel
from pydantic import BaseModel, Field

DEFAULT_CLAUDE_ALLOWED_MODELS = [
    "claude-3-5-haiku-20241022",
    "claude-3-5-haiku-latest",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-latest",
    "claude-3-7-sonnet-20250219",
    "claude-3-7-sonnet-latest",
    "claude-3-opus-20240229",
    "claude-opus-4-1",
]

DEFAULT_CLAUDE_PATTERNS = [
    r"^claude-.*",
    r"^anthropic\.claude-.*",
]


class ModelSpec(BaseModel):
    """Metadata and capabilities for an admissible model."""
    id: str
    alias: Optional[str] = None
    family: str = "anthropic"
    tier: Literal["economy", "standard", "flagship"] = "standard"
    input_cost_per_1m: float = 3.00
    output_cost_per_1m: float = 15.00
    cache_read_cost_per_1m: float = 0.30
    cache_write_cost_per_1m: float = 3.75
    supports_extended_thinking: bool = False
    max_output_tokens: int = 8192
    min_sensitivity_level: SensitivityLevel = SensitivityLevel.PUBLIC
    max_sensitivity_level: SensitivityLevel = SensitivityLevel.RESTRICTED
    quality_scores: Dict[str, float] = Field(default_factory=dict)


def default_model_specs() -> Dict[str, ModelSpec]:
    """Default specifications for canonical Claude models."""
    return {
        "claude-3-5-haiku-20241022": ModelSpec(
            id="claude-3-5-haiku-20241022",
            alias="haiku",
            family="anthropic",
            tier="economy",
            input_cost_per_1m=0.80,
            output_cost_per_1m=4.00,
            cache_read_cost_per_1m=0.08,
            cache_write_cost_per_1m=1.00,
            supports_extended_thinking=False,
            max_output_tokens=8192,
            quality_scores={"explore": 0.88, "author": 0.78, "reasoning": 0.70},
        ),
        "claude-3-5-sonnet-20241022": ModelSpec(
            id="claude-3-5-sonnet-20241022",
            alias="sonnet-3.5",
            family="anthropic",
            tier="standard",
            input_cost_per_1m=3.00,
            output_cost_per_1m=15.00,
            cache_read_cost_per_1m=0.30,
            cache_write_cost_per_1m=3.75,
            supports_extended_thinking=False,
            max_output_tokens=8192,
            quality_scores={"explore": 0.92, "author": 0.95, "reasoning": 0.92},
        ),
        "claude-3-7-sonnet-20250219": ModelSpec(
            id="claude-3-7-sonnet-20250219",
            alias="sonnet-3.7",
            family="anthropic",
            tier="standard",
            input_cost_per_1m=3.00,
            output_cost_per_1m=15.00,
            cache_read_cost_per_1m=0.30,
            cache_write_cost_per_1m=3.75,
            supports_extended_thinking=True,
            max_output_tokens=64000,
            quality_scores={"explore": 0.94, "author": 0.97, "reasoning": 0.96},
        ),
        "claude-3-opus-20240229": ModelSpec(
            id="claude-3-opus-20240229",
            alias="opus",
            family="anthropic",
            tier="flagship",
            input_cost_per_1m=15.00,
            output_cost_per_1m=75.00,
            cache_read_cost_per_1m=1.50,
            cache_write_cost_per_1m=18.75,
            supports_extended_thinking=False,
            max_output_tokens=4096,
            quality_scores={"explore": 0.93, "author": 0.96, "reasoning": 0.97},
        ),
    }


class TriggerConfig(BaseModel):
    """Configuration for a pluggable routing trigger in the pipeline."""
    type: str
    enabled: bool = True
    params: Dict[str, Any] = Field(default_factory=dict)


class ProfileConfig(BaseModel):
    """A named routing profile defining tier mappings and trigger configurations."""
    name: str = "balanced"
    description: str = "Balanced profile with sticky session anchoring and cache retention"
    exploration_model: str = "claude-3-5-haiku-20241022"
    authoring_model: str = "claude-3-7-sonnet-20250219"
    reasoning_model: str = "claude-3-7-sonnet-20250219"
    enable_thinking_on_reasoning: bool = True
    thinking_budget: int = 4096
    triggers: List[TriggerConfig] = Field(default_factory=lambda: [
        TriggerConfig(type="sensitivity_escalation", enabled=True),
        TriggerConfig(type="explicit_prompt", enabled=True),
        TriggerConfig(type="consecutive_failures", enabled=True, params={"failure_threshold": 2}),
        TriggerConfig(type="complexity_jump", enabled=True, params={"delta_threshold": 0.40, "cooldown_turns": 1}),
        TriggerConfig(type="cache_expiry", enabled=True, params={"idle_seconds_threshold": 300.0}),
    ])
    sensitivity_tier_models: Dict[str, str] = Field(default_factory=lambda: {
        "public": "claude-3-5-haiku-20241022",
        "internal": "claude-3-5-haiku-20241022",
        "confidential": "claude-3-7-sonnet-20250219",
        "restricted": "claude-3-7-sonnet-20250219",
    })


def default_profiles() -> Dict[str, ProfileConfig]:
    """Default profiles: balanced, economy, flagship."""
    return {
        "balanced": ProfileConfig(
            name="balanced",
            description="Phase-aware routing with sticky session anchoring and cache retention",
            exploration_model="claude-3-5-haiku-20241022",
            authoring_model="claude-3-7-sonnet-20250219",
            reasoning_model="claude-3-7-sonnet-20250219",
            enable_thinking_on_reasoning=True,
            thinking_budget=4096,
        ),
        "economy": ProfileConfig(
            name="economy",
            description="Aggressive token cost minimization",
            exploration_model="claude-3-5-haiku-20241022",
            authoring_model="claude-3-5-haiku-20241022",
            reasoning_model="claude-3-7-sonnet-20250219",
            enable_thinking_on_reasoning=False,
            thinking_budget=0,
        ),
        "flagship": ProfileConfig(
            name="flagship",
            description="Maximum capability passthrough with extended thinking",
            exploration_model="claude-3-7-sonnet-20250219",
            authoring_model="claude-3-7-sonnet-20250219",
            reasoning_model="claude-3-7-sonnet-20250219",
            enable_thinking_on_reasoning=True,
            thinking_budget=8192,
        ),
    }


class DeploymentRoutingConfig(BaseModel):
    """Customer-facing deployment configuration for model routing."""
    version: str = "1.0"
    enabled: bool = True
    allowed_models: List[str] = Field(default_factory=lambda: list(DEFAULT_CLAUDE_ALLOWED_MODELS))
    allowed_model_patterns: List[str] = Field(default_factory=lambda: list(DEFAULT_CLAUDE_PATTERNS))
    models: Dict[str, ModelSpec] = Field(default_factory=default_model_specs)
    active_profile: str = "balanced"
    profiles: Dict[str, ProfileConfig] = Field(default_factory=default_profiles)
    fallback_behavior: Literal["passthrough", "closest_tier", "error"] = "passthrough"

    def is_model_allowed(self, model_id: str) -> bool:
        """Verify whether a model ID is allowed by exact match or pattern."""
        if model_id in self.allowed_models:
            return True
        for pattern in self.allowed_model_patterns:
            if re.match(pattern, model_id):
                return True
        return False

    def get_model_spec(self, model_id: str) -> Optional[ModelSpec]:
        """Look up model spec by ID or alias."""
        if model_id in self.models:
            return self.models[model_id]
        for spec in self.models.values():
            if spec.alias and spec.alias.lower() == model_id.lower():
                return spec
        for k, spec in self.models.items():
            if k.startswith(model_id) or model_id.startswith(k):
                return spec
        return None


def load_routing_config(config_path: Optional[Path] = None) -> DeploymentRoutingConfig:
    """Load routing config from custom path, ~/.ace/routing.yaml, or defaults."""
    candidates = []
    if config_path:
        candidates.append(config_path)
    # User config
    home = Path.home()
    candidates.append(home / ".ace" / "routing.yaml")
    candidates.append(home / ".ace" / "routing.yml")
    # Current workspace config
    cwd = Path.cwd()
    candidates.append(cwd / ".ace" / "routing.yaml")
    candidates.append(cwd / "ace-routing.yaml")

    for path in candidates:
        if path.exists() and path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                    if isinstance(data, dict):
                        return DeploymentRoutingConfig.model_validate(data)
            except Exception:
                pass

    return DeploymentRoutingConfig()
