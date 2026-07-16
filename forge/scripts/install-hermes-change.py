#!/usr/bin/env python3
"""Build, install, or restore the shared Hermes ``pre_user_turn`` change."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from forge.hermes_change.installer import (  # noqa: E402
    InstallError,
    build_change_package,
    install_change,
    restore_change,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("build", "install", "restore"):
        command = commands.add_parser(action)
        command.add_argument("--hermes-root", type=Path, required=True)
        command.add_argument("--package", type=Path, required=True)
        if action == "build":
            command.add_argument("--source-version", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "build":
            manifest = build_change_package(
                args.hermes_root,
                args.package,
                source_version=args.source_version,
            )
        elif args.action == "install":
            manifest = install_change(args.hermes_root, args.package)
        else:
            manifest = restore_change(args.hermes_root, args.package)
    except InstallError as error:
        print(
            json.dumps(
                {"action": args.action, "ok": False, "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "action": args.action,
                "ok": True,
                "source_version": manifest.source_version,
                "files": [item.path for item in manifest.files],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
