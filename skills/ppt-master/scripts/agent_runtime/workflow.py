#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Workflow State

Enforces the serial PPT Master pipeline and its blocking confirmation gate.

Dependencies:
    None
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .security import atomic_write_json


PHASES = [
    "source_input",
    "project_initialized",
    "template_resolved",
    "awaiting_confirmations",
    "strategy_locked",
    "assets_ready",
    "executing_slides",
    "quality_checked",
    "post_processing",
    "exported",
]


@dataclass
class WorkflowState:
    """Durable state for one built-in-agent session."""

    phase: str = "source_input"
    project_path: str | None = None
    confirmations_approved: bool = False
    current_slide: int = 0
    post_process_index: int = -1
    image_mode: str = "disabled"
    model: str = ""
    updated_at: str = field(default_factory=lambda: _now())
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkflowState":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        values = {key: value for key, value in raw.items() if key in allowed}
        state = cls(**values)
        if state.phase not in PHASES:
            raise ValueError(f"Unknown workflow phase: {state.phase}")
        return state

    def transition(self, target: str) -> None:
        if target not in PHASES:
            raise ValueError(f"Unknown workflow phase: {target}")
        current_index = PHASES.index(self.phase)
        target_index = PHASES.index(target)
        if target_index == current_index:
            return
        if target_index != current_index + 1:
            raise ValueError(
                f"Invalid phase transition: {self.phase} -> {target}. "
                f"Next allowed phase: {PHASES[current_index + 1]}"
            )
        if target == "strategy_locked" and not self.confirmations_approved:
            raise ValueError(
                "The Eight Confirmations must be explicitly approved before "
                "strategy_locked."
            )
        self.phase = target
        self.updated_at = _now()

    def approve_confirmations(self) -> None:
        if self.phase != "awaiting_confirmations":
            raise ValueError(
                "Confirmations can only be approved during "
                "awaiting_confirmations."
            )
        self.confirmations_approved = True
        self.updated_at = _now()

    def save(self, path: Path) -> None:
        atomic_write_json(path, asdict(self))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
