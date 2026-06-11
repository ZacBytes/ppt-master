#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Loop

Runs the OpenRouter model, dispatches local tools, and persists progress.

Dependencies:
    None
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .config import AgentConfig
from .openrouter import OpenRouterClient
from .prompts import build_system_prompt
from .security import redact_text
from .session import SessionStore
from .tools import ToolRegistry


class PPTMasterAgent:
    """Persistent OpenRouter-backed PPT Master agent."""

    def __init__(
        self,
        *,
        repo_root: Path,
        config: AgentConfig,
        session_directory: Path,
    ):
        self.repo_root = repo_root.resolve()
        self.config = config
        self.session = SessionStore(session_directory)
        self.state = self.session.load_state(
            image_mode=config.image_mode,
            model=config.model,
        )
        self.state.save(self.session.state_path)
        self.client = OpenRouterClient(config)
        self.tools = ToolRegistry(
            repo_root=self.repo_root,
            config=config,
            state=self.state,
            session=self.session,
        )

    def run_turn(
        self,
        user_text: str,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        stop_after_svg: bool = False,
    ) -> str:
        self.config.require_api_key()
        self.tools.begin_turn(user_text)
        self.session.messages.append({
            "role": "user",
            "content": redact_text(user_text),
        })
        transient_messages: list[dict[str, Any]] = []
        for _round in range(self.config.max_tool_rounds):
            messages = [
                {
                    "role": "system",
                    "content": build_system_prompt(self.repo_root, self.state),
                },
                *self._recent_messages(),
                *transient_messages,
            ]
            transient_messages = []
            if on_event:
                stream_started = False

                def stream_delta(text: str) -> None:
                    nonlocal stream_started
                    if not stream_started:
                        on_event("assistant_start", {})
                        stream_started = True
                    on_event("assistant_delta", {"text": text})

                response = self.client.stream_complete(
                    messages,
                    self.tools.schemas(),
                    stream_delta,
                )
            else:
                response = self.client.complete(messages, self.tools.schemas())
            self.session.usage.add(response.get("usage"))
            message = response["choices"][0]["message"]
            assistant_message = {
                "role": "assistant",
                "content": redact_text(message.get("content") or ""),
            }
            if message.get("tool_calls"):
                assistant_message["tool_calls"] = message["tool_calls"]
            self.session.messages.append(assistant_message)
            if on_event and assistant_message["content"]:
                on_event("assistant_done", {
                    "content": assistant_message["content"],
                })
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                self._sync_confirmation_gate(assistant_message["content"])
                self.session.save_messages()
                self.session.save_usage()
                return assistant_message["content"]
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                name = function.get("name", "")
                switch_project = None
                wrote_svg = False
                if on_event:
                    on_event("tool_start", {"name": name})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    result = self.tools.call(name, arguments)
                    image_data_url = result.pop("__image_data_url", None)
                    switch_project = result.pop("__switch_project", None)
                    content = json.dumps(
                        result,
                        ensure_ascii=False,
                    )
                    wrote_svg = (
                        name == "write_file"
                        and str(arguments.get("path", "")).lower().endswith(".svg")
                        and "error" not in result
                    )
                    if image_data_url:
                        transient_messages.append({
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Inspect this local project image and "
                                        "continue the current task."
                                    ),
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image_data_url},
                                },
                            ],
                        })
                except Exception as exc:
                    content = json.dumps({
                        "error": redact_text(str(exc)),
                    }, ensure_ascii=False)
                self.session.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", ""),
                    "name": name,
                    "content": content,
                })
                if on_event:
                    on_event("tool_done", {"name": name})
                if switch_project:
                    self.switch_project(
                        Path(switch_project),
                        preserve_messages=True,
                    )
                if stop_after_svg and wrote_svg:
                    self.session.save_messages()
                    self.session.save_usage()
                    return f"Generated {arguments.get('path')}."
            self.session.save_messages()
            self.session.save_usage()
        raise RuntimeError(
            "Agent exceeded the maximum tool rounds for one turn. "
            "Use /resume to continue."
        )

    def status_text(self) -> str:
        status = self.tools.call("get_status", {})
        return (
            f"Phase: {status['phase']}\n"
            f"Project: {status['project_path'] or 'not selected'}\n"
            f"Model: {status['model']}\n"
            f"Image generation: {status['image_mode']}\n"
            f"Web search: unavailable\n"
            f"Tokens: {status['usage']['total_tokens']}"
        )

    def clear_conversation(self) -> None:
        self.session.messages = []
        self.session.save_messages()

    def switch_project(
        self,
        project_path: Path,
        *,
        preserve_messages: bool = False,
    ) -> None:
        """Switch persistence and workflow state to an existing project."""
        old_session = self.session
        old_state = self.state
        new_session = SessionStore(project_path / ".agent")
        has_saved_state = new_session.state_path.is_file()
        if preserve_messages and not new_session.messages:
            new_session.messages = list(old_session.messages)
            new_session.usage = old_session.usage
            new_session.save_messages()
            new_session.save_usage()
        self.session = new_session
        if has_saved_state:
            self.state = self.session.load_state(
                image_mode=self.config.image_mode,
                model=self.config.model,
            )
        else:
            self.state = old_state
        self.state.project_path = str(project_path.resolve())
        self.state.save(self.session.state_path)
        self.tools = ToolRegistry(
            repo_root=self.repo_root,
            config=self.config,
            state=self.state,
            session=self.session,
        )

    def _recent_messages(self) -> list[dict[str, Any]]:
        recent = []
        total_chars = 0
        for raw_message in reversed(self.session.messages[-80:]):
            message = dict(raw_message)
            content = message.get("content")
            if isinstance(content, str) and len(content) > 8000:
                message["content"] = (
                    content[:8000]
                    + "\n[Tool output truncated from conversation context.]"
                )
            message_chars = len(str(message.get("content", "")))
            if recent and total_chars + message_chars > 90000:
                break
            recent.append(message)
            total_chars += message_chars
        recent.reverse()
        while recent and recent[0].get("role") == "tool":
            recent = recent[1:]
        return recent

    def _sync_confirmation_gate(self, content: str) -> None:
        """Persist the confirmation gate when the agent asks for approval."""
        lowered = content.lower()
        asks_for_confirmation = (
            "please confirm" in lowered
            or "confirm these" in lowered
            or "eight confirmations" in lowered
        )
        if not asks_for_confirmation or not self.state.project_path:
            return
        while self.state.phase in {
            "source_input",
            "project_initialized",
            "template_resolved",
        }:
            next_phase = {
                "source_input": "project_initialized",
                "project_initialized": "template_resolved",
                "template_resolved": "awaiting_confirmations",
            }[self.state.phase]
            self.state.transition(next_phase)
        self.state.save(self.session.state_path)
