from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_PROBE_TIMEOUT = 1.0
DEFAULT_START_TIMEOUT = 20.0
_START_LOCK = threading.Lock()
_OPENER = build_opener(ProxyHandler({}))


class OllamaError(RuntimeError):
    """Raised when a local Ollama runtime cannot be made ready."""


def is_ollama_endpoint(base_url: str | None) -> bool:
    """Return whether an endpoint is a local Ollama HTTP endpoint."""
    if not base_url:
        return False
    try:
        normalized = _server_base_url(base_url)
        parts = urlsplit(normalized)
    except (OllamaError, ValueError):
        return False

    hostname = (parts.hostname or "").lower()
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    configured = os.getenv("OLLAMA_BASE_URL")
    if configured:
        try:
            if normalized == _server_base_url(configured):
                return True
        except (OllamaError, ValueError):
            return False
    return parts.port == 11434


def ensure_ollama_running(base_url: str | None, *, model: str | None = None) -> None:
    """Probe local Ollama and start ``ollama serve`` when it is unavailable.

    The function only acts on local Ollama endpoints. It never downloads a
    model; the configured model must already be present in Ollama.
    """
    if not is_ollama_endpoint(base_url):
        return

    endpoint = _server_base_url(base_url)
    if _probe_ollama(endpoint):
        return

    with _START_LOCK:
        if _probe_ollama(endpoint):
            return
        if not _auto_start_enabled():
            raise OllamaError(
                f"Local Ollama is not running at {endpoint}; "
                "automatic startup is disabled by OLLAMA_AUTO_START=0."
            )

        executable = _find_ollama_executable()
        timeout = _start_timeout()
        command = [executable, "serve"]
        print(
            f"GemAgents: Ollama is not running at {endpoint}; starting "
            f"{executable} serve.",
            file=sys.stderr,
        )
        try:
            process = _start_ollama(command)
        except OSError as error:
            raise OllamaError(
                f"Could not start Ollama with {executable!r}: {error}"
            ) from error

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if _probe_ollama(endpoint):
                    return
                return_code = process.poll()
                if return_code is not None:
                    raise OllamaError(
                        f"Ollama exited with status {return_code} before "
                        f"becoming ready at {endpoint}."
                    )
                time.sleep(0.2)
        except OllamaError:
            _stop_process(process)
            raise

        _stop_process(process)
        model_hint = f" for model {model!r}" if model else ""
        raise OllamaError(
            f"Started Ollama{model_hint}, but it did not become ready at "
            f"{endpoint} within {timeout:g} seconds."
        )


def _server_base_url(base_url: str | None) -> str:
    raw = (base_url or os.getenv("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL).strip()
    if not raw:
        raw = DEFAULT_OLLAMA_BASE_URL
    parts = urlsplit(raw.rstrip("/"))
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise OllamaError(f"Invalid Ollama base URL: {base_url!r}")
    path = parts.path.rstrip("/")
    if path == "/v1":
        path = ""
    elif path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _probe_ollama(endpoint: str, timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
    request = Request(
        f"{endpoint}/api/tags",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            return 200 <= int(status) < 300
    except (OSError, URLError, TimeoutError, ValueError):
        return False


def _auto_start_enabled() -> bool:
    value = os.getenv("OLLAMA_AUTO_START", "1").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise OllamaError(
        "OLLAMA_AUTO_START must be one of: 1, 0, true, false, yes, no, on, off."
    )


def _start_timeout() -> float:
    raw = os.getenv("OLLAMA_START_TIMEOUT", str(DEFAULT_START_TIMEOUT)).strip()
    try:
        timeout = float(raw)
    except ValueError as error:
        raise OllamaError("OLLAMA_START_TIMEOUT must be a positive number.") from error
    if timeout <= 0:
        raise OllamaError("OLLAMA_START_TIMEOUT must be a positive number.")
    return timeout


def _find_ollama_executable() -> str:
    configured = os.getenv("OLLAMA_EXECUTABLE", "").strip()
    executable = shutil.which(configured or "ollama")
    if executable:
        return executable
    if configured:
        raise OllamaError(
            f"OLLAMA_EXECUTABLE does not point to an executable: {configured!r}."
        )
    raise OllamaError(
        "Local Ollama is not running and the 'ollama' executable was not found on PATH. "
        "Install Ollama or set OLLAMA_EXECUTABLE."
    )


def _start_ollama(command: list[str]) -> subprocess.Popen[bytes]:
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
