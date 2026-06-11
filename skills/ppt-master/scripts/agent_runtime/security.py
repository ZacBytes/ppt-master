#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Security Helpers

Confines file access, redacts secrets, and validates tool arguments.

Dependencies:
    None
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


_SECRET_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(api[_-]?key|authorization)([\"'\s:=]+)([^\s,\"']+)"),
]


def redact_text(value: str) -> str:
    """Remove common API-key representations from a string."""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    """Recursively redact secrets from JSON-compatible values."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in {
                "api_key",
                "authorization",
                "access_token",
                "refresh_token",
                "secret",
            }
            else redact_value(item)
            for key, item in value.items()
        }
    return value


def resolve_safe_path(
    raw_path: str,
    *,
    repo_root: Path,
    project_path: Path | None = None,
    must_exist: bool = False,
) -> Path:
    """Resolve a path and ensure it remains inside an allowed root."""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    candidate = candidate.resolve()
    roots = [repo_root.resolve()]
    if project_path is not None:
        roots.append(project_path.resolve())
    if not any(candidate == root or root in candidate.parents for root in roots):
        raise ValueError(f"Path is outside the PPT Master workspace: {raw_path}")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(f"Path does not exist: {candidate}")
    return candidate


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text atomically in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    content = json.dumps(
        redact_value(value),
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_text(path, content + "\n")
