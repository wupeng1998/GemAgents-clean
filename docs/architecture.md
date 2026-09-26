# Architecture Review

## Current-state review

GemAgents is a pure Python terminal agent with a dedicated `gemagents` conda
environment. The runtime is organized around a LangGraph execution loop,
Python-native tools, persistent sessions, and deterministic metabolic workflows.

## First-principles redesign

A coding agent needs a small loop more than it needs a large UI stack:

1. accept a user objective
2. ask an LLM what to do next
3. execute a local tool when workspace evidence or edits are required
4. feed the observation back to the model
5. stop with a final answer

GemAgents uses LangGraph for that loop:

```text
START -> model -> tools -> model -> END
```

The local tool surface is intentionally small: read, write, exact edit, list,
grep, and shell. Permission modes are also small: `default`, `plan`, and `auto`.

## Preserved feature surface

The current feature surface is implemented by these Python-native modules:

| Capability | Implementation |
| --- | --- |
| CLI / headless print mode | `GemAgents.cli`, `gemagents`, `-p`, `--output-format text/json/stream-json` |
| Streaming communication | event stream output for headless execution |
| Tool execution | LangGraph tool loop in `GemAgents.agent` |
| Terminal UI | Rich-backed REPL |
| Session orchestration | JSON session store under `.gemagents/sessions` |
| Context compaction | prior-message compaction before LangGraph invocation |
| MCP | `.mcp.json` registry and resource-reading tools |
| Skills | Markdown/frontmatter loader under `.gemagents/skills` |
| Sandbox / permissions | workspace path checks, plan/default/auto modes, shell danger checks |
| Sub-agents / teams | agent and team task/mailbox tool surface backed by `.gemagents/tasks.json` and `.gemagents/memory.md` |
| Hooks | settings-driven lifecycle shell hooks for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop` |
| Output styles | Markdown output-style loader |
| User commands | `.gemagents/commands/*.md` slash prompt expansion |
| Unified configuration | user/project/settings-file merge with model profiles, env injection, extra directories, gitignore control |
| File history | persistent snapshots before write/edit/multi-edit, `/file-history list`, and `/file-history restore <id>` |
| Project context / memory | `AGENT.md` / `CLAUDE.md` injection plus persistent `.gemagents/memory.md` |
| Background task records | `task_create`, `task_update`, `task_list`, `task_get`, and `/tasks reset` persist through `.gemagents/tasks.json` |
| Resilience | explicit tool errors and bounded max graph steps |
| Multi-provider support | OpenAI-compatible, Anthropic, Gemini profile dispatch |
| Web tools | `web_fetch` and `web_search` tools |
| MultiEdit | exact multi-replacement tool |
| PowerShell | `run_powershell` when `pwsh`/`powershell` is installed |
| Multimodal image input | `--image` data URL message blocks |
| Built-in commands | `/help`, `/clear`, `/compact`, `/history`, `/skills`, `/agents`, `/commands`, `/output-style`, `/mcp`, `/config list/get/set`, `/mode`, `/model`, `/hooks`, `/permissions`, `/diff`, `/context`, `/file-history`, `/tasks`, `/memory`, `/session-export`, `/rewind`, `/diagnostics` |

## Metabolic analysis orchestration

Natural-language metabolic requests use the same model-to-tool loop as coding
requests. The LLM selects a typed tool, the local runner validates paths and
parameters, and the deterministic analysis layer records the source hash and
returns structured evidence. Current analysis tools include:

| Request family | Tool surface |
| --- | --- |
| Model summary and component lookup | `model_inspect`, `inspect_model_component` |
| FBA, pFBA, FVA, and condition comparison | `simulate_fba`, `simulate_pfba`, `simulate_fva`, `compare_scenarios` |
| Gene and reaction perturbations | `simulate_knockouts`, `scan_essentiality` |
| Resource interpretation | `analyze_shadow_prices` |
| Pathway, community, FSEOF, and strain design | `analyze_request` |
| Reconstruction and batch jobs | `metabolic_start`, `metabolic_batch_start`, status and recovery tools |

Analysis operations load a copy of the model and never edit the source model.
Reports preserve solver status, source hashes, assumptions, and limitations so
the LLM can explain results without turning a model prediction into an
experimental claim.

