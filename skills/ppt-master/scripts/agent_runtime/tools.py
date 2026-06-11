#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Tools

Provides repository-confined file tools and an allowlisted PPT script runner.

Dependencies:
    None
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request

from .config import AgentConfig
from .security import (
    atomic_write_text,
    file_sha256,
    redact_text,
    resolve_safe_path,
)
from .session import SessionStore
from .workflow import WorkflowState


MAX_TOOL_OUTPUT = 30000
MAX_READ_CHARS = 16000

SCRIPT_MAP = {
    "project_manager": "project_manager.py",
    "pdf_to_md": "source_to_md/pdf_to_md.py",
    "doc_to_md": "source_to_md/doc_to_md.py",
    "excel_to_md": "source_to_md/excel_to_md.py",
    "ppt_to_md": "source_to_md/ppt_to_md.py",
    "analyze_images": "analyze_images.py",
    "latex_render": "latex_render.py",
    "image_gen": "image_gen.py",
    "svg_quality_checker": "svg_quality_checker.py",
    "total_md_split": "total_md_split.py",
    "finalize_svg": "finalize_svg.py",
    "svg_to_pptx": "svg_to_pptx.py",
    "animation_config": "animation_config.py",
    "notes_to_audio": "notes_to_audio.py",
    "pptx_template_import": "pptx_template_import.py",
    "template_fill_pptx": "template_fill_pptx.py",
    "visual_review": "visual_review.py",
    "update_spec": "update_spec.py",
}

POST_PROCESSING_ORDER = {
    "total_md_split": 0,
    "finalize_svg": 1,
    "svg_to_pptx": 2,
}


class ToolRegistry:
    """Tool schemas and implementations exposed to the model."""

    def __init__(
        self,
        *,
        repo_root: Path,
        config: AgentConfig,
        state: WorkflowState,
        session: SessionStore,
    ):
        self.repo_root = repo_root.resolve()
        self.scripts_root = (
            self.repo_root / "skills" / "ppt-master" / "scripts"
        )
        self.config = config
        self.state = state
        self.session = session
        self._spec_lock_read = False
        self._confirmation_authorized = False
        self._preview_process: subprocess.Popen[str] | None = None
        self._preview_url = ""

    def begin_turn(self, user_text: str) -> None:
        """Set per-turn authorization signals that tools cannot invent."""
        normalized = user_text.strip().lower()
        affirmative = {
            "yes",
            "y",
            "confirm",
            "confirmed",
            "approve",
            "approved",
            "proceed",
            "continue",
            "ok",
            "okay",
            "looks good",
            "go ahead",
        }
        self._confirmation_authorized = (
            normalized in affirmative
            or normalized.startswith("confirm ")
            or normalized.startswith("approved ")
            or normalized.startswith("proceed ")
        )

    def schemas(self) -> list[dict[str, Any]]:
        return [
            _tool("read_file", "Read a UTF-8 text file.", {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 500},
            }, ["path"]),
            _tool("list_directory", "List files in a workspace directory.", {
                "path": {"type": "string"},
                "recursive": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            }, ["path"]),
            _tool("search_files", "Search workspace text files with a regex.", {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            }, ["pattern", "path"]),
            _tool("write_file", "Atomically write a UTF-8 workspace file.", {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
                "expected_sha256": {"type": "string"},
            }, ["path", "content"]),
            _tool("inspect_image", "Load a local image for visual inspection.", {
                "path": {"type": "string"},
            }, ["path"]),
            _tool("run_ppt_script", "Run an allowlisted PPT Master script.", {
                "script": {"type": "string", "enum": sorted(SCRIPT_MAP)},
                "args": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 1800},
            }, ["script", "args"]),
            _tool("start_live_preview", "Start the local SVG live preview service.", {
                "port": {"type": "integer", "minimum": 1024, "maximum": 65535},
            }, []),
            _tool("check_live_preview", "Check the local live preview service.", {}, []),
            _tool("stop_live_preview", "Stop the preview process started by this session.", {}, []),
            _tool("set_project", "Set the active existing project directory.", {
                "path": {"type": "string"},
            }, ["path"]),
            _tool("approve_confirmations", "Record explicit user approval of the Eight Confirmations.", {}, []),
            _tool("set_workflow_phase", "Advance exactly one workflow phase.", {
                "phase": {"type": "string"},
                "note": {"type": "string"},
            }, ["phase"]),
            _tool("get_status", "Get current workflow, image mode, and usage.", {}, []),
        ]

    def preview_status(self) -> dict[str, Any]:
        """Return preview status without recording a model tool event."""
        return self._call_check_live_preview()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_call_{name}", None)
        if handler is None:
            raise ValueError(f"Unknown tool: {name}")
        result = handler(**arguments)
        event_result = dict(result)
        event_result.pop("__image_data_url", None)
        event_result.pop("__switch_project", None)
        self.session.append_event({
            "tool": name,
            "arguments": arguments,
            "result": event_result,
        })
        return result

    def _call_read_file(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 240,
    ) -> dict[str, Any]:
        target = self._path(path, must_exist=True)
        if not target.is_file():
            raise ValueError(f"Not a file: {target}")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        selected = lines[start_line - 1:start_line - 1 + max_lines]
        content = "\n".join(selected)[:MAX_READ_CHARS]
        if (
            self.state.project_path
            and target == Path(self.state.project_path) / "spec_lock.md"
        ):
            self._spec_lock_read = True
        return {
            "path": str(target),
            "start_line": start_line,
            "line_count": len(selected),
            "sha256": file_sha256(target),
            "content": content,
            "truncated": len(content) >= MAX_READ_CHARS,
        }

    def _call_list_directory(
        self,
        path: str,
        recursive: bool = False,
        limit: int = 500,
    ) -> dict[str, Any]:
        target = self._path(path, must_exist=True)
        iterator = target.rglob("*") if recursive else target.iterdir()
        entries = []
        for entry in iterator:
            entries.append({
                "path": str(entry),
                "type": "directory" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
            })
            if len(entries) >= limit:
                break
        return {"entries": entries, "truncated": len(entries) >= limit}

    def _call_search_files(
        self,
        pattern: str,
        path: str,
        glob: str = "*",
        limit: int = 200,
    ) -> dict[str, Any]:
        target = self._path(path, must_exist=True)
        regex = re.compile(pattern)
        matches = []
        for file_path in target.rglob(glob):
            if not file_path.is_file() or file_path.stat().st_size > 2_000_000:
                continue
            try:
                lines = file_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if regex.search(line):
                    matches.append({
                        "path": str(file_path),
                        "line": line_number,
                        "text": line[:1000],
                    })
                    if len(matches) >= limit:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def _call_write_file(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
        expected_sha256: str = "",
    ) -> dict[str, Any]:
        target = self._path(path)
        self._validate_write_target(target)
        self._validate_svg_write(target)
        self._validate_svg_assets(target, content)
        if target.exists():
            if not overwrite:
                raise FileExistsError(
                    f"File exists; set overwrite=true after reading it: {target}"
                )
            if expected_sha256 and file_sha256(target) != expected_sha256:
                raise ValueError(f"File changed since it was read: {target}")
        atomic_write_text(target, content)
        if target.suffix.lower() == ".svg" and "svg_output" in target.parts:
            self.state.current_slide += 1
            self.state.save(self.session.state_path)
            self._spec_lock_read = False
        return {
            "path": str(target),
            "bytes": len(content.encode("utf-8")),
            "sha256": file_sha256(target),
        }

    def _validate_svg_assets(self, target: Path, content: str) -> None:
        if target.suffix.lower() != ".svg":
            return
        hrefs = re.findall(
            r"<image\b[^>]*\b(?:href|xlink:href)=[\"']([^\"']+)[\"']",
            content,
            flags=re.IGNORECASE,
        )
        for href in hrefs:
            if href.startswith(("data:", "http://", "https://")):
                if self.config.image_mode == "disabled":
                    raise ValueError(
                        "External or generated images are unavailable while "
                        "image generation is disabled. Use SVG-native visuals."
                    )
                continue
            asset = (target.parent / href).resolve()
            if not asset.is_file():
                raise ValueError(
                    f"SVG image asset does not exist: {href}. "
                    "Use an existing local asset or SVG-native visuals."
                )

    def _call_inspect_image(self, path: str) -> dict[str, Any]:
        target = self._path(path, must_exist=True)
        if not target.is_file():
            raise ValueError(f"Not a file: {target}")
        mime_type, _encoding = mimetypes.guess_type(target.name)
        if mime_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
        }:
            raise ValueError(
                "inspect_image supports PNG, JPEG, WebP, and GIF files."
            )
        size = target.stat().st_size
        if size > 10_000_000:
            raise ValueError("Image exceeds the 10 MB inspection limit.")
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
        return {
            "path": str(target),
            "bytes": size,
            "__image_data_url": f"data:{mime_type};base64,{encoded}",
        }

    def _call_run_ppt_script(
        self,
        script: str,
        args: list[str],
        timeout: int = 600,
    ) -> dict[str, Any]:
        if script not in SCRIPT_MAP:
            raise ValueError(f"Script is not allowlisted: {script}")
        if script == "image_gen" and self.config.image_mode != "enabled":
            raise ValueError(
                f"image_gen is unavailable while image mode is "
                f"{self.config.image_mode}."
            )
        self._validate_script_phase(script)
        safe_args = self._validate_args(args)
        script_path = self.scripts_root / SCRIPT_MAP[script]
        command = [sys.executable, str(script_path), *safe_args]
        env = os.environ.copy()
        result = subprocess.run(
            command,
            cwd=self.repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        output = redact_text((result.stdout + result.stderr)[-MAX_TOOL_OUTPUT:])
        if result.returncode != 0:
            raise RuntimeError(
                f"{script} failed with exit code {result.returncode}:\n{output}"
            )
        if script in POST_PROCESSING_ORDER:
            self.state.post_process_index = POST_PROCESSING_ORDER[script]
            self.state.save(self.session.state_path)
        return {
            "script": script,
            "returncode": result.returncode,
            "output": output,
        }

    def _call_set_project(self, path: str) -> dict[str, Any]:
        target = self._path(path, must_exist=True)
        if not target.is_dir():
            raise ValueError(f"Project path is not a directory: {target}")
        projects_root = (self.repo_root / "projects").resolve()
        if target != projects_root and projects_root not in target.parents:
            raise ValueError("Active projects must be under projects/.")
        self.state.project_path = str(target)
        if self.state.phase == "source_input":
            self.state.transition("project_initialized")
        self.state.save(self.session.state_path)
        return {
            "project_path": str(target),
            "__switch_project": str(target),
        }

    def _call_start_live_preview(self, port: int = 5050) -> dict[str, Any]:
        if not self.state.project_path:
            raise ValueError("Set an active project before starting preview.")
        if self._preview_url and self._preview_reachable():
            return {"url": self._preview_url, "status": "already-running"}
        if self._preview_process and self._preview_process.poll() is None:
            return {"url": self._preview_url, "status": "already-running"}
        server = self.scripts_root / "svg_editor" / "server.py"
        command = [
            sys.executable,
            str(server),
            self.state.project_path,
            "--live",
            "--port",
            str(port),
        ]
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._preview_process = subprocess.Popen(
            command,
            cwd=self.repo_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            shell=False,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        self._preview_url = f"http://127.0.0.1:{port}"
        for _attempt in range(20):
            if self._preview_process.poll() is not None:
                raise RuntimeError("Live preview exited during startup.")
            if self._preview_reachable():
                return {
                    "url": self._preview_url,
                    "pid": self._preview_process.pid,
                    "status": "running",
                }
            time.sleep(0.25)
        raise RuntimeError(
            f"Live preview did not become ready at {self._preview_url}."
        )

    def _call_check_live_preview(self) -> dict[str, Any]:
        running = bool(
            self._preview_process
            and self._preview_process.poll() is None
            and self._preview_reachable()
        )
        return {
            "url": self._preview_url,
            "running": running,
            "pid": self._preview_process.pid
            if self._preview_process and running
            else None,
        }

    def _call_stop_live_preview(self) -> dict[str, Any]:
        process = self._preview_process
        if process is None or process.poll() is not None:
            return {"stopped": False, "reason": "not-running"}
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        return {"stopped": True}

    def _call_approve_confirmations(self) -> dict[str, Any]:
        if not self._confirmation_authorized:
            raise ValueError(
                "The current user message does not explicitly approve the "
                "Eight Confirmations."
            )
        self.state.approve_confirmations()
        self.state.save(self.session.state_path)
        return {"confirmations_approved": True}

    def _call_set_workflow_phase(
        self,
        phase: str,
        note: str = "",
    ) -> dict[str, Any]:
        if phase in {"quality_checked", "post_processing", "exported"}:
            expected = self._expected_slide_count()
            actual = self._generated_slide_count()
            if expected and actual < expected:
                raise ValueError(
                    f"Cannot advance to {phase}: generated {actual} of "
                    f"{expected} required slides."
                )
        self.state.transition(phase)
        if note:
            self.state.notes.append(note[:1000])
        self.state.save(self.session.state_path)
        return {"phase": self.state.phase}

    def _expected_slide_count(self) -> int:
        if not self.state.project_path:
            return 0
        spec = Path(self.state.project_path) / "design_spec.md"
        if not spec.is_file():
            return 0
        text = spec.read_text(encoding="utf-8", errors="replace")
        patterns = [
            r"\|\s*\*\*Page Count\*\*\s*\|\s*(\d+)\s*\|",
            r"(?im)^\s*page_count\s*:\s*(\d+)\s*$",
            r"(?im)^\s*page count\s*:\s*(\d+)\s*$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1))
        return 0

    def _generated_slide_count(self) -> int:
        if not self.state.project_path:
            return 0
        output = Path(self.state.project_path) / "svg_output"
        return len(list(output.glob("*.svg"))) if output.is_dir() else 0

    def _call_get_status(self) -> dict[str, Any]:
        return {
            "phase": self.state.phase,
            "project_path": self.state.project_path,
            "confirmations_approved": self.state.confirmations_approved,
            "current_slide": self.state.current_slide,
            "image_mode": self.config.image_mode,
            "model": self.config.model,
            "usage": {
                "prompt_tokens": self.session.usage.prompt_tokens,
                "completion_tokens": self.session.usage.completion_tokens,
                "total_tokens": self.session.usage.total_tokens,
            },
            "web_search": "unavailable",
        }

    def _validate_script_phase(self, script: str) -> None:
        if script in {"total_md_split", "finalize_svg", "svg_to_pptx"}:
            if self.state.phase not in {
                "quality_checked",
                "post_processing",
            }:
                raise ValueError(
                    f"{script} requires quality_checked or post_processing."
                )
            expected = self.state.post_process_index + 1
            actual = POST_PROCESSING_ORDER[script]
            if actual != expected:
                order = ", ".join(POST_PROCESSING_ORDER)
                raise ValueError(
                    f"Post-processing must run in order: {order}"
                )

    def _validate_args(self, args: list[str]) -> list[str]:
        safe = []
        for arg in args:
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                raise ValueError("Script arguments cannot contain control lines.")
            lower = arg.lower()
            if lower.startswith(("http://", "https://")):
                raise ValueError("URLs are disabled in the built-in agent.")
            safe.append(arg)
        return safe

    def _path(self, raw_path: str, must_exist: bool = False) -> Path:
        project = Path(self.state.project_path) if self.state.project_path else None
        return resolve_safe_path(
            raw_path,
            repo_root=self.repo_root,
            project_path=project,
            must_exist=must_exist,
        )

    def _validate_write_target(self, target: Path) -> None:
        allowed_roots = []
        if self.state.project_path:
            allowed_roots.append(Path(self.state.project_path).resolve())
        allowed_roots.extend([
            (self.repo_root / "skills" / "ppt-master" / "templates").resolve(),
        ])
        if not any(
            target == root or root in target.parents
            for root in allowed_roots
        ):
            raise ValueError(
                "The built-in agent may only write inside the active project "
                "or skills/ppt-master/templates/."
            )

    def _validate_svg_write(self, target: Path) -> None:
        if target.suffix.lower() != ".svg" or "svg_output" not in target.parts:
            return
        if self.state.phase != "executing_slides":
            raise ValueError(
                "SVG pages may only be written during executing_slides."
            )
        if not self._spec_lock_read:
            raise ValueError(
                "Read the active project's spec_lock.md immediately before "
                "writing each SVG page."
            )
        match = re.match(r"(?:slide_)?(\d+)", target.stem)
        if not match:
            raise ValueError(
                "SVG page names must begin with a sequential slide number."
            )
        page_number = int(match.group(1))
        expected = self.state.current_slide + 1
        if page_number != expected:
            raise ValueError(
                f"Expected slide {expected:02d}, got {page_number:02d}."
            )

    def _preview_reachable(self) -> bool:
        if not self._preview_url:
            return False
        try:
            with urllib.request.urlopen(self._preview_url, timeout=1):
                return True
        except (urllib.error.URLError, TimeoutError):
            return False


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }
