# GemAgents

GemAgents 是基于 LangGraph 的纯 Python 终端 Coding Agent，包含确定性的代谢网络重建流程。发布包和主命令是 `gemagents`，Python 导入路径是 `GemAgents`。

现已接入 FAA/FNA → NCBI 注释证据 → CLEAN 酶预测 → 反应库映射 → CarveMe / Reconstructor 适配 → 独立 CER 质控 → SBML 的流程。默认 native 重建使用完整 v6 反应库，并在本地参考资产可用时启用 CLEAN 与 reference scaffold。
运行方法、DeepSeek 配置和本机实测限制见 [基因组重建使用说明](docs/genome-reconstruction.zh-CN.md)。

## CLEAN 辅助重建

native 流程会自动生成只包含“没有 NCBI EC 证据”蛋白的
`clean-input-no-ncbi.faa` 和 `clean-input-no-ncbi.tsv`。将官方 CLEAN 结果通过
`clean_predictions` 传入后，流程按唯一蛋白去重，默认只取预测前 30%，再将 EC
映射到反应库并写入 GPR；SBML 和 manifest 会保留 CLEAN 来源信息。

如果仓库旁的 CLEAN 输出存在，默认配置会自动使用它；也可以显式传入另一张表。

```json
{
  "engine": "native",
  "clean_predictions": "runs/clean_maxsep.csv",
  "clean_top_fraction": 0.30
}
```

官方无表头输出格式和本机部署说明见 [CLEAN 接入说明](docs/clean-integration.zh-CN.md)。

Gap-fill 严格按照配置的培养基执行：默认是有氧葡萄糖最小培养基，也支持用户自定义
exchange 摄取字典；只有明确设置 `"medium": "rich"` 时才使用全 exchange 的 rich 假设。
若选中的公开参考模板在移除分子氧摄取后仍能生长，native 流程会自动增加一个参考支持的
厌氧生长任务，并在同一次 gap-fill 和后续能量循环 QC 中同时保护有氧与厌氧表型。该判断按
代谢物 ID、公共注释和分子式识别氧，不绑定大肠杆菌反应名；参考模板不支持厌氧生长时不会
强迫专性需氧菌获得该表型。

## 架构

```text
Rich CLI / headless mode
        |
Session + settings + commands + skills
        |
LangGraph StateGraph
        |
LLM 决策 <-> 本地工具 <-> 权限策略
```

核心模块：

- `src/GemAgents/agent.py` 构建 LangGraph 工具循环。
- `src/GemAgents/tools.py` 实现文件、shell、web、MCP、任务、记忆和团队工具。
- `src/GemAgents/cli.py` 提供 REPL、headless、会话、slash command 和输出模式。
- `src/GemAgents/settings.py` 加载用户/项目配置与模型 profile。
- `src/GemAgents/extensions.py` 加载 skills、用户命令、agents 和 output styles。

## GemAgents 功能

- CLI 与 headless print mode：`gemagents`、`-p`、`--output-format text/json/stream-json`
- 基于 LangGraph 的 agentic loop 与本地工具执行
- Rich 终端 REPL
- 会话持久化与 `gemagents resume [SESSION_ID]` / `gemagent resume`
- `/compact` 上下文压缩
- 用户/项目 settings、模型 profiles、`env`、`additionalDirectories`、`respectGitignore`
- `.gemagents/` 下的 skills、用户命令、output styles、agent definitions
- settings 驱动的 tool lifecycle hooks
- 生命周期 hooks：`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`
- 从 `AGENT.md` / `CLAUDE.md` 注入项目上下文，并持久化 `.gemagents/memory.md`
- `default`/`auto` 自动执行、`plan` 只读权限模式
- 写入/编辑前文件历史快照与恢复
- Todo、Task、Memory 工具，其中 Task 持久化到 `.gemagents/tasks.json`
- Web fetch/search 工具
- MultiEdit
- `.mcp.json` MCP resource registry
- 安装了 `pwsh` 或 `powershell` 时可用的 PowerShell 工具
- 基于持久化 task 与 memory 记录的 sub-agent/team mailbox 工具面
- `--image` 多模态图片附件
- 内置命令：`/help`、`/clear`、`/compact`、`/history`、`/skills`、`/agents`、`/commands`、`/output-style`、`/mcp`、`/config list|get|set`、`/mode`、`/model`、`/hooks`、`/permissions`、`/diff`、`/context`、`/file-history`、`/tasks`、`/memory`、`/session-export`、`/rewind`、`/diagnostics`

## 环境

创建或更新项目环境：

```bash
conda env create -f environment.yml
conda activate gemagents
```

如果环境已存在：

```bash
conda env update -n gemagents -f environment.yml --prune
conda activate gemagents
```

使用 pip 时，核心 agent 安装命令为 `pip install .`；完整本地流程使用
`pip install '.[metabolic,memote,benchmark,status]'`。锁定安装、验证矩阵和旧入口迁移说明见
[环境矩阵](docs/environment-matrix.md)。

## 模型配置

直接设置模型：

```bash
export GEMAGENTS_MODEL="gpt-4o-mini"
export OPENAI_API_KEY="..."
```

本地 Ollama 模型也可以直接用 Ollama 环境变量作为默认值：

```bash
export OLLAMA_MODEL="qwen3.5:35b"
export OLLAMA_BASE_URL="http://localhost:11434"
```

或使用 `.gemagents/settings.json`：

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

## 使用

单次运行：

```bash
gemagents --model gpt -p "审查这个项目。"
gemagents --model gpt -p "总结这张截图" --image ./screen.png
gemagents --model gpt -p "返回 JSON" --output-format json
```

交互模式：

```bash
gemagents --model gpt
gemagents --model gpt --resume
gemagents resume
gemagents resume SESSION_ID
gemagent resume
gemagents --model gpt --plan
gemagents --model gpt --permission-mode default
```

交互模式会显示模型处理、工具调用和任务进度等可审计事件；这不是隐藏思维链。`resume`
不带会话 ID 时会列出会话并让你选择，带 ID 时直接恢复指定会话。

## 扩展文件

项目级扩展放在 `.gemagents/`：

```text
.gemagents/
├── settings.json
├── skills/<name>.md
├── commands/<name>.md
├── agents/<name>.md
└── output-styles/<name>.md
```

Markdown 文件支持 YAML frontmatter。命令和 skill 正文支持 `$ARGUMENTS`、`$1`、`$2` 等参数替换。

## 验证

```bash
pytest
ruff check .
python -m compileall -q src tests
```

迁移 rationale 和功能映射见 `docs/review-and-migration.md`。

## 发布与评价状态

工程版复现说明见 docs/releases/REPRODUCE.md。
运行 scripts/reproduce_release.py 可生成带代码、来源和运行产物哈希的能力清单。
当前锁定的表型数据不足以支持生物学准确率或算法优势声明，参考辅助轨道也不会被当作独立证据。

## 分析子智能体与 QHEPath

QHEPath 属于 GemAgents 的分析能力。分析子智能体识别到“异源途径设计”或“QHEPath”需求后，通过 `analyze_request` 调用确定性 QHEPath 算法；宿主模型、跨物种网络模型、底物和产物 ID 作为分析参数显式传入。输出包含基线产率、网络最大产率、最少异源反应数和多个次优/最优方案，并保留模型 SHA-256。该结果是模型条件下的化学计量预测，不替代实验验证。

同一分析入口也支持 FSEOF。分析子智能体识别到“FSEOF”“过表达靶点”或“下调靶点”后，传入基因组代谢模型、`biomass_id` 和产物反应 `objective_id`，按逐步强制产物通量计算反应斜率：正斜率列为过表达候选，负斜率列为下调候选；可选 `use_fva` 和生长约束，结果会附带下调反应的敲除后生长值。

分析子智能体还支持 SMETANA 风格的群落代谢互作分析：识别到 `SMETANA`、微生物群落或交叉喂养需求后，通过 `community_models` 显式传入至少两个 SBML/COBRA JSON 模型，使用 COBRApy 的 FVA 计算供体分泌与受体摄取潜力，输出交叉喂养候选、代谢互作潜力、物种贡献和资源重叠。该适配器保持只读，并兼容上游 [SMETANA](https://github.com/cdanielmachado/smetana) 的群落分析方向；它不会把候选互作解释为已观测的共存、代谢物转移或群落适应度。

同一入口会根据用户需求调用 COBRApy 的 FBA、pFBA 和 FVA（也可直接使用 `simulate_fba`、`simulate_fva` 工具）。报告固定保留以下解释边界：FBA 的最优目标值是在给定约束下的最大值，不代表真实细胞唯一采用的通量状态；pFBA 的简约通量不等于实测酶成本；FVA 区间不是统计置信区间，不同反应的极值也不保证能够同时达到。实现基于 [COBRApy](https://github.com/opencobra/cobrapy)，模型文件和群落成员均以 SHA-256 绑定并保持源文件不变。
