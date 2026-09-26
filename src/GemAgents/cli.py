from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import readline as _readline
except ImportError:  # pragma: no cover - Windows and minimal Python builds
    _readline = None

from GemAgents.layout import RepoLayout, resolve_workspace

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from rich.console import Console

    from GemAgents.agent import run_agent, run_agent_stream
    from GemAgents.config import load_config
    from GemAgents.extensions import (
        Extension,
        load_agents,
        load_commands,
        load_output_styles,
        load_skills,
        render_command,
    )
    from GemAgents.file_history import FileHistory
    from GemAgents.hooks import HookBlocked, run_hooks
    from GemAgents.mcp import McpRegistry
    from GemAgents.permissions import PermissionMode
    from GemAgents.session import Session, SessionStore
    from GemAgents.settings import RuntimeSettings
    from GemAgents.state import RuntimeState


def _ensure_runtime_imports() -> None:
    """Load interactive dependencies only when a runtime helper is used."""
    global FileHistory, HookBlocked, LocalToolRunner, PermissionMode, ToolError, run_hooks
    if "PermissionMode" not in globals():
        from GemAgents.file_history import FileHistory
        from GemAgents.hooks import HookBlocked, run_hooks
        from GemAgents.permissions import PermissionMode
        from GemAgents.tools import LocalToolRunner, ToolError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gemagents",
        description="Pure Python LangGraph coding agent.",
    )
    parser.add_argument("-v", "--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "-p",
        "--print",
        dest="print_prompt",
        nargs="?",
        help="run one prompt and exit",
    )
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text")
    parser.add_argument(
        "--thread-id",
        help="durable stream thread id; reuse it to replay a completed stream",
    )
    parser.add_argument(
        "--approval-id",
        help="workspace-bound approval token for an inspect-before-reconstruct continuation",
    )
    parser.add_argument(
        "--inspection-result",
        help="JSON inspection evidence path for an approved continuation",
    )
    parser.add_argument(
        "--execute-approved",
        action="store_true",
        help="submit an inspect-approved reconstruction without another model turn",
    )
    parser.add_argument("--model", help="model profile id or raw model name")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL")
    parser.add_argument("--api-key", help="model API key")
    parser.add_argument("--workspace", help="workspace path; defaults to current directory")
    parser.add_argument("--settings", help="extra settings.json path")
    parser.add_argument("--image", action="append", default=[], help="attach an image path")
    parser.add_argument("--max-steps", type=int, default=20, help="maximum tool loop steps")
    parser.add_argument(
        "--max-tool-calls", type=int, default=50, help="maximum executed tool calls"
    )
    parser.add_argument(
        "--max-wall-seconds", type=float, default=900.0, help="maximum agent wall time"
    )
    parser.add_argument("--max-input-tokens", type=int, help="maximum provider input tokens")
    parser.add_argument("--max-output-tokens", type=int, help="maximum provider output tokens")
    parser.add_argument("--max-total-tokens", type=int, help="maximum provider total tokens")
    parser.add_argument("--max-cost-usd", type=float, help="maximum reported provider cost in USD")
    parser.add_argument("--max-memory-mb", type=float, help="maximum agent process memory")
    parser.add_argument(
        "--tool-profile",
        choices=["scientist", "developer"],
        default="scientist",
        help="scientist exposes domain tools; developer adds editing and shell tools",
    )
    parser.add_argument(
        "--shell-timeout",
        type=int,
        default=30,
        help="shell command timeout in seconds",
    )
    parser.add_argument(
        "--permission-mode",
        choices=["default", "plan", "auto"],
        default="auto",
        help=(
            "default/auto execute requested tools, plan denies mutations"
        ),
    )
    parser.add_argument("--plan", action="store_true", help="shortcut for --permission-mode plan")
    parser.add_argument("--auto", action="store_true", help="shortcut for --permission-mode auto")
    parser.add_argument("--resume", nargs="?", const="", help="resume latest or a specific session")
    parser.add_argument(
        "--dump-system-prompt",
        action="store_true",
        help="print system prompt and exit",
    )
    parser.add_argument("--agent-teams", action="store_true", help="enable agent-team tool surface")
    parser.add_argument("--reconstruct", help="reconstruct a metabolic model from a FAA/FNA file")
    parser.add_argument(
        "--reconstruct-config", help="run a metabolic reconstruction JSON configuration"
    )
    parser.add_argument(
        "--prepare-ncbi-hmms", metavar="DIRECTORY", help="download and prepare NCBI enzyme HMMs"
    )
    parser.add_argument(
        "--setup-pgap", action="store_true", help="install or resume PGAP deployment"
    )
    parser.add_argument("--check-pgap", action="store_true", help="check PGAP runtime and database")
    parser.add_argument("--pgap-config", help="PGAP runtime configuration JSON path")
    parser.add_argument("--pgap-memory", help="PGAP container memory limit, e.g. 6g")
    parser.add_argument("--input-type", choices=["auto", "faa", "fna"], default="auto")
    parser.add_argument(
        "--annotation",
        choices=["auto", "pgap", "ncbi-import", "ncbi-hmm", "pyrodigal-ncbi-hmm"],
        default="auto",
    )
    parser.add_argument("--annotation-gbk", help="matching NCBI/PGAP GenBank annotation file")
    parser.add_argument(
        "--pgap-script", help="installed pgap.py path (requires Linux container runtime)"
    )
    parser.add_argument("--organism", help="organism name required by full PGAP")
    parser.add_argument("--kingdom", choices=["bacteria", "archaea"], default="bacteria")
    parser.add_argument(
        "--gram", choices=["positive", "negative", "unspecified"], default="unspecified"
    )
    parser.add_argument("--genetic-code", type=int, default=11)
    parser.add_argument(
        "--engine",
        choices=["native", "carveme", "reconstructor"],
        default="native",
        help="native v6 builder (default) or legacy compatibility adapter",
    )
    parser.add_argument("--hmm-dir", default="data/ncbi_hmm")
    parser.add_argument("--output-dir", help="new directory for reconstruction outputs")
    parser.add_argument(
        "--cpus", type=int, help="CPU limit (HMM default 4; PGAP deployment default 2)"
    )
    parser.add_argument("--quality", choices=["audit", "repair"], default="audit")
    parser.add_argument("--memote", metavar="SBML", help="score an existing model with MEMOTE")
    parser.add_argument("--memote-worker", help=argparse.SUPPRESS)
    parser.add_argument("--skip-memote", action="store_true", help="explicitly skip MEMOTE scoring")
    parser.add_argument("--memote-timeout", type=int, default=1800)
    parser.add_argument("--memote-solver-timeout", type=int, default=10)
    parser.add_argument("--reaction-library", help="prepared BiGG/ModelSEED library directory")
    parser.add_argument(
        "--reaction-library-mode",
        choices=("full", "strict"),
        help="reaction catalog mode (default: strict)",
    )
    parser.add_argument("--biomass-library", help="compiled public biomass catalog directory")
    parser.add_argument("--biomass-template", help="explicit biomass template ID")
    parser.add_argument(
        "--biomass-spec",
        help="input specification for explicitly compiling a biomass catalog",
    )
    parser.add_argument("--biomass-min-similarity", type=float)
    parser.add_argument("--gpr-min-identity", type=float)
    parser.add_argument("--gpr-min-coverage", type=float)
    parser.add_argument(
        "--clean-predictions",
        help="official CLEAN prediction CSV/TSV (headerless EC:.../score is supported)",
    )
    parser.add_argument(
        "--clean-runtime",
        help="CLEAN app directory; by default CLEAN runs on each input when discovered",
    )
    parser.add_argument("--clean-python", help="Python executable for the CLEAN runtime")
    parser.add_argument(
        "--clean-timeout", type=int, help="maximum seconds for one CLEAN prediction run"
    )
    parser.add_argument(
        "--clean-top-fraction",
        type=float,
        default=0.30,
        help="fraction of unique CLEAN candidate proteins to retain (default: 0.30)",
    )
    parser.add_argument("--medium-file", help="JSON exchange-to-uptake map")
    parser.add_argument("--prepare-reaction-library", metavar="DIRECTORY")
    parser.add_argument(
        "--bigg-models-directory",
        help="directory of public BiGG XML/JSON(.gz) models to merge into the library",
    )
    parser.add_argument(
        "--ec-alias-source",
        help="local CSV/TSV reaction-to-EC table (for example pear ec-code_annotation.csv)",
    )
    parser.add_argument(
        "--prepare-biomass-library",
        metavar="DIRECTORY",
        help="compile public biomass templates from --biomass-spec",
    )
    parser.add_argument(
        "--discover-public-biomass",
        metavar="JSON",
        help="record the current public BiGG model census",
    )
    parser.add_argument(
        "--download-public-biomass-models",
        action="store_true",
        help="download public BiGG model JSON.gz files while building the census",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["resume"],
        help="interactive command; use `resume` to choose a saved conversation",
    )
    parser.add_argument(
        "command_session",
        nargs="?",
        help="session ID for the `resume` command; omit it to choose interactively",
    )
    return parser


def require_optional_dependency(module: str, extra: str, feature: str) -> None:
    try:
        available = importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        available = False
    if not available:
        raise SystemExit(
            f"{feature} requires the optional dependency group; "
            f"install with: pip install 'gemagents[{extra}]'"
        )


def _configure_line_editor() -> None:
    """Enable terminal line editing when Python's readline extension exists."""
    if _readline is None:
        return
    try:
        _readline.parse_and_bind("set editing-mode emacs")
    except (AttributeError, RuntimeError):
        # Some embedded or non-interactive terminals expose readline partially.
        pass


def main(argv: list[str] | None = None) -> None:
    global SYSTEM_PROMPT, run_agent, run_agent_stream, load_config
    global load_project_context, load_agents, load_commands, load_output_styles
    global load_skills, render_command, FileHistory, HookBlocked, run_hooks
    global McpRegistry, PermissionMode, Session, SessionStore, RuntimeSettings
    global load_settings, RuntimeState, LocalToolRunner, ToolError, Console

    args = build_parser().parse_args(argv)

    if args.inspection_result and not args.approval_id:
        raise SystemExit("--inspection-result requires --approval-id")
    if (args.approval_id or args.inspection_result) and args.print_prompt is None:
        raise SystemExit("--approval-id and --inspection-result require --print")
    if args.execute_approved and not args.approval_id:
        raise SystemExit("--execute-approved requires --approval-id")
    if args.execute_approved and args.print_prompt is None:
        raise SystemExit("--execute-approved requires --print")

    if args.version:
        from GemAgents import __version__

        print(f"gemagents v{__version__}")
        return

    workspace = resolve_workspace(args.workspace)
    layout = RepoLayout(workspace)
    if args.command == "resume":
        from GemAgents.session import SessionStore

        selected = _select_resume_session(SessionStore(workspace), args.command_session)
        if selected is None:
            return
        args.resume = selected
    if args.memote or args.memote_worker:
        require_optional_dependency("cobra", "metabolic,memote", "MEMOTE")
        require_optional_dependency("memote", "metabolic,memote", "MEMOTE")
        from GemAgents.tools import metabolic_memote, metabolic_memote_worker

        if not args.output_dir:
            raise SystemExit("MEMOTE requires --output-dir")
        try:
            model_path = layout.resolve(args.memote or args.memote_worker)
            output = layout.writable(args.output_dir)
        except (PermissionError, ValueError) as error:
            raise SystemExit(f"MEMOTE input/output path rejected: {error}") from None
        try:
            result = (
                metabolic_memote_worker(model_path, output, args.memote_solver_timeout)
                if args.memote_worker
                else metabolic_memote(
                    model_path,
                    output,
                    {
                        "memote_timeout": args.memote_timeout,
                        "memote_solver_timeout": args.memote_solver_timeout,
                    },
                )
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["status"] not in {"completed", "completed_with_test_errors"}:
                raise SystemExit(1)
        except Exception as error:
            raise SystemExit(f"MEMOTE failed: {error}") from None
        return
    if args.prepare_reaction_library:
        require_optional_dependency("cobra", "metabolic", "Reaction library preparation")
        from GemAgents.tools import metabolic_prepare_library

        try:
            from GemAgents.contracts import default_ec_alias_source

            output = layout.writable(args.prepare_reaction_library)
            bigg_models = (
                layout.resolve(args.bigg_models_directory)
                if args.bigg_models_directory
                else None
            )
            ec_alias_source = (
                str(Path(args.ec_alias_source).expanduser().resolve())
                if args.ec_alias_source
                else default_ec_alias_source(workspace)
            )
        except (PermissionError, ValueError) as error:
            raise SystemExit(f"Reaction library input/output path rejected: {error}") from None
        result = metabolic_prepare_library(
            workspace,
            output,
            {
                "kingdom": args.kingdom,
                "gram": args.gram,
                **(
                    {"bigg_models_directory": str(bigg_models)}
                    if bigg_models is not None
                    else {}
                ),
                **(
                    {"ec_alias_source": ec_alias_source}
                    if ec_alias_source
                    else {}
                ),
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.prepare_biomass_library:
        require_optional_dependency("cobra", "metabolic", "Biomass library preparation")
        from GemAgents.tools import metabolic_prepare_biomass_library

        if not args.biomass_spec:
            raise SystemExit("--prepare-biomass-library requires --biomass-spec")
        try:
            from GemAgents.contracts import default_reaction_library_path

            library = layout.resolve(
                args.reaction_library or default_reaction_library_path(workspace)
            )
            spec = layout.resolve(args.biomass_spec)
            output = layout.writable(args.prepare_biomass_library)
        except (PermissionError, ValueError) as error:
            raise SystemExit(f"Biomass library input/output path rejected: {error}") from None
        result = metabolic_prepare_biomass_library(workspace, spec, output, library)
        print(
            json.dumps(
                {k: v for k, v in result.items() if k not in {"templates", "references"}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.discover_public_biomass:
        from GemAgents.tools import metabolic_discover_public_biomass_registry

        result = metabolic_discover_public_biomass_registry(
            layout.writable(args.discover_public_biomass),
            download_models=args.download_public_biomass_models,
        )
        print(
            json.dumps(
                {k: v for k, v in result.items() if k not in {"records"}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.setup_pgap or args.check_pgap:
        from GemAgents.errors import ToolError
        from GemAgents.tools import pgap_check, pgap_setup

        if args.setup_pgap and args.check_pgap:
            raise SystemExit("Use --setup-pgap or --check-pgap, not both")
        try:
            result = pgap_setup(workspace) if args.setup_pgap else pgap_check(workspace)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            ready = result.get("status") == "ready" if args.setup_pgap else result["ready"]
            if not ready:
                raise SystemExit(2)
        except (ToolError, OSError, subprocess.TimeoutExpired) as error:
            raise SystemExit(f"PGAP deployment failed: {error}") from None
        return
    if args.prepare_ncbi_hmms or args.reconstruct or args.reconstruct_config:
        require_optional_dependency("cobra", "metabolic", "Metabolic reconstruction")
        from GemAgents.errors import ToolError
        from GemAgents.metabolic.jobs.contracts import compile_config_file, compile_contract
        from GemAgents.tools import metabolic_pipeline, metabolic_prepare_hmms

        try:
            if args.prepare_ncbi_hmms:
                result = metabolic_prepare_hmms(layout.writable(args.prepare_ncbi_hmms))
            else:
                if args.reconstruct and args.reconstruct_config:
                    raise ToolError("Use --reconstruct or --reconstruct-config, not both")
                if args.reconstruct_config:
                    path = layout.resolve(args.reconstruct_config)
                    from GemAgents.contracts import load_configuration

                    config = load_configuration(path)
                else:
                    config = {
                        "input": args.reconstruct,
                        "input_type": args.input_type,
                        "annotation": args.annotation,
                        "allow_ambiguous_ec_gpr": True,
                        "kingdom": args.kingdom,
                        "gram": args.gram,
                        "genetic_code": args.genetic_code,
                        "engine": args.engine,
                        "hmm_dir": args.hmm_dir,
                        "quality": args.quality,
                        "memote": not args.skip_memote,
                        "memote_timeout": args.memote_timeout,
                        "memote_solver_timeout": args.memote_solver_timeout,
                    }
                    for key, value in (
                        ("annotation_gbk", args.annotation_gbk),
                        ("pgap_script", args.pgap_script),
                        ("pgap_config", args.pgap_config),
                        ("pgap_memory", args.pgap_memory),
                        ("reaction_library", args.reaction_library),
                        ("reaction_library_mode", args.reaction_library_mode),
                        ("biomass_library", args.biomass_library),
                        ("biomass_template", args.biomass_template),
                        ("biomass_min_similarity", args.biomass_min_similarity),
                        ("gpr_min_identity", args.gpr_min_identity),
                        ("gpr_min_coverage", args.gpr_min_coverage),
                        ("clean_predictions", args.clean_predictions),
                        ("clean_runtime", args.clean_runtime),
                        ("clean_python", args.clean_python),
                        ("clean_timeout", args.clean_timeout),
                        ("clean_top_fraction", args.clean_top_fraction),
                        ("medium_file", args.medium_file),
                        ("cpus", args.cpus),
                        ("organism", args.organism),
                        ("output", args.output_dir),
                    ):
                        if value is not None:
                            config[key] = value
                    from GemAgents.contracts import apply_reconstruction_defaults

                    config = apply_reconstruction_defaults(config, workspace)
                contract = (
                    compile_config_file(path, workspace)
                    if args.reconstruct_config
                    else compile_contract(config, workspace)
                )
                result = metabolic_pipeline(config, workspace, contract=contract)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as error:
            print(f"Reconstruction failed: {type(error).__name__}: {error}", file=sys.stderr)
            raise SystemExit(1) from None
        return
    from dotenv import load_dotenv
    from rich.console import Console

    from GemAgents.agent import SYSTEM_PROMPT, run_agent, run_agent_stream
    from GemAgents.config import load_config
    from GemAgents.context import load_project_context
    from GemAgents.extensions import (
        load_agents,
        load_commands,
        load_output_styles,
        load_skills,
        render_command,
    )
    from GemAgents.file_history import FileHistory
    from GemAgents.hooks import HookBlocked, run_hooks
    from GemAgents.mcp import McpRegistry
    from GemAgents.permissions import PermissionMode
    from GemAgents.session import Session, SessionStore
    from GemAgents.settings import RuntimeSettings, load_settings
    from GemAgents.state import RuntimeState
    from GemAgents.tools import LocalToolRunner, ToolError

    load_dotenv()
    settings = load_settings(
        workspace,
        Path(args.settings) if args.settings else None,
    )
    if args.dump_system_prompt:
        print(SYSTEM_PROMPT)
        return

    mode = _mode_from_args(args)
    config = _config_from_args(args, settings, workspace)
    runtime_state = RuntimeState.for_workspace(workspace)
    session_store = SessionStore(workspace)
    session = _load_session(args.resume, session_store)
    images = [Path(path).resolve() for path in args.image]
    project_context = load_project_context(workspace)
    _run_lifecycle_hooks(
        settings,
        "SessionStart",
        {"session_id": session.id, "workspace": str(workspace)},
        workspace,
        matcher="resume" if args.resume is not None else "startup",
    )

    if args.print_prompt is not None:
        prompt = args.print_prompt or sys.stdin.read().strip()
        inspection_result = _load_inspection_result(args.inspection_result, workspace)
        _run_headless(
            prompt,
            args.output_format,
            config,
            mode,
            settings,
            runtime_state,
            session,
            session_store,
            images,
            project_context,
            args.thread_id,
            approval_id=args.approval_id,
            inspection_result=inspection_result,
            execute_approved=args.execute_approved,
        )
        return

    _run_repl(
        config,
        mode,
        settings,
        runtime_state,
        session,
        session_store,
        project_context,
        show_history=args.resume is not None,
    )


def _config_from_args(args: argparse.Namespace, settings: RuntimeSettings, workspace: Path):
    model_name = args.model or settings.default_model
    if not model_name:
        raise SystemExit(
            "Set --model, GEMAGENTS_MODEL, OLLAMA_MODEL, or defaultModel in settings.json."
        )
    profile = settings.model_profile(model_name)
    return load_config(
        workspace=str(workspace),
        model=profile.model,
        protocol=profile.protocol,
        base_url=args.base_url or profile.base_url,
        api_key=args.api_key or profile.api_key,
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
        max_wall_seconds=args.max_wall_seconds,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens=args.max_output_tokens,
        max_total_tokens=args.max_total_tokens,
        max_cost_usd=args.max_cost_usd,
        max_memory_mb=args.max_memory_mb,
        shell_timeout=args.shell_timeout,
        tool_profile=args.tool_profile,
    )


def _load_session(resume_arg: str | None, store: SessionStore) -> Session:
    if resume_arg is None:
        return store.create()
    return store.load(resume_arg or None)


def _select_resume_session(store: SessionStore, requested: str | None = None) -> str | None:
    """Resolve ``gemagents resume [SESSION_ID]`` to a saved session ID."""
    if requested:
        try:
            store.load(requested)
        except (FileNotFoundError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise SystemExit(f"saved session is unavailable: {requested}") from error
        return requested

    candidates: list[tuple[str, Session, float]] = []
    for path in store.root.glob("*.json"):
        try:
            session = store.load(path.stem)
            modified = path.stat().st_mtime
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        candidates.append((path.stem, session, modified))
    candidates.sort(key=lambda item: item[2], reverse=True)
    if not candidates:
        print("No saved sessions found in this workspace.")
        return None

    print("Saved sessions (newest first):")
    for index, (session_id, session, _modified) in enumerate(candidates, start=1):
        last_message = next(
            (
                str(item.get("content", "")).replace("\n", " ").strip()
                for item in reversed(session.messages)
                if item.get("role") == "user" and str(item.get("content", "")).strip()
            ),
            "(no user message)",
        )
        preview = last_message[:72] + ("…" if len(last_message) > 72 else "")
        print(
            f"  {index}. {session_id} | {len(session.messages)} messages | {preview}"
        )
    try:
        answer = input("Choose a session number or ID [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if not answer:
        return candidates[0][0]
    if answer in {item[0] for item in candidates}:
        return answer
    try:
        index = int(answer)
    except ValueError as error:
        raise SystemExit("session selection must be a number or saved session ID") from error
    if not 1 <= index <= len(candidates):
        raise SystemExit(f"session selection must be between 1 and {len(candidates)}")
    return candidates[index - 1][0]


def _load_inspection_result(
    value: str | None, workspace: Path
) -> dict[str, object] | None:
    """Load explicit inspection evidence without accepting paths outside workspace."""
    if value is None:
        return None
    try:
        path = RepoLayout(workspace).resolve(value)
    except ValueError as error:
        raise SystemExit(f"inspection result path rejected: {error}") from None
    if path.is_symlink() or not path.is_file():
        raise SystemExit("inspection result must be a regular workspace file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"inspection result is not valid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise SystemExit("inspection result must contain a JSON object")
    return payload


def _run_headless(
    prompt: str,
    output_format: str,
    config,
    mode: PermissionMode,
    settings: RuntimeSettings,
    runtime_state: RuntimeState,
    session: Session,
    session_store: SessionStore,
    images: list[Path],
    project_context: str,
    thread_id: str | None,
    *,
    approval_id: str | None = None,
    inspection_result: dict[str, object] | None = None,
    execute_approved: bool = False,
) -> None:
    styles = load_output_styles(config.workspace)
    prompt = _apply_output_style(prompt, settings.output_style, styles)
    _run_lifecycle_hooks(
        settings,
        "UserPromptSubmit",
        {"session_id": session.id, "prompt": prompt},
        config.workspace,
    )
    weather_answer = _maybe_answer_weather_prompt(prompt, config, mode, settings, runtime_state)
    if weather_answer is not None:
        _append_session(session, prompt, weather_answer)
        session_store.save(session)
        _run_lifecycle_hooks(
            settings,
            "Stop",
            {"session_id": session.id, "last_assistant_message": weather_answer},
            config.workspace,
        )
        if output_format == "json":
            print(json.dumps({"text": weather_answer, "steps": 1, "session_id": session.id}))
        elif output_format == "stream-json":
            print(json.dumps({"type": "assistant", "text": weather_answer}, ensure_ascii=False))
            print(json.dumps({"type": "result", "steps": 1}, ensure_ascii=False))
        else:
            print(weather_answer)
        return

    if output_format == "stream-json":
        from GemAgents.checkpoint import EventStore

        durable_thread_id = thread_id or session.id
        layout = RepoLayout(config.workspace)
        events = run_agent_stream(
            prompt,
            config,
            mode=mode,
            runtime_state=runtime_state,
            hooks=settings.hooks,
            images=images,
            prior_messages=_messages_from_session(session),
            approval_id=approval_id,
            inspection_result=inspection_result,
            execute_approved=execute_approved,
            project_context=project_context,
            command_env=settings.env,
            additional_directories=settings.additional_directories,
            respect_gitignore=settings.respect_gitignore,
            on_event=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
            thread_id=durable_thread_id,
            event_store=EventStore(layout.runtime / "checkpoints.sqlite3"),
            event_log_path=layout.writable(".gemagents/events.jsonl"),
        )
        streamed_text = str(events[-1].get("text", "")) if events else ""
        streamed_turn = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": streamed_text},
        ]
        if session.messages[-2:] != streamed_turn:
            _append_session(session, prompt, streamed_text)
        session_store.save(session)
        _run_lifecycle_hooks(
            settings,
            "Stop",
            {"session_id": session.id, "last_assistant_message": streamed_text},
            config.workspace,
        )
        return

    result = run_agent(
        prompt,
        config,
        mode=mode,
        runtime_state=runtime_state,
        hooks=settings.hooks,
        images=images,
        prior_messages=_messages_from_session(session),
        approval_id=approval_id,
        inspection_result=inspection_result,
        execute_approved=execute_approved,
        command_env=settings.env,
        additional_directories=settings.additional_directories,
        respect_gitignore=settings.respect_gitignore,
        project_context=project_context,
    )
    _append_session(session, prompt, result.text)
    session_store.save(session)
    _run_lifecycle_hooks(
        settings,
        "Stop",
        {"session_id": session.id, "last_assistant_message": result.text},
        config.workspace,
    )

    if output_format == "json":
        print(json.dumps({"text": result.text, "steps": result.steps, "session_id": session.id}))
    else:
        print(result.text)


def _run_repl(
    config,
    mode: PermissionMode,
    settings: RuntimeSettings,
    runtime_state: RuntimeState,
    session: Session,
    session_store: SessionStore,
    project_context: str,
    *,
    show_history: bool = False,
) -> None:
    _configure_line_editor()
    console = Console()
    skills = load_skills(config.workspace)
    commands = load_commands(config.workspace)
    agents = load_agents(config.workspace)
    styles = load_output_styles(config.workspace)
    current_mode = {"value": mode}
    console.print("[bold]GemAgents[/bold]. Type /help or /exit.")
    if show_history:
        _print_session_history(session, console)

    while True:
        try:
            prompt = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not prompt:
            continue
        if prompt in {"/exit", "/quit", "/bye"}:
            session_store.save(session)
            _run_lifecycle_hooks(
                settings,
                "Stop",
                {"session_id": session.id, "last_assistant_message": ""},
                config.workspace,
            )
            return
        handled = _handle_builtin(
            prompt,
            session,
            session_store,
            skills,
            commands,
            agents,
            styles,
            config,
            settings,
            runtime_state,
            current_mode,
        )
        if handled is not None:
            console.print(handled)
            continue

        prompt = _expand_slash_prompt(prompt, skills, commands)
        prompt = _apply_output_style(prompt, settings.output_style, styles)
        _run_lifecycle_hooks(
            settings,
            "UserPromptSubmit",
            {"session_id": session.id, "prompt": prompt},
            config.workspace,
        )
        weather_answer = _maybe_answer_weather_prompt(
            prompt,
            config,
            current_mode["value"],
            settings,
            runtime_state,
        )
        if weather_answer is not None:
            _append_session(session, prompt, weather_answer)
            session_store.save(session)
            _run_lifecycle_hooks(
                settings,
                "Stop",
                {"session_id": session.id, "last_assistant_message": weather_answer},
                config.workspace,
            )
            console.print(weather_answer)
            continue

        events = run_agent_stream(
            prompt,
            config,
            mode=current_mode["value"],
            ask_permission=_ask_permission,
            runtime_state=runtime_state,
            hooks=settings.hooks,
            prior_messages=_messages_from_session(session),
            command_env=settings.env,
            additional_directories=settings.additional_directories,
            respect_gitignore=settings.respect_gitignore,
            project_context=project_context,
            on_event=lambda event: _render_repl_event(console, event),
        )
        answer = str(events[-1].get("text", "")) if events else ""
        _append_session(session, prompt, answer)
        session_store.save(session)
        _run_lifecycle_hooks(
            settings,
            "Stop",
            {"session_id": session.id, "last_assistant_message": answer},
            config.workspace,
        )
        console.print(answer)


def _print_session_history(session: Session, console) -> None:
    """Show persisted user/assistant turns when entering a resumed REPL."""
    if not session.messages:
        console.print("[dim]已恢复会话，但没有已保存的对话消息。[/dim]")
        return
    console.print(
        f"[dim]已恢复会话 {session.id}，历史消息 {len(session.messages)} 条：[/dim]"
    )
    for item in session.messages:
        role = "你" if item.get("role") == "user" else "GemAgents"
        content = str(item.get("content", ""))
        console.print(f"{role}: {content}", markup=False)


def _render_repl_event(console, event: dict[str, object]) -> None:
    """Render auditable execution progress without exposing hidden chain-of-thought."""
    event_name = event.get("event")
    if event_name == "execution_trace":
        phase = str(event.get("phase", ""))
        action = str(event.get("action", ""))
        if phase == "accept":
            input_data = event.get("input", {})
            console.print(
                f"执行轨迹｜已接收请求：{_trace_display(input_data)}",
                markup=False,
            )
        elif phase == "route":
            observation = _trace_display(event.get("observation", {}))
            console.print(
                f"执行轨迹｜路由：{action}；观察：{observation}",
                markup=False,
            )
        elif phase == "plan":
            next_action = _trace_next_action(event.get("next_action", ""))
            console.print(
                f"执行轨迹｜下一步：{_trace_display(action)}；动作：{next_action}",
                markup=False,
            )
            if event.get("summary"):
                console.print(f"  操作说明：{event['summary']}", markup=False)
            elif event.get("based_on") is not None:
                console.print(
                    f"  依据本轮工具观察：{_trace_display(event['based_on'])}", markup=False
                )
        elif phase == "execute":
            console.print(
                f"执行轨迹｜执行：{action}；输入：{_trace_display(event.get('input', {}))}",
                markup=False,
            )
        elif phase == "observe":
            observation = _trace_display(event.get("observation", {}))
            next_action = _trace_next_action(event.get("next_action", ""))
            console.print(
                f"执行轨迹｜观察：{action} 返回 {observation}；下一步：{next_action}",
                markup=False,
            )
    elif event_name == "model_started":
        console.print(f"[dim]模型处理中（步骤 {event.get('step', 0)}）…[/dim]")
    elif event_name == "tool_started" and "input" not in event:
        console.print(f"[dim]执行工具：{event.get('name', 'unknown')}…[/dim]")
    elif event_name == "tool_finished" and "observation" not in event:
        console.print(f"[dim]工具完成：{event.get('name', 'unknown')}[/dim]")
    elif event_name == "job_progress":
        console.print(
            f"[dim]执行进度：已完成 {event.get('completed_tool', 'tool')}[/dim]"
        )
    elif event_name == "reconstruction_progress":
        if event.get("monitoring_interrupted"):
            console.print(
                "已停止当前终端监控，未取消重建任务；"
                f"最后观察到状态={event.get('last_observed_status', 'unknown')}，"
                f"作业={event.get('job_id', 'unknown')}",
                markup=False,
            )
        for line in event.get("log_lines", []):
            console.print(f"  {line}", markup=False)
    elif event_name == "batch_progress":
        job_id = str(event.get("job_id", ""))
        status = str(event.get("status", "unknown"))
        if event.get("monitoring_interrupted"):
            message = (
                "[yellow]已停止当前终端监控；批处理仍在后台运行"
                f"（作业 {job_id or 'unknown'}）。[/yellow]"
            )
            console.print(message)
            return
        completed = event.get("completed_builds")
        total = event.get("total_builds")
        progress = (
            f"，已完成 {completed}/{total} 个菌株"
            if completed is not None and total is not None
            else f"，已完成 {completed} 个菌株"
            if completed is not None
            else ""
        )
        console.print(
            f"[dim]批处理监控：{job_id[:12] or 'unknown'} 状态={status}{progress}[/dim]"
        )
        log_lines = event.get("log_lines")
        if isinstance(log_lines, list):
            for line in log_lines:
                console.print(f"  {line}", markup=False)
    elif event_name == "approval_required":
        console.print(f"[dim]等待授权：{event.get('tool', 'tool')}[/dim]")
    elif event_name == "inspection_completed":
        console.print(f"[dim]检查完成：{event.get('status', 'unknown')}[/dim]")
    elif event_name == "request_blocked":
        console.print(f"[yellow]请求未执行：{event.get('reason', 'blocked')}[/yellow]")
    elif event_name == "run_failed":
        console.print(f"[red]运行失败：{event.get('error_type', 'unknown')}[/red]")


def _trace_display(value: object) -> str:
    """Render bounded structured trace values without Rich markup interpretation."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _trace_next_action(value: object) -> str:
    return {
        "execute requested tools": "执行所选工具",
        "return final response": "汇总本轮结果",
        "continue with authorized tool": "执行已授权工具",
        "return deterministic validation result": "返回确定性校验结果",
        "model decides next step": "将本轮观察交给模型选择后续操作",
        "continue monitoring worker": "worker 尚未结束，运行时等待后继续观察（不调用 LLM）",
        "report terminal batch result": "批处理已结束，汇总结果与失败证据",
        "report reconstruction result or blocking evidence": "汇总重建结果或阻塞证据",
        "stop foreground monitoring; worker is not cancelled": "停止前台监控，未取消 worker",
    }.get(str(value), str(value))


def _handle_builtin(
    prompt: str,
    session: Session,
    session_store: SessionStore,
    skills: dict[str, Extension],
    commands: dict[str, Extension],
    agents: dict[str, Extension],
    styles: dict[str, Extension],
    config,
    settings: RuntimeSettings,
    runtime_state: RuntimeState,
    current_mode: dict[str, PermissionMode],
) -> str | None:
    _ensure_runtime_imports()
    if prompt == "/help":
        return (
            "/help /clear /compact /history /skills /agents /commands /output-style "
            "/mcp /config /mode /tasks /memory /session-export /rewind "
            "/model /hooks /permissions /diff /context /file-history /diagnostics /exit "
            "plus skill and command slash prompts"
        )
    if prompt == "/clear":
        session.messages.clear()
        return "conversation cleared"
    if prompt == "/compact":
        if len(session.messages) > 40:
            session.messages = session.messages[-40:]
        return f"context compacted to {len(session.messages)} message(s)"
    if prompt == "/history":
        return "\n".join(session_store.list())
    if prompt.startswith("/model"):
        parts = prompt.split()
        if len(parts) == 2 and parts[1] == "list":
            names = settings.model_names
            return "\n".join(names) if names else config.model
        return f"model={config.model}\nprotocol={config.protocol}"
    if prompt == "/mode" or prompt.startswith("/mode "):
        parts = prompt.split()
        if len(parts) == 2:
            current_mode["value"] = PermissionMode(parts[1])
            return f"mode={current_mode['value'].value}"
        return f"mode={current_mode['value'].value}"
    if prompt.startswith("/tasks"):
        parts = prompt.split()
        if len(parts) == 2 and parts[1] == "reset":
            runtime_state.clear_tasks()
            runtime_state.todos.clear()
            return "tasks reset"
        return runtime_state.list_tasks()
    if prompt == "/memory":
        return "\n".join(runtime_state.memories)
    if prompt.startswith("/session-export"):
        parts = prompt.split(maxsplit=1)
        target = Path(parts[1]) if len(parts) == 2 else config.workspace / f"{session.id}.json"
        try:
            target = RepoLayout(config.workspace).writable(target)
        except (PermissionError, ValueError) as error:
            return f"session export rejected: {error}"
        target.write_text(
            json.dumps(session.messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return f"exported session to {target}"
    if prompt.startswith("/rewind"):
        parts = prompt.split()
        count = int(parts[1]) if len(parts) == 2 else 1
        del session.messages[-2 * count :]
        return f"rewound {count} turn(s)"
    if prompt == "/diagnostics":
        return (
            f"workspace={config.workspace}\n"
            f"model={config.model}\n"
            f"protocol={config.protocol}\n"
            f"mode={current_mode['value'].value}\n"
            f"session={session.id}\n"
            f"messages={len(session.messages)}\n"
            f"respectGitignore={settings.respect_gitignore}\n"
            f"additionalDirectories={len(settings.additional_directories)}"
        )
    if prompt == "/hooks":
        if not settings.hooks:
            return ""
        return "\n".join(sorted(settings.hooks))
    if prompt == "/permissions":
        return (
            f"mode={current_mode['value'].value}\n"
            "read_only=read_file,list_files,grep,web_fetch,web_search,"
            "weather,task_list,task_get,list_mcp_resources,read_mcp_resource,"
            "metabolic_batch_status\n"
            "mutating=write_file,edit_file,multi_edit,run_shell,run_powershell,"
            "todo_write,task_create,task_update,memory_write,restore_file,"
            "agent,team_create,team_delete,send_message,metabolic_batch_start"
        )
    if prompt.startswith("/diff"):
        parts = prompt.split(maxsplit=1)
        target = parts[1] if len(parts) == 2 else "--"
        return _git_diff(config.workspace, target)
    if prompt.startswith("/file-history"):
        return _handle_file_history_command(prompt, config.workspace)
    if prompt == "/context":
        return f"messages={len(session.messages)}\ncompact_after=40"
    if prompt == "/skills":
        return "\n".join(sorted(skills))
    if prompt == "/agents":
        return "\n".join(sorted(agents))
    if prompt == "/commands":
        return "\n".join(sorted(commands))
    if prompt.startswith("/output-style"):
        return "\n".join(sorted(styles))
    if prompt.startswith("/mcp"):
        return McpRegistry(config.workspace).list_resources()
    if prompt.startswith("/config"):
        return _handle_config_command(prompt, config, settings)
    return None


def _maybe_answer_weather_prompt(
    prompt: str,
    config,
    mode: PermissionMode,
    settings: RuntimeSettings,
    runtime_state: RuntimeState,
) -> str | None:
    _ensure_runtime_imports()
    location = _extract_weather_location(prompt)
    if location is None:
        return None

    runner = LocalToolRunner(
        config.workspace,
        mode=mode,
        runtime_state=runtime_state,
        hooks=settings.hooks,
        command_env=settings.env,
        additional_directories=settings.additional_directories,
        respect_gitignore=settings.respect_gitignore,
    )
    try:
        return runner.run("weather", {"location": location})
    except ToolError as error:
        return f"weather lookup failed: {error}"


def _extract_weather_location(prompt: str) -> str | None:
    text = prompt.strip()
    if "天气" in text and any(marker in text for marker in ("今天", "现在", "怎么样", "预报")):
        before_weather = text.split("天气", maxsplit=1)[0]
        for token in ("今天", "现在", "当前", "请问", "帮我查", "查一下", "搜索", "联网"):
            before_weather = before_weather.replace(token, "")
        location = before_weather.strip(" ，。！？?：:,.!\"'")
        return location or None

    lowered = text.lower()
    if "weather in " in lowered:
        location = text[lowered.index("weather in ") + len("weather in ") :].strip(
            " ，。！？?：:,.!\"'"
        )
        return location or None

    return None


def _handle_config_command(prompt: str, config, settings: RuntimeSettings) -> str:
    parts = prompt.split(maxsplit=3)
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "list"):
        return json.dumps(settings.raw, ensure_ascii=False, indent=2, sort_keys=True)
    if len(parts) == 3 and parts[1] == "get":
        return json.dumps(settings.get_path(parts[2]), ensure_ascii=False)
    if len(parts) == 4 and parts[1] == "set":
        settings.set_path(parts[2], _parse_config_value(parts[3]))
        return f"set {parts[2]}"
    return f"workspace={config.workspace}\nmodel={config.model}\nprotocol={config.protocol}"


def _parse_config_value(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _git_diff(workspace: Path, target: str) -> str:
    args = ["git", "diff"]
    if target != "--":
        args.extend(["--", target])
    completed = subprocess.run(
        args,
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout or completed.stderr


def _handle_file_history_command(prompt: str, workspace: Path) -> str:
    _ensure_runtime_imports()
    history = FileHistory(workspace)
    parts = prompt.split()
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "list"):
        return history.render_list()
    if len(parts) == 3 and parts[1] == "restore":
        return history.restore(parts[2])
    return "usage: /file-history [list|restore <snapshot-id>]"


def _run_lifecycle_hooks(
    settings: RuntimeSettings,
    event: str,
    payload: dict[str, object],
    workspace: Path,
    *,
    matcher: str = "*",
) -> None:
    _ensure_runtime_imports()
    try:
        run_hooks(settings.hooks, event, payload, workspace=workspace, matcher=matcher)
    except HookBlocked as error:
        raise SystemExit(str(error)) from error


def _expand_slash_prompt(
    prompt: str,
    skills: dict[str, Extension],
    commands: dict[str, Extension],
) -> str:
    if not prompt.startswith("/"):
        return prompt
    name, _, arguments = prompt[1:].partition(" ")
    if name in skills:
        return render_command(skills[name], arguments)
    if name in commands:
        return render_command(commands[name], arguments)
    return prompt


def _apply_output_style(
    prompt: str,
    style_name: str | None,
    styles: dict[str, Extension],
) -> str:
    if not style_name or style_name not in styles or not styles[style_name].body.strip():
        return prompt
    return f"{styles[style_name].body.strip()}\n\n{prompt}"


def _messages_from_session(session: Session) -> list[BaseMessage]:
    from langchain_core.messages import AIMessage, HumanMessage

    messages: list[BaseMessage] = []
    for item in session.messages:
        if item.get("role") == "user":
            messages.append(HumanMessage(content=str(item.get("content", ""))))
        elif item.get("role") == "assistant":
            messages.append(AIMessage(content=str(item.get("content", ""))))
    return messages


def _append_session(session: Session, user_prompt: str, assistant_text: str) -> None:
    session.messages.append({"role": "user", "content": user_prompt})
    session.messages.append({"role": "assistant", "content": assistant_text})


def _mode_from_args(args: argparse.Namespace) -> PermissionMode:
    _ensure_runtime_imports()
    if args.plan:
        return PermissionMode.PLAN
    if args.auto or args.permission_mode in {"default", "auto"}:
        return PermissionMode.AUTO
    return PermissionMode(args.permission_mode)


def _ask_permission(tool_name: str, tool_input: dict[str, object]) -> bool:
    print(f"\nTool request: {tool_name}({tool_input})")
    answer = input("Allow? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
