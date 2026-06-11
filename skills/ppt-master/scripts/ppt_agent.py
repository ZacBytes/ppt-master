#!/usr/bin/env python3
"""
PPT Master - Built-in OpenRouter Agent

Runs PPT Master's complete local workflow without an external coding agent.

Usage:
    python3 scripts/ppt_agent.py
    python3 scripts/ppt_agent.py --project projects/my_deck_ppt169_20260611

Examples:
    python3 scripts/ppt_agent.py --image-mode disabled
    python3 scripts/ppt_agent.py --image-mode prompts-only

Dependencies:
    None
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from agent_runtime import AgentConfig, PPTMasterAgent  # noqa: E402
from agent_runtime.config import VALID_IMAGE_MODES  # noqa: E402
from agent_runtime.security import resolve_safe_path  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the built-in OpenRouter PPT Master agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--project",
        help="Existing project directory under projects/.",
    )
    parser.add_argument(
        "--image-mode",
        choices=sorted(VALID_IMAGE_MODES),
        help="Override image generation mode for this process.",
    )
    parser.add_argument("--model", help="Override the OpenRouter model.")
    parser.add_argument(
        "--message",
        help="Run one non-interactive turn and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[3]
    if args.image_mode:
        os.environ["PPT_MASTER_IMAGE_GENERATION"] = args.image_mode
    if args.model:
        os.environ["PPT_MASTER_AGENT_MODEL"] = args.model
    try:
        config = AgentConfig.load(repo_root)
        project_path = None
        if args.project:
            project_path = resolve_safe_path(
                args.project,
                repo_root=repo_root,
                must_exist=True,
            )
            projects_root = (repo_root / "projects").resolve()
            if project_path != projects_root and projects_root not in project_path.parents:
                raise ValueError("--project must be inside projects/.")
        session_directory = (
            project_path / ".agent"
            if project_path
            else repo_root / ".ppt-master-agent"
        )
        agent = PPTMasterAgent(
            repo_root=repo_root,
            config=config,
            session_directory=session_directory,
        )
        if project_path:
            agent.switch_project(project_path, preserve_messages=False)
        if args.message:
            print(agent.run_turn(args.message))
            return 0
        return _interactive(agent)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _interactive(agent: PPTMasterAgent) -> int:
    print("PPT Master built-in agent")
    print(
        f"Model: {agent.config.model} | "
        f"Images: {agent.config.image_mode} | Web search: unavailable"
    )
    print(
        "Commands: /status, /open, /new, /resume, /preview, /export, "
        "/clear, /help, /quit"
    )
    while True:
        try:
            user_text = input("\nppt-master> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_text:
            continue
        if user_text in {"/quit", "/exit"}:
            return 0
        if user_text == "/status":
            print(agent.status_text())
            continue
        if user_text == "/clear":
            agent.clear_conversation()
            print("Conversation history cleared; workflow state preserved.")
            continue
        if user_text.startswith("/open "):
            raw_path = user_text[6:].strip()
            try:
                project_path = resolve_safe_path(
                    raw_path,
                    repo_root=agent.repo_root,
                    must_exist=True,
                )
                projects_root = (agent.repo_root / "projects").resolve()
                if (
                    project_path != projects_root
                    and projects_root not in project_path.parents
                ):
                    raise ValueError("Project must be inside projects/.")
                agent.switch_project(project_path, preserve_messages=False)
                print(f"Opened project: {project_path}")
            except (OSError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
            continue
        if user_text.startswith("/new "):
            request = user_text[5:].strip()
            user_text = (
                "Create a new PPT Master project from this request, then "
                f"continue through the workflow gates: {request}"
            )
        elif user_text == "/resume":
            user_text = (
                "Resume the active project from its current workflow phase. "
                "Verify prerequisites and continue until the next blocking "
                "checkpoint or completion."
            )
        elif user_text == "/preview":
            try:
                result = agent.tools.call("start_live_preview", {})
                print(result["url"])
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
            continue
        elif user_text == "/export":
            user_text = (
                "Complete all currently eligible quality and post-processing "
                "steps, then export the active project to PPTX. Respect every "
                "workflow gate."
            )
        elif user_text in {"/usage", "/model"}:
            print(agent.status_text())
            continue
        if user_text == "/help":
            print(
                "Describe the presentation and provide local source paths. "
                "The agent cannot browse or fetch URLs. Use /status to inspect "
                "the current phase. Use /open <project>, /new <request>, "
                "/resume, /preview, or /export for workflow shortcuts."
            )
            continue
        try:
            print(agent.run_turn(user_text))
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
