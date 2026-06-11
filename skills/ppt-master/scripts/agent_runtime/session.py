#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Session Store

Persists redacted messages, tool events, usage, and workflow state.

Dependencies:
    None
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from .security import atomic_write_json, redact_value
from .workflow import WorkflowState


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, raw: dict[str, Any] | None) -> None:
        if not raw:
            return
        self.prompt_tokens += int(raw.get("prompt_tokens", 0))
        self.completion_tokens += int(raw.get("completion_tokens", 0))
        self.total_tokens += int(raw.get("total_tokens", 0))


class SessionStore:
    """JSON/JSONL-backed session persistence."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.messages_path = directory / "messages.json"
        self.events_path = directory / "tool_events.jsonl"
        self.state_path = directory / "state.json"
        self.usage_path = directory / "usage.json"
        self.messages: list[dict[str, Any]] = self._load_json(
            self.messages_path,
            [],
        )
        self.usage = Usage(**self._load_json(self.usage_path, {}))

    def load_state(self, *, image_mode: str, model: str) -> WorkflowState:
        raw = self._load_json(self.state_path, None)
        if raw:
            state = WorkflowState.from_dict(raw)
            state.image_mode = image_mode
            state.model = model
            return state
        return WorkflowState(image_mode=image_mode, model=model)

    def save_messages(self) -> None:
        atomic_write_json(self.messages_path, self.messages)

    def save_usage(self) -> None:
        atomic_write_json(self.usage_path, asdict(self.usage))

    def append_event(self, event: dict[str, Any]) -> None:
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(redact_value(event), ensure_ascii=False) + "\n"
            )

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError):
            return default
