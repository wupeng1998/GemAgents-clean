# Review and Migration Notes

## Current-state review

The original repository was a TypeScript/Node.js terminal agent. Its runtime
entrypoint was `package.json -> tsx src/entrypoint/cli.ts`, with React Ink UI
components, TypeScript services, JavaScript step snapshots, npm scripts, and a
Node dependency lockfile.

That architecture contradicted the requested end state: a pure Python project
with a dedicated conda environment named `gemagents`.

## First-principles redesign

A coding agent needs a small loop more than it needs a large UI stack:

1. accept a user objective
2. ask an LLM what to do next
3. execute a local tool when workspace evidence or edits are required
4. feed the observation back to the model
5. stop with a final answer

The Python rewrite uses LangGraph for that loop:

```text
START -> model -> tools -> model -> END
```

The local tool surface is intentionally small: read, write, exact edit, list,
grep, and shell. Permission modes are also small: `default`, `plan`, and `auto`.

## Preserved feature surface

The Python implementation keeps the project functionality as Python-native modules:

| Original area | Python replacement |
| --- | --- |
| CLI / headless print mode | `GemAgents.cli`, `gemagents`, `-p`, `--output-format text/json/stream-json` |
| Streaming communication | event stream output for headless execution |
| Tool execution | LangGraph tool loop in `GemAgents.agent` |
| Terminal UI | Rich-backed REPL instead of React Ink |
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

No npm compatibility shim was introduced. Keeping two runtimes would violate the
pure Python requirement and immediately increase maintenance cost.
