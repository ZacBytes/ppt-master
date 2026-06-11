#!/usr/bin/env python3
"""
PPT Master - OpenRouter Client

Calls the OpenAI-compatible chat-completions endpoint with retry handling.

Dependencies:
    None
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from .config import AgentConfig
from .security import redact_text


class OpenRouterClient:
    """Minimal OpenRouter chat-completions client."""

    def __init__(self, config: AgentConfig):
        self.config = config

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/hugohe3/ppt-master",
                "X-Title": "PPT Master Built-in Agent",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.config.request_timeout,
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not result.get("choices"):
                    raise RuntimeError("OpenRouter returned no choices.")
                return result
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                message = redact_text(body[:2000])
                if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise RuntimeError(
                        f"OpenRouter HTTP {exc.code}: {message}"
                    ) from exc
                last_error = RuntimeError(
                    f"OpenRouter HTTP {exc.code}: {message}"
                )
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < 3:
                time.sleep((2 ** attempt) + random.random())
        raise RuntimeError(
            f"OpenRouter request failed after retries: "
            f"{redact_text(str(last_error))}"
        ) from last_error

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Callable[[str], None],
    ) -> dict[str, Any]:
        """Stream one completion and return an assembled response."""
        payload = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/hugohe3/ppt-master",
                "X-Title": "PPT Master Built-in Agent",
            },
            method="POST",
        )
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        with urllib.request.urlopen(
            request,
            timeout=self.config.request_timeout,
        ) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise RuntimeError(
                        redact_text(json.dumps(chunk["error"]))[:2000]
                    )
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    on_delta(text)
                for raw_call in delta.get("tool_calls") or []:
                    index = int(raw_call.get("index", 0))
                    call = tool_calls.setdefault(index, {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if raw_call.get("id"):
                        call["id"] = raw_call["id"]
                    if raw_call.get("type"):
                        call["type"] = raw_call["type"]
                    function = raw_call.get("function") or {}
                    call["function"]["name"] += function.get("name") or ""
                    call["function"]["arguments"] += (
                        function.get("arguments") or ""
                    )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        if tool_calls:
            message["tool_calls"] = [
                tool_calls[index] for index in sorted(tool_calls)
            ]
        return {"choices": [{"message": message}], "usage": usage}
