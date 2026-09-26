from __future__ import annotations

from pathlib import Path

from GemAgents.layout import RepoLayout


def load_project_context(workspace: Path) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    layout = RepoLayout(workspace)
    for name in ("AGENTS.md", "AGENT.md", "CLAUDE.md"):
        path = layout.resolve(name)
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            if content in seen:
                continue
            seen.add(content)
            parts.append(f"# {name}\n{content}")
    memory_path = layout.runtime / "memory.md"
    if memory_path.is_file():
        parts.append(f"# Project memory\n{memory_path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)
