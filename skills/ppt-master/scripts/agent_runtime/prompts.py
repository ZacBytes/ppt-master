#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Prompt Assembly

Loads the authoritative workflow and the role reference needed for this turn.

Dependencies:
    None
"""

from __future__ import annotations

from pathlib import Path

from .workflow import WorkflowState


ROLE_FILES = {
    "awaiting_confirmations": "strategist.md",
    "strategy_locked": "strategist.md",
    "assets_ready": "image-generator.md",
    "executing_slides": "executor-base.md",
    "quality_checked": "executor-base.md",
    "post_processing": "executor-base.md",
}


def build_system_prompt(
    repo_root: Path,
    state: WorkflowState,
) -> str:
    skill_root = repo_root / "skills" / "ppt-master"
    sections = [
        _read(repo_root / "AGENTS.md"),
        _read(skill_root / "SKILL.md"),
    ]
    role_name = ROLE_FILES.get(state.phase)
    if role_name:
        sections.append(_read(skill_root / "references" / role_name))
    authoritative_instructions = "\n\n---\n\n".join(sections)
    return f"""You are PPT Master's built-in presentation agent.

Current workflow state:
- phase: {state.phase}
- project: {state.project_path or "not selected"}
- image generation: {state.image_mode}
- confirmations approved: {state.confirmations_approved}
- current slide: {state.current_slide}

Non-negotiable built-in runtime policy:
- There is no web search, web browsing, URL fetching, or web image search.
- Never ask a tool to run web_to_md.py or image_search.py.
- A URL is plain reference text unless the user supplies its contents locally.
- For topic-only requests, ask for a substantive brief or local source material.
- Image generation is optional. In disabled mode, use editable SVG-native
  diagrams, charts, icons, shapes, typography, user images, and template assets.
- In prompts-only mode, image prompts may be written but image_gen.py must not run.
- Never reveal environment variables, API keys, authorization headers, or secrets.
- Respect the workflow state machine and the Eight Confirmations hard stop.
- Use tools to perform the work. Do not merely describe commands the user should run.
- Before every SVG page, call read_file on the active project's spec_lock.md.
- Write SVG pages directly, sequentially. Do not generate them through a loop/script.
- Use set_workflow_phase after completing and verifying each phase.

Authoritative repository instructions follow.

{authoritative_instructions}
"""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig")
