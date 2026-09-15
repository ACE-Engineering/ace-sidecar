"""ace.sidecar.routing.types — Domain types, enums, and contexts for model routing."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SensitivityLevel(IntEnum):
    """Data sensitivity classification hierarchy."""
    PUBLIC = 1        # Open-source, public code, general questions
    INTERNAL = 2      # Standard internal corporate code, business logic
    CONFIDENTIAL = 3  # Sensitive IP, internal customer identifiers, financial code
    RESTRICTED = 4    # Secrets, credentials, private keys, PII, tokens


class TriggerAction(StrEnum):
    """Actions emitted by the routing trigger pipeline."""
    MAINTAIN_ANCHOR = "maintain_anchor"    # Keep current session model anchor (zero overhead)
    INITIALIZE = "initialize"              # Turn 1: compute initial destination model
    EXPLICIT_OVERRIDE = "explicit_override"# User specifically instructed a model change
    ESCALATE = "escalate"                  # Significant complexity jump or error recovery
    DOWNROUTE = "downroute"                # Downroute to high-economy model
    SENSITIVITY_ALARM = "sensitivity_alarm"# Immediate quarantine/compliance boundary enforcement


class TriggerResult(BaseModel):
    """Outcome of evaluating the routing trigger pipeline on a turn."""
    action: TriggerAction
    reason: str
    target_model_hint: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TurnContext(BaseModel):
    """Extracted features and telemetry from the incoming turn request."""
    last_user_prompt: str = ""
    recent_tool_names: List[str] = Field(default_factory=list)
    touched_file_paths: List[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    predicted_complexity: float = 0.5
    predicted_sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    requires_thinking: bool = False
    requested_model: str = ""


class SessionState(BaseModel):
    """In-memory session state tracking for sticky model anchoring."""
    session_id: str
    anchored_model: Optional[str] = None
    anchored_sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL
    anchored_complexity: float = 0.5
    turn_count: int = 0
    turns_since_last_switch: int = 0
    consecutive_tool_failures: int = 0
    last_turn_timestamp: float = 0.0
    idle_seconds: float = 0.0


class RouteDecision(BaseModel):
    """Final decision produced by the model router."""
    target_model: str
    original_model: str
    reason: str
    action: TriggerAction
    anchor_updated: bool = False
    parameter_overrides: Dict[str, Any] = Field(default_factory=dict)
    stripped_parameters: List[str] = Field(default_factory=list)
