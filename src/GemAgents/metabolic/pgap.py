"""Pure PGAP boundary helpers shared by the compatibility facade."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.layout import RepoLayout
from GemAgents.metabolic.contracts import load_configuration


def pgap_prepare_windows(
    assets: Path,
    *,
    download_fn: Callable[[str, Path], None] | None = None,
    hash_fn: Callable[[Path], str] | None = None,
    json_fn: Callable[[Path, object], None] | None = None,
) -> None:
    if download_fn is None:
        from GemAgents.metabolic.network import metabolic_download

        download_fn = metabolic_download
    if hash_fn is None or json_fn is None:
        from GemAgents.metabolic.io.core import metabolic_hash, metabolic_json

        hash_fn = hash_fn or metabolic_hash
        json_fn = json_fn or metabolic_json
    assets.mkdir(parents=True, exist_ok=True)
    installer = assets / "wsl.2.7.13.0.x64.msi"
    url = "https://github.com/microsoft/WSL/releases/download/2.7.13/wsl.2.7.13.0.x64.msi"
    if not installer.is_file():
        download_fn(url, installer)
    json_fn(assets / "wsl-package.json", {"url": url, "sha256": hash_fn(installer)})
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$deploymentRoot = $PSScriptRoot\n"
        "$stateFile = Join-Path $deploymentRoot 'windows-prerequisites.js"
        "on'\n"
        "$logFile = Join-Path $deploymentRoot 'windows-bootstrap.log'\n"
        "$state = @{ status = 'running'; reboot_required = $false; steps "
        "= @() }\n"
        "function Save-State { $state | ConvertTo-Json -Depth 5 | Set-Con"
        "tent -LiteralPath $stateFile -Encoding UTF8 }\n"
        "Save-State\n"
        "Start-Transcript -LiteralPath $logFile -Append | Out-Null\n"
        "try {\n"
        "    $principal = [Security.Principal.WindowsPrincipal]::new([Sec"
        "urity.Principal.WindowsIdentity]::GetCurrent())\n"
        "    if (-not $principal.IsInRole([Security.Principal.WindowsBuil"
        "tInRole]::Administrator)) {\n"
        "        throw 'Windows administrator privileges are required to "
        "install WSL system components.'\n"
        "    }\n"
        "    $installer = Join-Path $deploymentRoot 'wsl.2.7.13.0.x64.msi"
        "'\n"
        "    $signature = Get-AuthenticodeSignature -LiteralPath $install"
        "er\n"
        "    if ($signature.Status -ne 'Valid' -or $signature.SignerCerti"
        "ficate.Subject -notmatch 'Microsoft Corporation') {\n"
        "        throw 'WSL installer Microsoft signature verification fa"
        "iled.'\n"
        "    }\n"
        "    foreach ($feature in @('VirtualMachinePlatform', 'Microsoft-"
        "Windows-Subsystem-Linux')) {\n"
        "        $current = Get-WindowsOptionalFeature -Online -FeatureNa"
        "me $feature\n"
        "        if ($current.State -ne 'Enabled') {\n"
        "            $result = Enable-WindowsOptionalFeature -Online -Fea"
        "tureName $feature -All -NoRestart\n"
        "            $state.reboot_required = $state.reboot_required -or "
        "$result.RestartNeeded\n"
        "        }\n"
        "        $state.steps += $feature\n"
        "        Save-State\n"
        "    }\n"
        "    $msiLog = Join-Path $deploymentRoot 'wsl-msi.log'\n"
        "    $arguments = @('/i', ('\"{0}\"' -f $installer), '/qn', '/nores"
        "tart', '/L*v', ('\"{0}\"' -f $msiLog))\n"
        "    $installation = Start-Process -FilePath msiexec.exe -Argumen"
        "tList $arguments -WindowStyle Hidden -Wait -PassThru\n"
        '    if ($installation.ExitCode -notin @(0, 3010)) { throw "WSL M'
        'SI installation failed: $($installation.ExitCode)" }\n'
        "    $state.reboot_required = $state.reboot_required -or ($instal"
        "lation.ExitCode -eq 3010)\n"
        "    $state.steps += 'Microsoft WSL 2.7.13'\n"
        "    $state.status = if ($state.reboot_required) { 'restart_requi"
        "red' } else { 'installed' }\n"
        "} catch {\n"
        "    $state.status = 'failed'\n"
        "    $state.error = $_.Exception.Message\n"
        "} finally {\n"
        "    Save-State\n"
        "    Stop-Transcript | Out-Null\n"
        "}\n"
        "# Deliberately never restart Windows: the user may have unsaved "
        "work.\n"
    )
    (assets / "windows-bootstrap.ps1").write_text(script, encoding="utf-8-sig")


def pgap_prepare_ubuntu(
    assets: Path,
    *,
    download_fn: Callable[[str, Path], None] | None = None,
    hash_fn: Callable[[Path], str] | None = None,
    json_fn: Callable[[Path, object], None] | None = None,
) -> None:
    if download_fn is None:
        from GemAgents.metabolic.network import metabolic_download

        download_fn = metabolic_download
    if hash_fn is None or json_fn is None:
        from GemAgents.metabolic.io.core import metabolic_hash, metabolic_json

        hash_fn = hash_fn or metabolic_hash
        json_fn = json_fn or metabolic_json
    assets.mkdir(parents=True, exist_ok=True)
    url = "https://releases.ubuntu.com/24.04.4/ubuntu-24.04.4-wsl-amd64.wsl"
    checksum = "9b2f7730dc68227dd04a9f3e5eab86ad85caf556b8606ad94f1f29ff5c4fd3f5"
    image = assets / "ubuntu-24.04-amd64.wsl"
    if not image.is_file() or hash_fn(image) != checksum:
        download_fn(url, image)
    if hash_fn(image) != checksum:
        raise ToolError("Ubuntu WSL image failed the published SHA256 check")
    json_fn(assets / "ubuntu-package.json", {"url": url, "sha256": checksum})


def pgap_decode(data: bytes) -> str:
    """Decode WSL management output without leaking platform encoding details."""
    if b"\x00" in data[:100]:
        return data.decode("utf-16-le", errors="replace").lstrip("\ufeff").strip()
    try:
        return data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return data.decode("mbcs" if os.name == "nt" else "gb18030", errors="replace").strip()


def pgap_call(
    command: list[str],
    *,
    log: Path | None = None,
    timeout: int = 120,
    decode_fn: Callable[[bytes], str] | None = None,
) -> str:
    """Execute a validated argument vector without shell interpolation.

    ``decode_fn`` is an explicit compatibility seam: the facade passes its
    historical decoder so callers that patched that symbol retain the old
    behavior while the subprocess boundary moves into this leaf.
    """
    decode = decode_fn or pgap_decode
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "timeout": timeout,
        "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    }
    if log is None:
        result = subprocess.run(command, capture_output=True, **kwargs)
        output = decode(result.stdout)
        if result.returncode:
            output += "\n" + decode(result.stderr)
    else:
        with log.open("ab") as handle:
            handle.write((json.dumps(command, ensure_ascii=False) + "\n").encode())
            handle.flush()
            result = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, **kwargs)
        output = f"See {log}"
    if result.returncode:
        raise ToolError(f"PGAP command failed ({result.returncode}): {output[-1800:]}")
    return output


def pgap_wsl_prefix(distro: str) -> list[str]:
    """Build an argument-vector-only WSL prefix for a validated distribution name."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", distro):
        raise ToolError("Invalid PGAP WSL distribution name")
    return ["wsl.exe", "--distribution", distro, "--user", "root", "--exec"]


def pgap_runtime_config(workspace: Path, config_path: str | None = None) -> dict:
    """Load and validate the pinned PGAP runtime descriptor without side effects."""
    path = Path(config_path) if config_path else RepoLayout(workspace).assets / "pgap/runtime.json"
    if not path.is_file():
        return {}
    config = load_configuration(path)
    if not isinstance(config, dict) or config.get("runtime") not in {"wsl", "local"}:
        raise ToolError("Invalid PGAP runtime configuration")
    allowed = {
        "runtime",
        "distro",
        "home",
        "version",
        "cpus",
        "memory",
        "launcher_sha256",
        "launcher_url",
        "image_digests",
    }
    if set(config) - allowed:
        raise ToolError("Unknown fields in PGAP runtime configuration")
    if not isinstance(config.get("version"), str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", config["version"]
    ):
        raise ToolError("PGAP runtime requires a pinned version")
    if config.get("runtime") == "wsl":
        pgap_wsl_prefix(config.get("distro", "GemAgents-PGAP"))
        home = config.get("home", "")
        if (
            not isinstance(home, str)
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", home)
            or ".." in home.split("/")
        ):
            raise ToolError("PGAP Linux installation path must be absolute")
    return config


def pgap_check(
    workspace: Path,
    *,
    call_fn: Callable[..., str],
) -> dict:
    """Check the configured PGAP runtime through an injected command runner.

    The leaf owns readiness semantics while the compatibility facade supplies
    the historical subprocess seam.  This keeps the check deterministic in
    tests and prevents monkeypatched ``GemAgents.tools.pgap_call`` from being
    bypassed during the incremental extraction.
    """
    runtime = pgap_runtime_config(workspace)
    result = {"ready": False, "runtime": runtime, "errors": []}
    try:
        if os.name == "nt":
            result["wsl_version"] = call_fn(["wsl.exe", "--version"])
            result["distributions"] = call_fn(["wsl.exe", "--list", "--quiet"]).splitlines()
        if not runtime:
            raise ToolError("PGAP runtime is not configured; run --setup-pgap")
        prefix = pgap_wsl_prefix(runtime["distro"]) if runtime["runtime"] == "wsl" else []
        result["docker_version"] = call_fn(
            prefix + ["docker", "info", "--format", "{{.ServerVersion}}"]
        )
        result["database_version"] = call_fn(prefix + ["cat", runtime["home"] + "/data/VERSION"])
        if result["database_version"] != runtime["version"]:
            raise ToolError("Installed PGAP data version differs from the pinned runtime version")
        marker = f"{runtime['home']}/data/input-{runtime['version']}/.pgap_complete"
        call_fn(prefix + ["test", "-f", marker])
        call_fn(prefix + ["docker", "image", "inspect", f"ncbi/pgap:{runtime['version']}"])
        result["ready"] = True
    except (ToolError, OSError, subprocess.TimeoutExpired) as error:
        result["errors"].append(str(error))
    return result


def pgap_run(
    input_path: Path,
    out: Path,
    config: dict,
    workspace: Path | None = None,
    *,
    runtime_config_fn: Callable[..., dict] | None = None,
    fasta_fn: Callable[..., list[tuple[str, str]]] | None = None,
    write_fasta_fn: Callable[..., None] | None = None,
    wsl_prefix_fn: Callable[..., list[str]] | None = None,
    call_fn: Callable[..., str] | None = None,
    json_fn: Callable[..., None] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    adapter_register_fn: Callable[[dict], None] | None = None,
    adapter_clear_fn: Callable[[dict], None] | None = None,
) -> Path:
    """Run PGAP with injected seams for the historical compatibility facade."""
    if runtime_config_fn is None:
        runtime_config_fn = pgap_runtime_config
    if fasta_fn is None or write_fasta_fn is None:
        from GemAgents.metabolic.sequence import metabolic_fasta, metabolic_write_fasta

        fasta_fn = fasta_fn or metabolic_fasta
        write_fasta_fn = write_fasta_fn or metabolic_write_fasta
    if wsl_prefix_fn is None:
        wsl_prefix_fn = pgap_wsl_prefix
    if call_fn is None:
        call_fn = pgap_call
    if json_fn is None:
        from GemAgents.metabolic.io import metabolic_json

        json_fn = metabolic_json
    if which_fn is None:
        which_fn = shutil.which

    runtime_root = workspace or Path(str(config.get("workspace", out.parent)))
    runtime = runtime_config_fn(runtime_root, config.get("pgap_config"))
    if not config.get("organism"):
        raise ToolError("PGAP requires a valid organism name")
    cpu = int(config.get("cpus", runtime.get("cpus", 2)))
    memory = str(config.get("pgap_memory", runtime.get("memory", "6g")))
    if cpu < 1 or not re.fullmatch(r"[1-9]\d*(?:[bkmg])?", memory.lower()):
        raise ToolError("Invalid PGAP CPU or memory limit")
    normalized = out / "pgap-input.fna"
    write_fasta_fn(fasta_fn(input_path, "fna"), normalized)
    prefix = wsl_prefix_fn(runtime["distro"]) if runtime.get("runtime") == "wsl" else []
    container_name = "gemagents_pgap_" + uuid.uuid4().hex
    if prefix:
        container_runtime = "docker"
    else:
        container_runtime = next(
            (x for x in ("docker", "podman", "singularity", "apptainer") if which_fn(x)),
            None,
        )
        if container_runtime is None:
            raise ToolError("PGAP container runtime is unavailable; run --setup-pgap")
    adapter = {
        "kind": "pgap_container",
        "identity": container_name,
        "runtime": "wsl" if prefix else "local",
        "distro": runtime.get("distro") if prefix else None,
        "container_runtime": container_runtime,
    }
    registered = False
    if adapter_register_fn is not None:
        adapter_register_fn(adapter)
        registered = True
    version_args = ["--use-version", runtime["version"]] if runtime.get("version") else []
    environment = ["env", f"PGAP_INPUT_DIR={runtime['home']}/data"] if runtime else []
    pgap_out = out / "pgap"
    if prefix:
        # Container bind mounts refer to the Linux filesystem, so stage computation on ext4.
        job = runtime["home"] + "/jobs/" + container_name
        call_fn(prefix + ["mkdir", "-p", job])
        source = call_fn(prefix + ["wslpath", "-a", "-u", str(normalized)])
        destination = call_fn(prefix + ["wslpath", "-a", "-u", str(pgap_out)])
        call_fn(prefix + ["cp", "--", source, job + "/genome.fna"])
        script = runtime["home"] + "/pgap.py"
        if config.get("pgap_script"):
            source_script = call_fn(prefix + ["wslpath", "-a", "-u", config["pgap_script"]])
            call_fn(prefix + ["cp", "--", source_script, job + "/pgap.py"])
            script = job + "/pgap.py"
        genome, output, python = job + "/genome.fna", job + "/output", "python3"
        try:
            call_fn(prefix + ["docker", "info", "--format", "{{.ServerVersion}}"])
        except ToolError:
            call_fn(prefix + ["/usr/sbin/service", "docker", "start"], log=out / "pgap.log")
    else:
        script = str(Path(config.get("pgap_script", runtime.get("home", ".") + "/pgap.py")))
        if not Path(script).is_file():
            raise ToolError("PGAP launcher is missing; run --setup-pgap or supply pgap_script")
        genome, output, python = str(normalized), str(pgap_out), sys.executable
    command = (
        prefix
        + environment
        + [
            python,
            script,
            "--no-self-update",
            *version_args,
            "-n",
            "-g",
            genome,
            "-s",
            str(config["organism"]),
            "-o",
            output,
            "--cpus",
            str(cpu),
            "--memory",
            memory,
            "--container-name",
            container_name,
        ]
    )
    execution = {
        "command": command,
        "runtime": runtime,
        "cpus": cpu,
        "memory": memory,
        "container": container_name,
        "linux_output": output if prefix else None,
        "container_runtime": container_runtime,
    }
    json_fn(out / "pgap-command.json", command)
    json_fn(out / "pgap-execution.json", execution)
    try:
        call_fn(command, log=out / "pgap.log", timeout=int(config.get("timeout", 7200)))
    except subprocess.TimeoutExpired:
        # Remove only this run's explicitly named container, never unrelated jobs.
        try:
            call_fn(
                prefix
                + [execution["container_runtime"], "rm", "-f", container_name],
                log=out / "pgap.log",
            )
            if registered and adapter_clear_fn is not None:
                adapter_clear_fn(adapter)
                registered = False
        except (ToolError, OSError, subprocess.TimeoutExpired):
            pass
        raise ToolError(
            "PGAP timed out; inspect pgap.log and the recorded Linux job directory"
        ) from None
    finally:
        if prefix:
            try:
                call_fn(
                    prefix + ["cp", "-a", "--", output, destination],
                    log=out / "pgap.log",
                    timeout=600,
                )
            except (ToolError, OSError, subprocess.TimeoutExpired) as error:
                execution["copy_error"] = str(error)
                json_fn(out / "pgap-execution.json", execution)
    gbk = pgap_out / "annot.gbk"
    if not gbk.is_file() or gbk.stat().st_size == 0:
        raise ToolError(
            "PGAP produced no local annot.gbk; inspect pgap.log and pgap-execution.json"
        )
    if registered and adapter_clear_fn is not None:
        adapter_clear_fn(adapter)
    return gbk


def cleanup_pgap_adapter(
    adapter: dict,
    *,
    call_fn: Callable[..., str] | None = None,
) -> dict:
    """Remove one validated PGAP container left by a terminated worker.

    The adapter record contains only a generated container identity and a
    pinned runtime selector.  It never accepts a free-form command or shell
    string, and rejects identities that are not generated by this integration.
    """
    if not isinstance(adapter, dict) or adapter.get("kind") != "pgap_container":
        raise ToolError("unsupported adapter identity")
    identity = adapter.get("identity")
    if not isinstance(identity, str) or not re.fullmatch(r"gemagents_pgap_[0-9a-f]{32}", identity):
        raise ToolError("invalid PGAP container identity")
    runtime = adapter.get("runtime")
    if runtime not in {"local", "wsl"}:
        raise ToolError("invalid PGAP adapter runtime")
    executable = adapter.get("container_runtime", "docker")
    if executable not in {"docker", "podman", "singularity", "apptainer"}:
        raise ToolError("invalid PGAP container runtime")
    prefix = pgap_wsl_prefix(adapter["distro"]) if runtime == "wsl" else []
    call = call_fn or pgap_call
    call(prefix + [executable, "rm", "-f", identity], timeout=30)
    return {
        "delivery": "adapter_removed",
        "kind": "pgap_container",
        "identity": identity,
        "runtime": runtime,
    }


def pgap_setup(
    workspace: Path,
    *,
    version: str = "2026-06-18.build8602",
    distro: str = "GemAgents-PGAP",
    linux_home: str = "/opt/gemagents-pgap",
    call_fn: Callable[..., str] | None = None,
    download_fn: Callable[[str, Path], None] | None = None,
    hash_fn: Callable[[Path], str] | None = None,
    json_fn: Callable[[Path, object], None] | None = None,
    prepare_windows_fn: Callable[[Path], None] | None = None,
    prepare_ubuntu_fn: Callable[[Path], None] | None = None,
    runtime_check_fn: Callable[[Path], dict] | None = None,
    platform_name: str | None = None,
    geteuid_fn: Callable[[], int] | None = None,
) -> dict:
    """Install the isolated PGAP runtime through explicit side-effect seams."""
    if call_fn is None:
        call_fn = pgap_call
    if download_fn is None:
        from GemAgents.metabolic.network import metabolic_download

        download_fn = metabolic_download
    if hash_fn is None or json_fn is None:
        from GemAgents.metabolic.io import metabolic_hash, metabolic_json

        hash_fn = hash_fn or metabolic_hash
        json_fn = json_fn or metabolic_json
    if prepare_windows_fn is None:
        def prepare_windows_fn(_assets: Path) -> None:
            raise ToolError("Windows PGAP prerequisite callback is not configured")

    if prepare_ubuntu_fn is None:
        def prepare_ubuntu_fn(_assets: Path) -> None:
            raise ToolError("Ubuntu PGAP preparation callback is not configured")

    if runtime_check_fn is None:
        def runtime_check_fn(target: Path) -> dict:
            return pgap_check(target, call_fn=call_fn)
    platform_name = os.name if platform_name is None else platform_name
    geteuid_fn = getattr(os, "geteuid", lambda: 0) if geteuid_fn is None else geteuid_fn

    root = RepoLayout(workspace).assets / "pgap"
    assets = root / "deployment"
    assets.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "version": version, "phase": "preflight"}
    log = assets / "linux-install.log"

    def phase(name: str):
        report["phase"] = name
        json_fn(assets / "setup-state.json", report)
        print(f"[pgap setup] {name}", flush=True)

    try:
        phase("preflight")
        if platform_name == "nt":
            try:
                call_fn(["wsl.exe", "--version"])
            except (ToolError, OSError):
                prepare_windows_fn(assets)
                report.update(
                    status="requires_windows_administrator",
                    administrator_script=str(assets / "windows-bootstrap.ps1"),
                    next_step="Run the prepared script as administrator, then rerun --setup-pgap",
                )
                phase("windows_prerequisites")
                return report
            distributions = call_fn(["wsl.exe", "--list", "--quiet"]).splitlines()
            if distro not in [name.strip() for name in distributions]:
                phase("download_linux")
                prepare_ubuntu_fn(assets)
                phase("import_linux")
                try:
                    call_fn(
                        [
                            "wsl.exe",
                            "--import",
                            distro,
                            str(root / "wsl"),
                            str(assets / "ubuntu-24.04-amd64.wsl"),
                            "--version",
                            "2",
                        ],
                        log=log,
                        timeout=600,
                    )
                except ToolError:
                    report.update(
                        status="windows_runtime_not_ready",
                        next_step="Inspect linux-install.log; Windows may require restart",
                    )
                    phase("import_linux_failed")
                    return report
            prefix = pgap_wsl_prefix(distro)
        else:
            if geteuid_fn() != 0:
                raise ToolError("Linux PGAP setup needs root for Docker package installation")
            prefix = []
        phase("install_linux_packages")
        call_fn(prefix + ["apt-get", "update"], log=log, timeout=1200)
        call_fn(
            prefix
            + [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "apt-get",
                "install",
                "-y",
                "python3",
                "ca-certificates",
                "curl",
                "docker.io",
                "aria2",
            ],
            log=log,
            timeout=1800,
        )
        proxy_script = (
            "import json,os; from pathlib import Path; "
            "p=Path('/etc/docker/daemon.json'); "
            "d=json.loads(p.read_text()) if p.exists() else {}; "
            "proxy=os.environ.get('https_proxy') or os.environ.get('http_proxy'); "
            "change=bool(proxy) and 'proxies' not in d; "
            "d.update({'proxies':{'http-proxy':proxy,'https-proxy':proxy,"
            "'no-proxy':'localhost,127.0.0.1,::1'}}) if change else None; "
            "p.write_text(json.dumps(d,indent=2)) if change else None; "
            "print('restart' if change else 'start')"
        )
        action = call_fn(prefix + ["python3", "-c", proxy_script])
        call_fn(prefix + ["/usr/sbin/service", "docker", action], log=log, timeout=120)
        call_fn(prefix + ["docker", "info"], log=log)
        call_fn(prefix + ["mkdir", "-p", linux_home + "/data", linux_home + "/jobs"])
        launcher = assets / "pgap.py"
        url = f"https://raw.githubusercontent.com/ncbi/pgap/{version}/scripts/pgap.py"
        download_fn(url, launcher)
        source = (
            call_fn(prefix + ["wslpath", "-a", "-u", str(launcher)])
            if prefix
            else str(launcher)
        )
        call_fn(prefix + ["cp", "--", source, linux_home + "/pgap.py"])
        phase("pull_pgap_container")
        call_fn(prefix + ["docker", "pull", f"ncbi/pgap:{version}"], log=log, timeout=7200)
        marker = f"{linux_home}/data/input-{version}/.pgap_complete"
        try:
            call_fn(prefix + ["test", "-f", marker])
            data_installed = True
        except ToolError:
            data_installed = False
        if not data_installed:
            phase("download_pgap_database")
            name = f"input-{version}.tgz"
            try:
                call_fn(prefix + ["test", "-f", f"{linux_home}/data/{name}"])
            except ToolError:
                call_fn(
                    prefix
                    + [
                        "aria2c",
                        "--continue=true",
                        "--split=8",
                        "--max-connection-per-server=8",
                        "--min-split-size=4M",
                        "--file-allocation=none",
                        "--auto-file-renaming=false",
                        "--summary-interval=30",
                        "--max-tries=8",
                        "--retry-wait=5",
                        f"--dir={linux_home}/data",
                        f"--out={name}.download",
                        f"https://ncbi-pgap.s3.amazonaws.com/input_data/{name}",
                    ],
                    log=log,
                    timeout=14400,
                )
                call_fn(
                    prefix
                    + [
                        "mv",
                        "--",
                        f"{linux_home}/data/{name}.download",
                        f"{linux_home}/data/{name}",
                    ]
                )
        phase("install_pgap_database_and_container")
        call_fn(
            prefix
            + [
                "env",
                f"PGAP_INPUT_DIR={linux_home}/data",
                "python3",
                linux_home + "/pgap.py",
                "--use-version",
                version,
                "--no-self-update",
                "-n",
                "--quiet",
            ],
            log=log,
            timeout=14400,
        )
        runtime = {
            "runtime": "wsl" if prefix else "local",
            "distro": distro if prefix else None,
            "home": linux_home,
            "version": version,
            "cpus": 2,
            "memory": "6g",
            "launcher_sha256": hash_fn(launcher),
            "launcher_url": url,
        }
        runtime["image_digests"] = json.loads(
            call_fn(
                prefix
                + [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    f"ncbi/pgap:{version}",
                ]
            )
        )
        json_fn(root / "runtime.json", runtime)
        phase("verify")
        verification = runtime_check_fn(workspace)
        report["verification"] = verification
        report["status"] = "ready" if verification["ready"] else "verification_failed"
        phase("finished")
        return report
    except Exception as error:
        report.update(status="failed", error=str(error))
        phase("failed")
        raise
