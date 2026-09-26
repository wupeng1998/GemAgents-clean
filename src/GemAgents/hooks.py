from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HookResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str


class HookBlocked(RuntimeError):
    pass


def run_hooks(
    hooks: dict[str, Any],
    event: str,
    payload: dict[str, Any],
    *,
    workspace: Path,
    matcher: str = "*",
) -> list[HookResult]:
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return []

    results: list[HookResult] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_matcher = str(entry.get("matcher", "*"))
        if entry_matcher not in {"*", matcher}:
            continue
        for hook in entry.get("hooks", []):
            if not isinstance(hook, dict) or not hook.get("command"):
                continue
            command = str(hook["command"])
            completed = subprocess.run(
                command,
                input=json.dumps(payload),
                cwd=workspace,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
            result = HookResult(command, completed.returncode, completed.stdout, completed.stderr)
            results.append(result)
            if completed.returncode == 2:
                raise HookBlocked(completed.stderr.strip() or f"hook blocked {event}")
    return results

