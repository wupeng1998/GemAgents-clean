from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from GemAgents.frontmatter import read_markdown_document
from GemAgents.layout import RepoLayout, UserLayout


@dataclass(frozen=True)
class Extension:
    name: str
    path: Path
    metadata: dict[str, object]
    body: str


def load_skills(workspace: Path) -> dict[str, Extension]:
    return _load_markdown_extensions(workspace, "skills")


def load_commands(workspace: Path) -> dict[str, Extension]:
    return _load_markdown_extensions(workspace, "commands")


def load_agents(workspace: Path) -> dict[str, Extension]:
    builtins = {
        "general-purpose": Extension(
            name="general-purpose",
            path=Path("<built-in>"),
            metadata={"description": "General coding assistant"},
            body="Use this agent for broad coding and research tasks.",
        ),
        "Explore": Extension(
            name="Explore",
            path=Path("<built-in>"),
            metadata={"description": "Read-only exploration agent"},
            body="Explore the repository and report findings without editing files.",
        ),
    }
    builtins.update(_load_markdown_extensions(workspace, "agents"))
    return builtins


def load_output_styles(workspace: Path) -> dict[str, Extension]:
    styles = {
        "default": Extension("default", Path("<built-in>"), {}, ""),
        "Explanatory": Extension(
            "Explanatory",
            Path("<built-in>"),
            {},
            "Explain reasoning clearly.",
        ),
        "Learning": Extension("Learning", Path("<built-in>"), {}, "Teach while answering."),
    }
    styles.update(_load_markdown_extensions(workspace, "output-styles"))
    return styles


def render_command(extension: Extension, arguments: str) -> str:
    words = arguments.split()
    rendered = extension.body.replace("$ARGUMENTS", arguments)
    for index, word in enumerate(words, start=1):
        rendered = rendered.replace(f"${index}", word)
    return rendered


def _load_markdown_extensions(workspace: Path, dirname: str) -> dict[str, Extension]:
    result: dict[str, Extension] = {}
    project_root = RepoLayout(workspace).resolve(f".gemagents/{dirname}")
    for root in (UserLayout().extensions(dirname), project_root):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            name = path.relative_to(root).with_suffix("").as_posix().replace("/", ":")
            document = read_markdown_document(path)
            result[name] = Extension(
                name=name,
                path=path,
                metadata=document.metadata,
                body=document.body,
            )
    return result
