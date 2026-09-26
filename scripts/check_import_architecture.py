#!/usr/bin/env python3
"""Static boundaries for deterministic metabolic modules."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

FORBIDDEN_IMPORT_ROOTS = (
    "anthropic",
    "langchain",
    "langgraph",
    "openai",
    "GemAgents.agent",
    "GemAgents.cli",
    "GemAgents.mcp",
    "GemAgents.tools",
)
LEGACY_FACADE_PATHS = frozenset(
    {
        "src/GemAgents/metabolic/pipeline.py",
        "src/GemAgents/metabolic/reconstruction/__init__.py",
    }
)


def _is_forbidden(module: str) -> bool:
    return any(module == root or module.startswith(root + ".") for root in FORBIDDEN_IMPORT_ROOTS)


def scan(root: Path) -> dict[str, object]:
    root = root.resolve()
    metabolic = (root / "src/GemAgents/metabolic").resolve()
    violations: list[dict[str, object]] = []
    parse_errors: list[dict[str, str]] = []
    for path in sorted(metabolic.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            parse_errors.append(
                {"path": str(path.relative_to(root)), "error": type(error).__name__}
            )
            continue
        relative = str(path.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            for module in modules:
                if _is_forbidden(module):
                    violations.append(
                        {
                            "path": relative,
                            "line": node.lineno,
                            "kind": "forbidden_import",
                            "module": module,
                        }
                    )
                if (
                    relative == "src/GemAgents/metabolic/jobs/runtime.py"
                    and module == "GemAgents.metabolic.legacy"
                ):
                    violations.append(
                        {
                            "path": relative,
                            "line": node.lineno,
                            "kind": "job_runtime_legacy_import",
                            "module": module,
                        }
                    )
                if (
                    module == "GemAgents.metabolic.legacy"
                    and relative not in LEGACY_FACADE_PATHS
                ):
                    violations.append(
                        {
                            "path": relative,
                            "line": node.lineno,
                            "kind": "deterministic_leaf_legacy_import",
                            "module": module,
                        }
                    )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.attr in {"cwd", "getcwd"}:
                    violations.append(
                        {"path": relative, "line": node.lineno, "kind": "cwd_resource_lookup"}
                    )
    return {
        "root": str(root.resolve()),
        "scope": "src/GemAgents/metabolic/**/*.py",
        "violations": violations,
        "parse_errors": parse_errors,
        "passed": not violations and not parse_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = scan(args.root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"import architecture guard: {'PASS' if result['passed'] else 'FAIL'}")
        for item in result["violations"]:
            print(f"violation: {item}")
        for item in result["parse_errors"]:
            print(f"parse_error: {item}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
