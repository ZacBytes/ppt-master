#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Web Server

Serves the local studio UI and coordinates background agent turns.

Dependencies:
    flask>=3.0.0
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import mimetypes
from pathlib import Path
import re
import shutil
import threading
import uuid
from typing import Any

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
)
from werkzeug.utils import secure_filename

from agent_runtime import AgentConfig, PPTMasterAgent
from agent_runtime.security import redact_text, resolve_safe_path
from agent_runtime.workflow import PHASES


ALLOWED_UPLOADS = {
    ".csv",
    ".doc",
    ".docx",
    ".emf",
    ".epub",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".md",
    ".odt",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rtf",
    ".svg",
    ".tsv",
    ".txt",
    ".webp",
    ".wmf",
    ".xls",
    ".xlsm",
    ".xlsx",
}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
PHASE_LABELS = {
    "source_input": "Source",
    "project_initialized": "Project",
    "template_resolved": "Template",
    "awaiting_confirmations": "Confirm",
    "strategy_locked": "Strategy",
    "assets_ready": "Assets",
    "executing_slides": "Build",
    "quality_checked": "Quality",
    "post_processing": "Export",
    "exported": "Done",
}


class StudioRuntime:
    """Thread-safe active agent and task coordinator."""

    def __init__(self, repo_root: Path, config: AgentConfig):
        self.repo_root = repo_root.resolve()
        self.config = config
        self.lock = threading.RLock()
        self.turn_lock = threading.Lock()
        self.task: dict[str, Any] = {
            "id": "",
            "status": "idle",
            "started_at": "",
            "finished_at": "",
            "error": "",
        }
        self.active_project: Path | None = None
        self.draft_id = ""
        self.draft_label = ""
        self.event_condition = threading.Condition(self.lock)
        self.events: list[dict[str, Any]] = []
        self.event_sequence = 0
        self.agent = self._new_agent()
        saved_project = self.agent.state.project_path
        if saved_project and Path(saved_project).is_dir():
            self.active_project = Path(saved_project).resolve()
            self.agent.switch_project(self.active_project)

    def _new_agent(self, directory: Path | None = None) -> PPTMasterAgent:
        directory = directory or (
            self.active_project / ".agent"
            if self.active_project
            else self.repo_root / ".ppt-master-agent"
        )
        return PPTMasterAgent(
            repo_root=self.repo_root,
            config=self.config,
            session_directory=directory,
        )

    def switch_project(self, project: Path | None) -> None:
        with self.lock:
            if self.task["status"] == "running":
                raise RuntimeError("Wait for the active agent turn to finish.")
            self.active_project = project.resolve() if project else None
            self.draft_id = ""
            self.draft_label = ""
            self.agent = self._new_agent()
            if self.active_project:
                self.agent.switch_project(
                    self.active_project,
                    preserve_messages=False,
                )
                self._reconcile_artifact_state()
            self._emit("state", state=self.state_payload())

    def start_new_project(
        self,
        brief: str,
        slide_count: int = 0,
        audience: str = "",
        tone: str = "",
        template: str = "",
    ) -> tuple[str, dict[str, Any]]:
        with self.lock:
            if self.task["status"] == "running":
                raise RuntimeError("Wait for the active agent turn to finish.")
            self.draft_id = uuid.uuid4().hex
            self.draft_label = "New presentation"
            self.active_project = None
            draft_dir = (
                self.repo_root / ".ppt-master-agent" / "drafts" / self.draft_id
            )
            self.agent = self._new_agent(draft_dir)
        params: list[str] = []
        if slide_count:
            params.append(f"Target slide count: {slide_count}")
        if audience:
            params.append(f"Target audience: {audience}")
        if tone:
            params.append(f"Tone / style: {tone}")
        if template:
            params.append(f"Use layout template: {template}")
        param_block = "\n".join(params)
        full_request = f"{brief}\n\n[Parameters:\n{param_block}]" if params else brief
        outline_instruction = (
            "\n\nAfter creating design_spec.md and spec_lock.md, ALSO write a file named "
            "'outline.json' in the project root using this exact JSON structure:\n"
            '{"title":"<deck title>","slides":[{"index":1,"title":"<slide title>",'
            '"purpose":"<one sentence describing what this slide achieves>",'
            '"key_points":["point 1","point 2"],'
            '"layout":"cover|section|content|closing"}]}\n'
            "Include every planned slide. Valid layout values: cover, section, content, closing.\n"
            "Then STOP — do not generate SVG slides yet."
        )
        task_id = self.start_turn(
            "Create a new PPT Master project from this request. Run the Strategist role "
            "to produce design_spec.md and spec_lock.md, write outline.json, then stop.\n\n"
            f"Request: {full_request}{outline_instruction}"
        )
        return task_id, self.state_payload()

    def start_turn(self, text: str) -> str:
        if not text.strip():
            raise ValueError("Message cannot be empty.")
        with self.lock:
            if self.task["status"] == "running":
                raise RuntimeError("An agent turn is already running.")
            task_id = uuid.uuid4().hex
            self.task = {
                "id": task_id,
                "status": "running",
                "started_at": _now(),
                "finished_at": "",
                "error": "",
            }
            self._emit("task_started", task=dict(self.task))
        thread = threading.Thread(
            target=self._run_turn,
            args=(task_id, text),
            daemon=True,
        )
        thread.start()
        return task_id

    def approve_and_continue(self) -> str:
        """Repair legacy gate state, approve it, and continue the pipeline."""
        with self.lock:
            if self.task["status"] == "running":
                raise RuntimeError("Wait for the active agent turn to finish.")
            if not self.agent.state.project_path:
                raise RuntimeError("Create or open a project first.")
            while self.agent.state.phase in {
                "source_input",
                "project_initialized",
                "template_resolved",
            }:
                next_phase = {
                    "source_input": "project_initialized",
                    "project_initialized": "template_resolved",
                    "template_resolved": "awaiting_confirmations",
                }[self.agent.state.phase]
                self.agent.state.transition(next_phase)
            if self.agent.state.phase == "awaiting_confirmations":
                self.agent.state.confirmations_approved = True
                self.agent.state.save(self.agent.session.state_path)
        return self.start_turn("Confirm")

    def _run_turn(self, task_id: str, text: str) -> None:
        with self.turn_lock:
            try:
                bounded_slide = self.agent.state.phase == "executing_slides"
                self.agent.run_turn(
                    text,
                    on_event=self._agent_event,
                    stop_after_svg=bounded_slide,
                )
                with self.lock:
                    self._sync_project_from_agent()
                    self._reconcile_artifact_state()
                    continue_text = self._next_bounded_step()
                    self.task.update({
                        "status": "complete",
                        "finished_at": _now(),
                    })
                    self._emit("task_complete", state=self.state_payload())
                if continue_text:
                    threading.Timer(
                        0.25,
                        lambda: self.start_turn(continue_text),
                    ).start()
            except Exception as exc:
                with self.lock:
                    self.task.update({
                        "status": "error",
                        "finished_at": _now(),
                        "error": redact_text(str(exc)),
                    })
                    self._emit("task_error", state=self.state_payload())

    def _next_bounded_step(self) -> str:
        project = self.active_project
        if not project:
            return ""
        state = self.agent.state
        manifest = project / "images" / "image_prompts.json"
        if state.phase == "strategy_locked":
            if self.config.image_mode == "enabled":
                if not manifest.is_file():
                    return (
                        "Create images/image_prompts.json in required manifest "
                        "format from design_spec.md section VIII. Include every "
                        "AI image marked Pending. Then run image_gen with "
                        f"--manifest {manifest}. Do not merely describe the step."
                    )
                return (
                    "Run image_gen now with --manifest "
                    f"{manifest}. Wait for all Novita tasks to finish. "
                    "Do not merely announce that image generation is starting."
                )
            state.transition("assets_ready")
            state.save(self.agent.session.state_path)
        if state.phase == "assets_ready":
            return (
                "Begin slide execution. Advance to executing_slides, then "
                "generate only slide 01 from the approved outline. Read "
                "spec_lock.md immediately before writing it."
            )
        if state.phase != "executing_slides":
            return ""
        expected = self.agent.tools._expected_slide_count()
        actual = len(_list_slides(project))
        if expected and actual < expected:
            next_page = actual + 1
            return (
                f"Generate only slide {next_page:02d} of {expected} from the "
                "approved outline. Read spec_lock.md immediately before writing "
                "it. Use only existing local assets; image generation is "
                f"{self.config.image_mode}. Do not start live preview."
            )
        if expected and actual == expected:
            return (
                "All required SVG slides now exist. Run SVG quality checking. "
                "Fix any errors using SVG-native visuals and existing local "
                "assets, then complete the required post-processing sequence "
                "and export the PPTX."
            )
        return ""

    def _agent_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._emit(event_type, **payload)

    def _emit(self, event_type: str, **payload: Any) -> None:
        with self.event_condition:
            self.event_sequence += 1
            event = {
                "id": self.event_sequence,
                "type": event_type,
                **payload,
            }
            self.events.append(event)
            self.events = self.events[-1000:]
            self.event_condition.notify_all()

    def events_after(self, sequence: int) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.events if event["id"] > sequence]

    def _sync_project_from_agent(self) -> None:
        path = self.agent.state.project_path
        if path:
            self.active_project = Path(path).resolve()
            self.draft_id = ""
            self.draft_label = ""

    def _reconcile_artifact_state(self) -> None:
        """Recover durable workflow progress from files after an interrupted turn."""
        project = self.active_project
        if not project:
            return
        state = self.agent.state
        if state.phase == "source_input":
            state.transition("project_initialized")
        if state.phase == "project_initialized":
            state.transition("template_resolved")
        if (project / "design_spec.md").is_file() and (
            project / "spec_lock.md"
        ).is_file():
            if state.phase == "template_resolved":
                state.transition("awaiting_confirmations")
            if state.confirmations_approved and state.phase == "awaiting_confirmations":
                state.transition("strategy_locked")
            if (
                state.phase == "strategy_locked"
                and self.config.image_mode == "disabled"
            ):
                state.transition("assets_ready")
            if state.phase == "strategy_locked" and self.config.image_mode == "enabled":
                manifest = project / "images" / "image_prompts.json"
                if self._manifest_complete(manifest):
                    state.transition("assets_ready")
        slides = _list_slides(project)
        if slides and state.phase == "assets_ready":
            state.transition("executing_slides")
        state.save(self.agent.session.state_path)

    @staticmethod
    def _manifest_complete(path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        items = data.get("items") or []
        return bool(items) and all(
            item.get("status") == "Generated" for item in items
        )

    def state_payload(self) -> dict[str, Any]:
        with self.lock:
            self._sync_project_from_agent()
            messages = []
            for message in self.agent.session.messages:
                role = message.get("role")
                content = message.get("content")
                if role not in {"user", "assistant"} or not content:
                    continue
                messages.append({
                    "role": role,
                    "content": content,
                })
            exports = _list_exports(self.active_project)
            slides = _list_slides(self.active_project)
            preview = self.agent.tools.preview_status()
            return {
                "task": dict(self.task),
                "workflow": asdict(self.agent.state),
                "messages": messages[-120:],
                "usage": asdict(self.agent.session.usage),
                "projects": _list_projects(self.repo_root),
                "active_project": str(self.active_project)
                if self.active_project
                else "",
                "active_label": (
                    self.active_project.name
                    if self.active_project
                    else self.draft_label
                ),
                "active_draft": bool(self.draft_id),
                "slides": slides,
                "exports": exports,
                "preview": preview,
                "model": self.config.model,
                "image_mode": self.config.image_mode,
                "phase_labels": PHASE_LABELS,
                "phase_order": PHASES,
                "web_search": False,
                "key_configured": bool(self.config.api_key),
            }


def create_app(
    repo_root: Path,
    config: AgentConfig | None = None,
) -> Flask:
    """Create the local studio Flask application."""
    repo_root = repo_root.resolve()
    config = config or AgentConfig.load(repo_root)
    static_dir = Path(__file__).resolve().parent / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    runtime = StudioRuntime(repo_root, config)
    app.extensions["ppt_master_runtime"] = runtime

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/api/state")
    def api_state():
        return jsonify(runtime.state_payload())

    @app.get("/api/events")
    def api_events():
        last_event_id = request.headers.get("Last-Event-ID")
        try:
            sequence = (
                int(last_event_id)
                if last_event_id
                else runtime.event_sequence
            )
        except ValueError:
            sequence = runtime.event_sequence

        @stream_with_context
        def generate():
            nonlocal sequence
            yield "retry: 1500\n\n"
            while True:
                pending = runtime.events_after(sequence)
                if pending:
                    for event in pending:
                        sequence = event["id"]
                        yield (
                            f"id: {sequence}\n"
                            f"event: {event['type']}\n"
                            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                        )
                    continue
                with runtime.event_condition:
                    runtime.event_condition.wait(timeout=15)
                if not runtime.events_after(sequence):
                    yield ": keep-alive\n\n"

    @app.post("/api/chat")
    def api_chat():
        data = request.get_json(silent=True) or {}
        text = str(data.get("message", "")).strip()
        try:
            task_id = runtime.start_turn(text)
            return jsonify({"task_id": task_id}), 202
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/confirm")
    def api_confirm():
        try:
            task_id = runtime.approve_and_continue()
            return jsonify({"task_id": task_id}), 202
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/projects/open")
    def api_open_project():
        data = request.get_json(silent=True) or {}
        raw_path = str(data.get("path", "")).strip()
        try:
            project = _resolve_project(repo_root, raw_path)
            runtime.switch_project(project)
            return jsonify(runtime.state_payload())
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/templates")
    def api_templates():
        decks_root = repo_root / "skills" / "ppt-master" / "templates" / "decks"
        index_path = decks_root / "decks_index.json"
        if not index_path.is_file():
            return jsonify([])
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify([])
        result = []
        for name, meta in index.items():
            deck_dir = decks_root / name
            slides: list[str] = []
            if deck_dir.is_dir():
                for svg_file in sorted(deck_dir.glob("*.svg"))[:5]:
                    encoded = svg_file.name.replace(" ", "%20")
                    slides.append(
                        f"/api/template-svg/{name.replace(' ', '%20')}/{encoded}"
                    )
            result.append({
                "id": name,
                "name": name,
                "summary": meta.get("summary", ""),
                "primary_color": meta.get("primary_color", "#4F46E5"),
                "canvas_format": meta.get("canvas_format", "ppt169"),
                "slides": slides,
            })
        return jsonify(result)

    @app.get("/api/template-svg/<path:template_path>")
    def api_template_svg(template_path: str):
        decks_root = (
            repo_root / "skills" / "ppt-master" / "templates" / "decks"
        ).resolve()
        candidate = (decks_root / template_path).resolve()
        try:
            candidate.relative_to(decks_root)
        except ValueError:
            return jsonify({"error": "Invalid path"}), 400
        if not candidate.is_file() or candidate.suffix.lower() != ".svg":
            return jsonify({"error": "Not found"}), 404
        return send_file(candidate, mimetype="image/svg+xml")

    @app.get("/api/workflow/outline")
    def api_workflow_outline():
        if not runtime.active_project:
            return jsonify({"error": "No active project"}), 404
        outline_path = runtime.active_project / "outline.json"
        if not outline_path.is_file():
            return jsonify({"error": "Outline not ready"}), 404
        try:
            data = json.loads(outline_path.read_text(encoding="utf-8"))
            return jsonify(data)
        except (OSError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 500

    @app.patch("/api/workflow/outline")
    def api_workflow_outline_update():
        if not runtime.active_project:
            return jsonify({"error": "No active project"}), 404
        outline_path = runtime.active_project / "outline.json"
        if not outline_path.is_file():
            return jsonify({"error": "Outline not ready"}), 404
        body = request.get_json(silent=True) or {}
        try:
            outline = json.loads(outline_path.read_text(encoding="utf-8"))
            if "slides" in body:
                outline["slides"] = body["slides"]
            if "title" in body:
                outline["title"] = body["title"]
            outline_path.write_text(
                json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return jsonify(outline)
        except (OSError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/projects/new")
    def api_new_project():
        data = request.get_json(silent=True) or {}
        brief = str(data.get("brief", "")).strip()
        if not brief:
            return jsonify({"error": "A presentation brief is required."}), 400
        try:
            task_id, state = runtime.start_new_project(
                brief,
                slide_count=int(data.get("slide_count") or 0),
                audience=str(data.get("audience") or "").strip(),
                tone=str(data.get("tone") or "").strip(),
                template=str(data.get("template") or "").strip(),
            )
            return jsonify({"task_id": task_id, "state": state}), 202
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/upload")
    def api_upload():
        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "No files were uploaded."}), 400
        upload_root = (
            runtime.active_project / "incoming"
            if runtime.active_project
            else runtime.agent.session.directory / "uploads"
        )
        upload_root.mkdir(parents=True, exist_ok=True)
        saved = []
        for item in files:
            filename = secure_filename(item.filename or "")
            suffix = Path(filename).suffix.lower()
            if not filename or suffix not in ALLOWED_UPLOADS:
                return jsonify({
                    "error": f"Unsupported file type: {item.filename}"
                }), 400
            target = _deduplicate_path(upload_root / filename)
            item.save(target)
            saved.append(str(target.resolve()))
        return jsonify({"files": saved})

    @app.post("/api/preview/start")
    def api_preview_start():
        try:
            result = runtime.agent.tools.call("start_live_preview", {})
            return jsonify(result)
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/preview/stop")
    def api_preview_stop():
        return jsonify(runtime.agent.tools.call("stop_live_preview", {}))

    @app.get("/api/slides/<path:filename>")
    def api_slide(filename: str):
        if not runtime.active_project:
            return jsonify({"error": "No active project."}), 404
        slide_dir = runtime.active_project / "svg_output"
        target = resolve_safe_path(
            str(slide_dir / filename),
            repo_root=repo_root,
            project_path=runtime.active_project,
            must_exist=True,
        )
        if target.parent != slide_dir.resolve() or target.suffix.lower() != ".svg":
            return jsonify({"error": "Invalid slide path."}), 400
        return send_file(target, mimetype="image/svg+xml")

    @app.get("/api/exports/<path:filename>")
    def api_export(filename: str):
        if not runtime.active_project:
            return jsonify({"error": "No active project."}), 404
        export_dir = runtime.active_project / "exports"
        target = resolve_safe_path(
            str(export_dir / filename),
            repo_root=repo_root,
            project_path=runtime.active_project,
            must_exist=True,
        )
        if target.parent != export_dir.resolve():
            return jsonify({"error": "Invalid export path."}), 400
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return send_file(target, mimetype=mime, as_attachment=True)

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify({"error": "Upload exceeds the 100 MB limit."}), 413

    @app.after_request
    def no_cache(response):
        if request.path.startswith("/api/") or request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


def _resolve_project(repo_root: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("Project path is required.")
    candidate = resolve_safe_path(
        raw_path,
        repo_root=repo_root,
        must_exist=True,
    )
    projects_root = (repo_root / "projects").resolve()
    if candidate == projects_root or projects_root not in candidate.parents:
        raise ValueError("Project must be inside projects/.")
    if not candidate.is_dir():
        raise ValueError("Project path is not a directory.")
    return candidate


def _list_projects(repo_root: Path) -> list[dict[str, Any]]:
    projects_root = repo_root / "projects"
    if not projects_root.is_dir():
        return []
    rows = []
    for path in projects_root.iterdir():
        if not path.is_dir() or path.name.startswith("_smoke_"):
            continue
        state_file = path / ".agent" / "state.json"
        phase = "source_input"
        if state_file.is_file():
            try:
                phase = json.loads(
                    state_file.read_text(encoding="utf-8")
                ).get("phase", phase)
            except (OSError, json.JSONDecodeError):
                pass
        rows.append({
            "name": path.name,
            "path": str(path.resolve()),
            "phase": phase,
            "slides": len(list((path / "svg_output").glob("*.svg")))
            if (path / "svg_output").is_dir()
            else 0,
            "modified": datetime.fromtimestamp(
                path.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        })
    rows.sort(key=lambda item: item["modified"], reverse=True)
    return rows


def _list_slides(project: Path | None) -> list[dict[str, Any]]:
    if not project:
        return []
    directory = project / "svg_output"
    if not directory.is_dir():
        return []
    return [
        {
            "name": path.name,
            "url": f"/api/slides/{path.name}",
            "modified": path.stat().st_mtime_ns,
        }
        for path in sorted(directory.glob("*.svg"))
    ]


def _list_exports(project: Path | None) -> list[dict[str, Any]]:
    if not project:
        return []
    directory = project / "exports"
    if not directory.is_dir():
        return []
    files = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        files.append({
            "name": path.name,
            "size": path.stat().st_size,
            "url": f"/api/exports/{path.name}",
            "modified": datetime.fromtimestamp(
                path.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat(),
        })
    files.sort(key=lambda item: item["modified"], reverse=True)
    return files


def _deduplicate_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate an upload filename.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
