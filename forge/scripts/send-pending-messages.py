#!/usr/bin/env python3
"""Send pending Forge knowledge messages to MEMEX and retain failures."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.request
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import yaml


def _write_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(f"{datetime.now().isoformat()} {message}\n")


def _post(
    url: str,
    payload: object,
    memex_token: str,
    *,
    session_id: str | None = None,
    timeout: int = 15,
) -> tuple[str | None, object]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": memex_token,
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    request = urllib.request.Request(url, json.dumps(payload).encode(), headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode()
        match = re.search(r"data: (\{.*\})", body)
        parsed = json.loads(match.group(1) if match else body) if body.strip() else {}
        return response.headers.get("mcp-session-id"), parsed


def _message_fields(text: str) -> tuple[str | None, str, list[str] | None]:
    aspect_match = re.search(r"^##\s*\[(\w+)\]", text, re.MULTILINE)
    project_match = re.search(r"^project::\s*(.+)$", text, re.MULTILINE)
    tags_match = re.search(r"^tags::\s*(.+)$", text, re.MULTILINE)
    tags = None
    if tags_match:
        tags = [
            tag.strip()
            for tag in re.split(r"[,\s]+", tags_match.group(1))
            if tag.strip()
        ]
    return (
        aspect_match.group(1) if aspect_match else None,
        project_match.group(1).strip() if project_match else "INFINITY_FORGE",
        tags,
    )


def send_pending_messages(
    pending: Path,
    sent: Path,
    log: Path,
    config: Path,
    *,
    url: str,
) -> tuple[int, int]:
    """Move only confirmed successful messages to the sent directory."""

    files = sorted(pending.glob("*.md"))
    if not files:
        return 0, 0
    configuration = yaml.safe_load(config.read_text(encoding="utf-8"))
    memex_token = configuration["mcp_servers"]["memex"]["headers"]["Authorization"]
    session_id, _ = _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "forge-messages", "version": "1.0"},
            },
        },
        memex_token,
    )
    _post(
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        memex_token,
        session_id=session_id,
    )
    sent.mkdir(parents=True, exist_ok=True)
    successful = failed = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
            aspect, project, tags = _message_fields(text)
            arguments: dict[str, object] = {"content": text, "project": project}
            if aspect:
                arguments["aspect"] = aspect
            if tags:
                arguments["tags"] = tags
            _, response = _post(
                url,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "save_memex", "arguments": arguments},
                },
                memex_token,
                session_id=session_id,
                timeout=120,
            )
            if isinstance(response, dict) and response.get("result", {}).get("isError"):
                raise RuntimeError("MEMEX reported an error")
            # RISK(side-effect): move a message only after MEMEX confirms success.
            shutil.move(str(path), sent / path.name)
            _write_log(log, f"SENT {path.name}")
            successful += 1
        except Exception as error:
            _write_log(log, f"FAILED {path.name} {error}")
            failed += 1
    return successful, failed


def _parser() -> argparse.ArgumentParser:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending", type=Path, default=home / "forge" / "outbox")
    parser.add_argument("--sent", type=Path, default=home / "forge" / "outbox" / "sent")
    parser.add_argument("--log", type=Path, default=home / "forge" / "messages.log")
    parser.add_argument("--config", type=Path, default=home / ".hermes" / "config.yaml")
    parser.add_argument("--url", default="http://127.0.0.1:8080/mcp")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        successful, failed = send_pending_messages(
            args.pending,
            args.sent,
            args.log,
            args.config,
            url=args.url,
        )
    except Exception as error:
        print(f"CHECK_ERROR: {error}", file=sys.stderr)
        return 2
    print(f"sent={successful} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
