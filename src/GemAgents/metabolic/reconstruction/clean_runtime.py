"""Run the CLEAN predictor for the current reconstruction input."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from GemAgents.errors import ToolError


def discover_clean_runtime(workspace: Path | None) -> tuple[Path, Path] | None:
    """Return the local CLEAN app and interpreter when both are usable.

    CLEAN is an optional external research dependency.  The discovery list is
    deliberately narrow and does not treat a historical prediction table as a
    runtime.  Users can provide ``clean_runtime`` and ``clean_python`` in a
    reconstruction config when CLEAN is installed elsewhere.
    """
    if workspace is None:
        return None
    app_candidates = (
        workspace.parent / "CLEAN" / "app",
        workspace / "CLEAN" / "app",
    )
    python_candidates = (
        Path("<CLEAN_PYTHON>"),
        Path(sys.executable),
    )
    for app in app_candidates:
        if not (app / "CLEAN_infer_fasta.py").is_file():
            continue
        for executable in python_candidates:
            if executable.is_file():
                return app.resolve(), executable.resolve()
    return None


def run_clean_predictions(
    *,
    clean_fasta: Path,
    out: Path,
    runtime: Path,
    python_executable: Path,
    timeout: int,
) -> Path:
    """Run CLEAN on one generated FASTA and copy its raw output into the run.

    The upstream script uses fixed ``data/inputs`` and ``results/inputs``
    directories.  A content-derived name isolates concurrent GemAgents jobs,
    while the copied FASTA, raw table and command log remain under the run
    directory as reproducible evidence.
    """
    script = runtime / "CLEAN_infer_fasta.py"
    if not script.is_file():
        raise ToolError(f"CLEAN runtime script is missing: {script}")
    if not python_executable.is_file():
        raise ToolError(f"CLEAN Python executable is missing: {python_executable}")
    if timeout < 1:
        raise ToolError("clean_timeout must be a positive integer")

    digest = hashlib.sha256(clean_fasta.read_bytes()).hexdigest()[:16]
    name = f"gemagents_{digest}"
    # CLEAN_infer_fasta.py appends ``.fasta`` to the supplied basename.
    input_path = runtime / "data" / "inputs" / f"{name}.fasta"
    result_path = runtime / "results" / "inputs" / f"{name}_maxsep.csv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(clean_fasta, input_path)

    command = [str(python_executable), str(script), "--fasta_data", name]
    # CLEAN_infer_fasta.py invokes its ESM helper through the bare ``python``
    # command.  A GemAgents worker often runs in another conda environment,
    # so make the selected CLEAN environment win PATH resolution for both the
    # top-level script and its child process.
    environment = os.environ.copy()
    environment["PATH"] = f"{python_executable.parent}{os.pathsep}{environment.get('PATH', '')}"
    try:
        completed = subprocess.run(
            command,
            cwd=runtime,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        log = out / "clean.log"
        log.write_text(
            f"command: {command!r}\nstatus: timeout\n{error}\n",
            encoding="utf-8",
        )
        raise ToolError(f"CLEAN prediction timed out after {timeout}s; see {log}") from error

    log = out / "clean.log"
    log.write_text(
        f"command: {command!r}\nreturncode: {completed.returncode}\n"
        f"stdout:\n{completed.stdout or ''}\nstderr:\n{completed.stderr or ''}",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise ToolError(
            f"CLEAN prediction failed with exit code {completed.returncode}; see {log}"
        )
    if not result_path.is_file():
        raise ToolError(f"CLEAN produced no prediction table; see {log}")

    raw_output = out / "clean-predictions-raw.csv"
    shutil.copy2(result_path, raw_output)
    # The raw table is now preserved in the run directory.  Remove the
    # per-run files from CLEAN's shared scratch directories so repeated jobs
    # do not accumulate inputs or accidentally reuse a previous result.
    input_path.unlink(missing_ok=True)
    result_path.unlink(missing_ok=True)
    return raw_output


__all__ = ["discover_clean_runtime", "run_clean_predictions"]
