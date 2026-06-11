#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Configuration

Loads OpenRouter and built-in agent settings without persisting secrets.

Dependencies:
    None
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


VALID_IMAGE_MODES = {"disabled", "prompts-only", "enabled"}
DEFAULT_MODEL = "google/gemma-4-31b-it"
DEFAULT_BASE_URL = "https://api.novita.ai/v3/openai"


def _load_agent_env(repo_root: Path) -> Path | None:
    candidates = [
        Path.cwd() / ".env",
        repo_root / ".env",
        Path.home() / ".ppt-master" / ".env",
    ]
    prefixes = ("NOVITA_", "OPENROUTER_", "PPT_MASTER_AGENT_", "PPT_MASTER_IMAGE_", "IMAGE_")
    for path in candidates:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key.startswith(prefixes):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key, value)
        return path
    return None


@dataclass(frozen=True)
class AgentConfig:
    """Runtime configuration for the built-in agent."""

    api_key: str
    model: str
    base_url: str
    image_mode: str
    max_tokens: int
    temperature: float
    request_timeout: int
    max_tool_rounds: int

    @classmethod
    def load(cls, repo_root: Path) -> "AgentConfig":
        _load_agent_env(repo_root)
        image_mode = os.environ.get(
            "PPT_MASTER_IMAGE_GENERATION",
            "disabled",
        ).strip().lower()
        if image_mode not in VALID_IMAGE_MODES:
            choices = ", ".join(sorted(VALID_IMAGE_MODES))
            raise ValueError(
                f"PPT_MASTER_IMAGE_GENERATION must be one of: {choices}"
            )
        return cls(
            api_key=(
                os.environ.get("NOVITA_API_KEY", "").strip()
                or os.environ.get("OPENROUTER_API_KEY", "").strip()
            ),
            model=os.environ.get(
                "PPT_MASTER_AGENT_MODEL",
                DEFAULT_MODEL,
            ).strip(),
            base_url=os.environ.get(
                "PPT_MASTER_AGENT_BASE_URL",
                os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL),
            ).rstrip("/"),
            image_mode=image_mode,
            max_tokens=int(os.environ.get("PPT_MASTER_AGENT_MAX_TOKENS", "16384")),
            temperature=float(
                os.environ.get("PPT_MASTER_AGENT_TEMPERATURE", "0.7")
            ),
            request_timeout=int(
                os.environ.get("PPT_MASTER_AGENT_TIMEOUT", "120")
            ),
            max_tool_rounds=int(
                os.environ.get("PPT_MASTER_AGENT_MAX_TOOL_ROUNDS", "40")
            ),
        )

    def require_api_key(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "NOVITA_API_KEY is not set. Add it to the process "
                "environment or an ignored .env file."
            )
