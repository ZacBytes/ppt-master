#!/usr/bin/env python3
"""
PPT Master - Built-in Agent Web Studio

Starts the local browser interface for the built-in OpenRouter agent.

Usage:
    python3 scripts/ppt_agent_web.py
    python3 scripts/ppt_agent_web.py --port 5060 --no-open

Examples:
    python3 scripts/ppt_agent_web.py

Dependencies:
    flask>=3.0.0
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import webbrowser

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from agent_runtime import AgentConfig  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the PPT Master local web studio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5080)
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the default browser automatically.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[3]
    try:
        from agent_web import create_app

        config = AgentConfig.load(repo_root)
        app = create_app(repo_root, config)
    except ModuleNotFoundError as exc:
        if exc.name == "flask":
            print(
                "Error: Flask is not installed. Run: "
                "python -m pip install -r "
                f"\"{repo_root / 'requirements.txt'}\"",
                file=sys.stderr,
            )
            return 1
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{args.port}"
    print(f"PPT Master Studio: {url}")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(
        host=args.host,
        port=args.port,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
