# GemAgents

GemAgents is a pure Python terminal coding agent built on LangGraph, with
deterministic metabolic reconstruction workflows. The distribution and primary
CLI are `gemagents`, while the import package is `GemAgents`.

FAA/FNA metabolic reconstruction now has a deterministic CLI and background agent
tools, with NCBI annotation routes, CLEAN enzyme predictions, public
CarveMe/Reconstructor adapters, and independent CER quality checks. See the
[Chinese usage guide](docs/genome-reconstruction.zh-CN.md)
for installation, DeepSeek configuration, verified runs, and PGAP runtime requirements.
For the deployed Windows/WSL PGAP runtime, see
[PGAP deployment and usage](docs/pgap-deployment.zh-CN.md).

The default native track uses the complete v6 reaction catalog. When the bundled
public reference assets and adjacent CLEAN output are available, it also enables
CLEAN evidence and the reference-assisted scaffold; each can be disabled or
replaced in the reconstruction config.

## CLEAN-assisted reconstruction

The native reconstruction pipeline writes a FASTA/TSV containing only proteins
without NCBI EC evidence. Official CLEAN output can then be supplied through
`clean_predictions`; the pipeline deduplicates by protein, keeps the top 30% by
default, maps EC numbers to the reaction library, and records CLEAN provenance in
the SBML model.

Gap-filling uses the configured `medium`: it defaults to aerobic glucose-minimal,
accepts an explicit exchange-to-uptake map, and uses the all-exchange `rich`
preset only when `"medium": "rich"` is requested.
When the selected public reference template still grows after molecular-oxygen
uptake is removed, the native workflow adds a reference-supported anaerobic
growth task and preserves both conditions during gap-filling and energy-cycle
repair. Oxygen ports are recognized from identifiers, public annotations, and
formulae rather than an E. coli-specific reaction name; obligate aerobes are not
forced to acquire an unsupported phenotype.

```json
{
  "engine": "native",
  "clean_predictions": "runs/clean_maxsep.csv",
  "clean_top_fraction": 0.30
}
```

See [CLEAN integration](docs/clean-integration.zh-CN.md) for the official
headerless output format and deployment notes.

## Architecture

```text
Rich CLI / headless mode
        |
Session + settings + commands + skills
        |
LangGraph StateGraph
        |
LLM decision <-> local tools <-> permission policy
```

Core modules:

- `src/GemAgents/agent.py` builds the LangGraph tool loop.
- `src/GemAgents/tools.py` implements file, shell, web, MCP, task, memory, and team tools.
- `src/GemAgents/cli.py` provides REPL, headless, session, slash-command, and output modes.
- `src/GemAgents/settings.py` loads user/project settings and model profiles.
- `src/GemAgents/extensions.py` loads skills, user commands, agents, and output styles.

## GemAgents Features

- CLI and headless print mode: `gemagents`, `-p`, `--output-format text/json/stream-json`
- LangGraph agentic loop with local tool execution
- Rich terminal REPL
- Session persistence and `--resume`
- Context compaction via `/compact`
- Project/user settings with model profiles, `env`, `additionalDirectories`, and `respectGitignore`
- Skills, user commands, output styles, and agent definitions from `.gemagents/`
- Hooks from settings on tool lifecycle events
- Lifecycle hooks: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`
- Project context from `AGENT.md` / `CLAUDE.md` and persistent `.gemagents/memory.md`
- Permission modes: `default`/`auto` (CLI execution), and `plan`
- File history snapshots and restore
- Todo/task/memory tools, with tasks stored in `.gemagents/tasks.json`
- Web fetch/search tools
- MultiEdit
- MCP resource registry from `.mcp.json`
- PowerShell tool when `pwsh` or `powershell` is installed
- Sub-agent/team mailbox tool surface backed by persistent task and memory records
- Multimodal image attachments with `--image`
- Built-in commands: `/help`, `/clear`, `/compact`, `/history`, `/skills`, `/agents`, `/commands`, `/output-style`, `/mcp`, `/config list|get|set`, `/mode`, `/model`, `/hooks`, `/permissions`, `/diff`, `/context`, `/file-history`, `/tasks`, `/memory`, `/session-export`, `/rewind`, `/diagnostics`

## Environment

Create or update the project environment:

```bash
conda env create -f environment.yml
conda activate gemagents
```

If it already exists:

```bash
conda env update -n gemagents -f environment.yml --prune
conda activate gemagents
```

For pip installs, use `pip install .` for the core agent or
`pip install '.[metabolic,memote,benchmark,status]'` for the complete local
workflow. See [the tested environment matrix](docs/environment-matrix.md) for
locked installation and migration commands.

## Model Configuration

Set a model directly:

```bash
export GEMAGENTS_MODEL="gpt-4o-mini"
export OPENAI_API_KEY="..."
```

For a local Ollama model, GemAgents also accepts Ollama-native defaults:

```bash
export OLLAMA_MODEL="qwen3.5:35b"
export OLLAMA_BASE_URL="http://localhost:11434"
```

Or use `.gemagents/settings.json`:

```json
{
  "defaultModel": "gpt",
  "env": {
    "EXAMPLE_FLAG": "enabled"
  },
  "additionalDirectories": ["/tmp/shared-workspace"],
  "respectGitignore": true,
  "models": {
    "gpt": {
      "protocol": "openai-chat",
      "model": "gpt-4o-mini",
      "baseURL": "https://api.openai.com/v1",
      "apiKey": "${OPENAI_API_KEY}"
    },
    "claude": {
      "protocol": "anthropic",
      "model": "claude-sonnet-4-5",
      "apiKey": "${ANTHROPIC_AUTH_TOKEN}"
    },
    "gemini": {
      "protocol": "gemini",
      "model": "gemini-2.5-pro",
      "apiKey": "${GEMINI_API_KEY}"
    }
  }
}
```

## Usage

One-shot:

```bash
gemagents --model gpt -p "Inspect this project."
gemagents --model gpt -p "Summarize this screenshot" --image ./screen.png
gemagents --model gpt -p "Return JSON" --output-format json
```

Interactive:

```bash
gemagents --model gpt
gemagents --model gpt --resume
gemagents resume
gemagents resume SESSION_ID
gemagent resume
gemagents --model gpt --plan
gemagents --model gpt --permission-mode default
```

The bare interactive command and `--permission-mode default` run requested tools
automatically. Use `--plan` when you want to deny mutations.

## Extension Files

Project-local extensions live under `.gemagents/`:

```text
.gemagents/
├── settings.json
├── skills/<name>.md
├── commands/<name>.md
├── agents/<name>.md
└── output-styles/<name>.md
```

Markdown files support YAML frontmatter. Command and skill bodies support
`$ARGUMENTS`, `$1`, `$2`, and so on.

## Verification

```bash
pytest
ruff check .
python -m compileall -q src tests
```

See `docs/review-and-migration.md` for the migration rationale and feature
mapping.

## Release and benchmark status

The current engineering release is documented in docs/releases/REPRODUCE.md.
Run scripts/reproduce_release.py to generate the hash-bound capability manifest.
The locked phenotype dataset is insufficient for biological accuracy claims;
reference-assisted runs remain explicitly non-independent.
