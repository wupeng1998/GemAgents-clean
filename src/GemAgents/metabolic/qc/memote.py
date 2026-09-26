"""Isolated MEMOTE worker and report orchestration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from xml.etree import ElementTree

from GemAgents.errors import ToolError
from GemAgents.metabolic.io import metabolic_hash, metabolic_json


def metabolic_memote_worker(
    model_path: Path,
    out: Path,
    solver_timeout: int = 10,
    *,
    hash_fn: Callable[[Path], str] = metabolic_hash,
    json_fn: Callable[[Path, object], None] = metabolic_json,
) -> dict:
    """Run the public MEMOTE suite and preserve its default weighted scoring."""
    import cobra
    from memote.suite import api
    from memote.suite.reporting.config import ReportConfiguration
    from memote.suite.reporting.snapshot import SnapshotReport
    from memote.utils import jsonify

    cobra.Configuration().solver = "glpk"
    # COBRA FVA defaults to all CPUs and starts process pools on Windows.
    # Keep the already isolated MEMOTE worker single-process and reproducible.
    cobra.Configuration().processes = 1
    started = time.perf_counter()
    model, sbml_version, notifications = api.validate_model(str(model_path))
    json_fn(out / "sbml-validation.json", notifications)
    if model is None:
        raise ToolError("MEMOTE cannot load this SBML; see sbml-validation.json")
    code, results = api.test_model(
        model,
        sbml_version=sbml_version,
        results=True,
        pytest_args=[
            "-v",
            "--tb=short",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            f"--junitxml={out / 'junit.xml'}",
        ],
        solver_timeout=solver_timeout,
    )
    (out / "results.json").write_text(jsonify(results, pretty=True), encoding="utf-8")
    if int(code) not in {0, 1}:
        raise ToolError(f"MEMOTE suite did not complete (pytest exit {code})")
    junit = ElementTree.parse(out / "junit.xml")
    errors = []
    for case in junit.iter("testcase"):
        for outcome in case:
            message = outcome.attrib.get("message", "")
            expected = message.startswith(("AssertionError", "assert ", "Failed:", "DID NOT RAISE"))
            if outcome.tag == "error" or (outcome.tag == "failure" and not expected):
                errors.append({"test": case.attrib.get("name"), "message": message[:1000]})
    configuration = ReportConfiguration.load()
    weighted_tests = {
        case
        for section in configuration["cards"]["scored"]["sections"].values()
        for case in section.get("cases", [])
    }
    weighted_errors = [e for e in errors if e["test"].split("[")[0] in weighted_tests]
    missing = sorted(weighted_tests - set(results.cases))
    score_complete = not weighted_errors and not missing
    report = SnapshotReport(result=results, configuration=configuration)
    scored = json.loads(report.render_json())
    (out / "report.html").write_text(report.render_html(), encoding="utf-8")
    configuration_path = Path(str(files("memote.suite.templates") / "test_config.yml"))
    shutil.copyfile(configuration_path, out / "score-config.yml")
    summary = {
        "status": (
            "incomplete"
            if not score_complete
            else "completed_with_test_errors"
            if errors
            else "completed"
        ),
        "memote_version": version("memote"),
        "model_sha256": hash_fn(model_path),
        "score_config_sha256": hash_fn(out / "score-config.yml"),
        "score_percent": 100 * scored["score"]["total_score"] if score_complete else None,
        "score_complete": score_complete,
        "full_suite_complete": not errors,
        "weighted_test_errors": weighted_errors,
        "missing_weighted_tests": missing,
        "sections": scored["score"]["sections"],
        "pytest_exit_code": int(code),
        "test_errors": errors,
        "elapsed_seconds": time.perf_counter() - started,
        "solver_timeout": solver_timeout,
        "processes": 1,
        "scoring": "Public MEMOTE default weights; no private pear implementation",
        "scope": "Model consistency and annotation score; not biological accuracy or CER clearance",
        "report": str(out / "report.html"),
    }
    json_fn(out / "summary.json", summary)
    return summary


def metabolic_memote(
    model_path: Path,
    out: Path,
    config: dict | None = None,
    *,
    hash_fn: Callable[[Path], str] = metabolic_hash,
    json_fn: Callable[[Path, object], None] = metabolic_json,
    run_fn: Callable[..., object] | None = None,
    python_executable: str | None = None,
    platform_name: str | None = None,
) -> dict:
    """Run MEMOTE in an isolated subprocess and preserve incomplete results."""
    config = config or {}
    if out.exists() and any(out.iterdir()):
        raise ToolError("MEMOTE output directory must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    model_path, out = model_path.resolve(), out.resolve()
    if not model_path.is_file():
        raise ToolError(f"SBML model does not exist: {model_path}")
    timeout = int(config.get("memote_timeout", 1800))
    solver_timeout = int(config.get("memote_solver_timeout", 10))
    if timeout < 1 or solver_timeout < 1:
        raise ToolError("MEMOTE timeouts must be positive")
    command = [
        python_executable or sys.executable,
        "-m",
        "GemAgents",
        "--memote-worker",
        str(model_path),
        "--output-dir",
        str(out),
        "--memote-solver-timeout",
        str(solver_timeout),
    ]
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    # The worker runs from the report directory so a relative PYTHONPATH=src
    # no longer points at this checkout. Always pass the package root.
    package_root = str(Path(__file__).resolve().parents[3])
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        [package_root, existing_pythonpath] if existing_pythonpath else [package_root]
    )
    run_fn = subprocess.run if run_fn is None else run_fn
    platform_name = os.name if platform_name is None else platform_name
    try:
        with (out / "memote.log").open("wb") as handle:
            result = run_fn(
                command,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=out,
                env=environment,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if platform_name == "nt" else 0,
            )
        if (out / "summary.json").is_file():
            return json.loads((out / "summary.json").read_text(encoding="utf-8"))
        if result.returncode or not (out / "summary.json").is_file():
            raise ToolError(f"MEMOTE worker failed ({result.returncode}); see {out / 'memote.log'}")
        return json.loads((out / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ToolError, subprocess.TimeoutExpired) as error:
        summary = {
            "status": "incomplete",
            "score_percent": None,
            "error": str(error),
            "model_sha256": hash_fn(model_path),
            "log": str(out / "memote.log"),
        }
        json_fn(out / "summary.json", summary)
        return summary


__all__ = ["metabolic_memote", "metabolic_memote_worker"]
