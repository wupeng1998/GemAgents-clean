#!/usr/bin/env python3
"""Verify a wheel installation in an isolated, dependency-free environment.

The verifier never installs into the active conda environment and never
downloads dependencies.  It reports an explicit environment block when the
interpreter or the local ``build`` module cannot satisfy the project contract.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path
from typing import Any

from GemAgents.layout import RepoLayout

_PROJECT_NAME_RE = re.compile(r"^name\s*=\s*[\"']([^\"']+)[\"']", re.M)
_REQUIRES_PYTHON_RE = re.compile(r"^requires-python\s*=\s*[\"']([^\"']+)[\"']", re.M)


def _project_metadata(root: Path) -> dict[str, str]:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    name = _PROJECT_NAME_RE.search(text)
    requires = _REQUIRES_PYTHON_RE.search(text)
    if name is None or requires is None:
        raise ValueError("pyproject.toml is missing project name or requires-python")
    return {"name": name.group(1), "requires_python": requires.group(1)}


def _runtime_supported(requires_python: str | None = None) -> bool:
    """Return whether the active interpreter is in the declared project range."""
    if requires_python in {None, ">=3.10,<3.12"}:
        return (3, 10) <= sys.version_info[:2] < (3, 12)
    return sys.version_info[:2] == (3, 11)


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "argv": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def verify(root: Path) -> dict[str, Any]:
    root = root.resolve()
    metadata = _project_metadata(root)
    result: dict[str, Any] = {
        "schema_version": 1,
        "project": metadata,
        "python": sys.version.split()[0],
        "status": "NOT_RUN",
        "steps": [],
        "isolated": True,
        "network": "disabled_by_no_deps_and_no_index",
        "active_environment_modified": False,
    }
    if not _runtime_supported(metadata["requires_python"]):
        result.update(
            {
                "status": "BLOCKED_ENVIRONMENT",
                "reason": (
                    f"project requires {metadata['requires_python']}; "
                    "verifier interpreter is incompatible"
                ),
            }
        )
        return result
    if importlib.util.find_spec("build") is None:
        result.update(
            {
                "status": "BLOCKED_ENVIRONMENT",
                "reason": "local build module is unavailable",
            }
        )
        return result

    with tempfile.TemporaryDirectory(prefix="gemagents-install-") as raw:
        temporary = Path(raw)
        wheelhouse = temporary / "wheelhouse"
        wheelhouse.mkdir()
        build_step = _run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheelhouse),
            ],
            cwd=root,
        )
        result["steps"].append({"name": "build_wheel", **build_step})
        if build_step["returncode"] != 0:
            result.update({"status": "FAIL", "reason": "wheel build failed"})
            return result
        wheels = sorted(wheelhouse.glob(f"{metadata['name'].replace('-', '_')}-*.whl"))
        if len(wheels) != 1:
            result.update({"status": "FAIL", "reason": "wheel output is missing or ambiguous"})
            return result
        environment = temporary / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = environment / "bin" / "python"
        install_step = _run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])],
            cwd=temporary,
            env={**os.environ, "PIP_NO_INDEX": "1"},
        )
        result["steps"].append({"name": "pip_install_no_deps", **install_step})
        if install_step["returncode"] != 0:
            result.update({"status": "FAIL", "reason": "isolated wheel installation failed"})
            return result
        probe = _run(
            [
                str(python),
                "-c",
                "import importlib.metadata as m, GemAgents; "
                "print(m.version('gemagents')); print(GemAgents.__file__)",
            ],
            cwd=temporary,
            env={
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "PYTHONHOME"}
            },
        )
        result["steps"].append({"name": "installed_import_probe", **probe})
        if probe["returncode"] != 0:
            result.update({"status": "FAIL", "reason": "installed import probe failed"})
            return result
        with zipfile.ZipFile(wheels[0]) as archive:
            result["wheel_entries_checked"] = len(archive.namelist())
        result.update(
            {"status": "PASS", "reason": "isolated wheel installation and import probe passed"}
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    layout = RepoLayout(root)
    try:
        output = layout.writable(args.output)
    except (PermissionError, ValueError) as error:
        raise SystemExit(f"invalid verifier output path: {args.output}") from error
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing verifier output: {output}")
    try:
        payload = verify(root)
    except (OSError, UnicodeError, ValueError) as error:
        payload = {
            "schema_version": 1,
            "status": "FAIL",
            "reason": f"verifier setup failed: {type(error).__name__}",
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": payload["status"]}, ensure_ascii=False))
    return 0 if payload["status"] == "PASS" else (
        2 if payload["status"] == "BLOCKED_ENVIRONMENT" else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
