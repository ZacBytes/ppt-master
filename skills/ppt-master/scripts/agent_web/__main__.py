"""Entry point: python -m agent_web [--port PORT]"""

import argparse
from pathlib import Path

from .server import create_app

def main() -> None:
    parser = argparse.ArgumentParser(description="PPT Master Studio")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (default: 4 levels up from this file)",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root) if args.repo_root else Path(__file__).resolve().parents[4]
    app = create_app(repo_root)
    print(f"PPT Master Studio: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
