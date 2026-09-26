# FAA/FNA 端到端代谢模型重建

当前入口完成序列校验、注释证据收集、EC 到反应映射、重建、独立 CER 质控和 SBML 导出。
重建计算在本地完成；DeepSeek 负责调用工具和解释报告，不负责猜测酶功能或修改化学计量。
native 质控流程保持独立实现，并借鉴 MQC 将“无源净生成”和“限定培养基下的产率上限”分层检查，
以及 pear 由失败生长任务驱动路径补全的思路。用户显式提供 MQC 路径时，可通过独立适配器保存
MQC 原始报告并复算 biomass；外部报告不会覆盖 GemAgents 质量证书。

默认引擎现在是 native：优先读取修复后的 `data/reaction_library_v8/` 和
`data/prokaryotic_biomass_library_v2/`，未物化新资产时回退到 v6 兼容目录，不调用
CarveMe 或 Reconstructor。v8 以公开 BiGG union 为总库，保留 v6 的质控和来源审计，
并把 BiGG 与 ModelSEED v3 统一到同一套代谢物和反应方程，
并将不平衡、冲突和化学信息不足条目留在 `reaction_catalog.jsonl`，不作为严格初始证据。
NCBI 注释命中的反应先经过 v6 质控；缺口填补优先使用通过质控的反应，只有任务不可行时才允许高代价的
`rescue_for_growth` 反应；该类反应会在 `gapfill-report.json` 中单独标记并触发 `needs_review`。
公开 BiGG 模型 census 与 biomass 统计保存在
[`data/public_biomass_registry_stats.json`](../data/public_biomass_registry_stats.json)；没有经校验的
参考 FNA/FAA 的模型仅用于统计，不会自动参与序列相似度选择。

细菌和古菌使用同一套 native 流程：默认 `reaction_library_mode=strict`、CLEAN 预测前 30%、
公开 reference scaffold、gap-fill 和 QC 均不按域分叉。v8 reaction library 的
`applicable_kingdoms` 明确包含 `bacteria` 和 `archaea`；v2 biomass catalog 共享同一组
原核模板和反应库。古菌声明了细菌来源的 `iAF692` 时，按可审计策略解析为通用
`tongyong`，报告保留请求域、来源域和 fallback 原因；若前体在给定培养基不可达，仍保留
失败检查，不把草稿伪装成可生长模型。

## 注释路线

| 输入与选择 | 实际执行 | 条件与范围 |
|---|---|---|
| FNA，默认 `auto` | 完整 NCBI PGAP → 注释导入 | 需 PGAP、物种名、可用容器运行时 |
| FAA，默认 `auto` | NCBI 酶 equivalog HMM → EC 证据 | 本地 PyHMMER；不等同于完整 PGAP |
| FAA/FNA + `annotation_gbk` | 导入已有 GenBank/NCBI/PGAP 注释 | FNA 要求基因组序列完全一致；FAA 要求每条蛋白精确匹配 CDS 翻译 |
| FNA，显式 `pyrodigal-ncbi-hmm` | Pyrodigal 预测 CDS → NCBI 酶 HMM | 当前 Windows 可运行；基因预测来自 Pyrodigal |

PGAP 是原核注释流程，不能直接把 FAA 当作完整基因组输入。它综合多种证据，
不宜未经物种和任务基准就断言在所有情形下“最准确”。当前实现先覆盖细菌、古菌；
真核 FNA 的剪接基因预测、区室与生物量方案尚未接入。
见 [NCBI PGAP 要求和输入输出](https://github.com/ncbi/pgap/wiki/Quick-Start)、
[NCBI 注释证据](https://www.ncbi.nlm.nih.gov/genome/annotation_prok/evidence/)。

首次重建验证时，本机没有可用的容器环境，PGAP 预检明确失败，没有自动换成其他注释方法。
随后按用户要求部署了独立 WSL2 / Ubuntu / Docker，并加入完整 PGAP 安装和调用入口，
最新状态和用法见 [PGAP 部署说明](pgap-deployment.zh-CN.md)。
PGAP 官方要求约 100 GB 磁盘、每 CPU 2–4 GB 内存和容器运行时。已有 Linux PGAP 结果仍可直接导入。

## 安装

本机已使用独立 `.venv-research`，Python 3.13.9；没有修改默认 Python 或其他项目环境。
重新部署时，在项目根目录执行：

```powershell
python -m venv .venv-research
.\.venv-research\Scripts\python.exe -m pip install -e ".[dev,metabolic]"
.\.venv-research\Scripts\python.exe -m pip install ./carveme ./reconstructor
.\.venv-research\Scripts\python.exe -m GemAgents --prepare-ncbi-hmms data/ncbi_hmm
```

CarveMe 使用普通安装，以避免同名外层源码目录对 editable 导入的遮蔽。本流程用 SCIP/GLPK，
不需要商业求解器。调用的是 NCBI 证据驱动的重建适配，不调用 DIAMOND 注释。

Reconstructor 另需公开 ModelSEED EC 对照表，本机已准备：

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/ModelSEED/ModelSEEDDatabase/master/Biochemistry/reactions.tsv -OutFile data/ncbi_hmm/modelseed_reactions.tsv
```

NCBI 缓存保存原始文件和 SHA256。当前数据库为 `hmm_PGAP/20.0`，从 18,950 个模型中选择
4,495 个带完整 EC、可命名的 equivalog 模型；应用 NCBI 给出的序列和结构域分数阈值。
这是偏保守、覆盖有限的酶证据集。缺失 EC 不代表基因不存在或不具备代谢功能。
参考 [NCBI HMM 发布目录](https://ftp.ncbi.nlm.nih.gov/hmm/current/)。

## 直接重建

对一个目录中的多条 FAA 序列，可以直接向 Agent 描述“对目录下所有序列建模”。路径可以用
英文或中文引号包住，路径含空格时也不需要手动拆分；路由器会去除路径分隔用的引号和首尾
空格，再识别目录批处理意图。若缺少映射文件但工作区中存在本地基准清单和公开 BiGG 模型，
Agent 会先自动生成带版本和哈希的映射，再校验反应库、生物量库和 HMM 目录，最后由
`metabolic_batch_start` 提交可恢复的后台批处理。只有依赖或必要资产确实缺失时才会停止并
报告具体原因；不要让模型自行拼接 shell 命令。当前仓库也可以直接使用同一个确定性脚本。

如果输入目录是仓库根目录下的 `bigg`、输出目录是 `bigg_model`，可以直接运行零参数入口；
它会自动选择有效映射和当前默认反应库、生物量库及 HMM 目录：

```bash
conda run -n gemagents env PYTHONPATH=src \
  python scripts/build_bigg_models.py
```

需要覆盖路径或只重跑指定菌株时，可追加 `--input-directory`、`--output-directory`、
`--mapping`、`--only GCF_...` 或 `--max-builds N`。先查看解析结果而不启动构建：
`python scripts/build_bigg_models.py --dry-run`。

通过交互式 `gemagents` 发起时，Agent 会保持当前终端连接，持续读取批处理状态和 worker
日志，显示 `已完成/总数` 及每个菌株的结果，并一直等待到 `completed`、`failed` 或
`cancelled` 后才返回最终答复。只有直接运行下面的脚本时，才需要另行查看
`batch-status.json`；按 Ctrl-C 只会停止前台监控，不会自动终止已提交的 worker。

```bash
python scripts/run_bigg_gemagents_batch.py \
  --workspace . \
  --mapping bigg_model/preparation/mapping.native.json \
  --biomass-library data/prokaryotic_biomass_library_v2 \
  --reaction-library data/reaction_library_v8 \
  --hmm-directory data/ncbi_hmm \
  --output-directory bigg_model
```

批处理状态写入 `batch-status.json`；每个可映射序列独立写入 `builds/<assembly>/`，空文件、
缺少映射或当前不支持的真核输入保留在状态表中，不会被伪装成成功。

FAA 输入：

```powershell
 .\.venv-research\Scripts\python.exe -m GemAgents --reconstruct sample.faa --engine native --output-dir runs/sample_faa
```

已有匹配的 NCBI/PGAP GenBank 注释：

```powershell
 .\.venv-research\Scripts\python.exe -m GemAgents --reconstruct sample.fna --annotation ncbi-import --annotation-gbk annot.gbk --engine native --output-dir runs/sample_ncbi
```

在本机已部署的 PGAP 环境中，从原始 FNA 开始：

```bash
python -m GemAgents --reconstruct sample.fna --annotation pgap --organism "Escherichia coli" --output-dir runs/sample_pgap
```

PGAP 可通过 `--setup-pgap` 安装或继续部署，使用 `--check-pgap` 检查实际就绪状态。
本机部署默认 2 CPU、容器内存 `6g`，可用 `--cpus`、`--pgap-memory` 或 JSON 覆盖；
默认外部进程超时 7,200 秒。启动参数禁用可选使用情况上报。
核酸发生规范化变化时，严格序列检查可能拒绝导入，需要核实变化来源，不按文件名放行。

本机原始 FNA 可明确选择：

```powershell
.\.venv-research\Scripts\python.exe -m GemAgents --reconstruct sample.fna --annotation pyrodigal-ncbi-hmm --genetic-code 11 --output-dir runs/sample_native
```

此路线适用于原核完整/草图基因组，单基因组训练至少需 20 kb 序列。遗传密码表必须与物种一致；
本次 *Mycoplasmoides genitalium* 示例用表 4。通用 `.fa/.fasta` 扩展名需要显式
`--input-type faa` 或 `fna`。输出目录必须新建或为空，避免混入上次的模型。

## 配置文件与 Agent

例如保存为 `reconstruction.json`：

```json
{
  "input": "sample.fna",
  "annotation": "ncbi-import",
  "annotation_gbk": "annot.gbk",
  "engine": "reconstructor",
  "kingdom": "bacteria",
  "gram": "negative",
  "modelseed_reactions": "data/ncbi_hmm/modelseed_reactions.tsv",
  "medium": "minimal",
  "min_growth": 0.01,
  "quality": "repair",
  "max_edits": 10,
  "solver_timeout": 120,
  "output": "runs/sample_reconstructor"
}
```

```powershell
.\.venv-research\Scripts\python.exe -m GemAgents --reconstruct-config reconstruction.json
```

gap-fill 严格按照 `medium` 执行：默认是有氧葡萄糖最小培养基；只有显式写成
`"medium": "rich"` 时才允许反应库中所有 exchange 摄取。也可将 `medium`
改为 exchange ID 到最大摄取速率的字典，未列出的摄取全部关闭，拼错 ID 会失败。
`rich` 只是筛查假设，不是默认培养基，也不应被用来替代用户的实测培养基。
BiGG 与 ModelSEED 使用不同 ID，必须按所选反应库填写。

native 引擎还会从选中的公开参考模板派生“参考支持的培养条件任务”。当前实现会按交换
代谢物 ID、BiGG/ModelSEED 注释、名称和 `O2` 分子式识别分子氧端口；若参考模板在移除这些
端口后仍达到 `min_growth`，则将同一去氧培养基作为额外 gap-fill 任务。流程先在
NCBI/CLEAN/公开参考候选层求解，确实不可行时才开放无 GPR 的守恒化学候选。额外条件新增
反应会逐条敲除复验，删除不影响任何必需条件的冗余项；原始培养基已经过 QC 的 gap-fill
集合不做这种扩展删减。所有必需条件随后一并进入 CER repair 的任务保护，避免修复 ATP/GTP
循环时重新切断厌氧或其他参考表型。参考模板本身不支持去氧生长、培养基本来不含氧，或
自定义培养基无法映射到参考模板时，不推断厌氧能力。
CarveMe 默认细菌模板，可选 `gram` 或 `kingdom=archaea`；Reconstructor 必须明确
Gram 阳性/阴性，当前不能用于古菌。支原体示例的通用细菌生物量也是近似模板。

Agent 工具 `metabolic_start(config_path)` 在工作区范围校验后启动后台计算，返回 `job_id`、
输出和日志路径；`metabolic_status(job_id)` 读取完成/失败状态，也能识别异常退出。
可向 GemAgents 输入“按 reconstruction.json 启动重建”，再按返回的任务 ID 查询。
`plan` 模式允许查询、禁止启动；`auto` 可执行已经授权的重建。
后台作业与日志在 `.gemagents/metabolic_jobs/`，长计算不占用普通 shell 工具的 30 秒时限。

DeepSeek 支持标准 `DEEPSEEK_API_KEY`，也兼容输入中使用的 `DEEPEEK_API_KEY` 拼写。
本机密钥已保存为 Windows 用户环境变量，不在代码、JSON 配置或报告中。
新终端可直接读取；当前终端如需立即使用：

```powershell
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
.\.venv-research\Scripts\python.exe -m GemAgents --model deepseek --auto
```

默认选择当前账户模型列表中的 `deepseek-v4-flash`，可通过 `DEEPSEEK_MODEL` 覆盖。
2026-09-10 实测 `/models` 返回 200；真实 Agent 对话返回 **402 / Insufficient Balance**。
因此接线和本地后台工具已验证，在线 LLM 工具调用仍等待账户恢复余额。
协议参数依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/) 和当前账户模型列表。

## 模型与报告

每次成功执行会得到：

- `proteins.faa`、`annotation.json`：蛋白和可追溯的 HMM/导入证据。
- `gene_id_map.json`：输入标识到模型安全基因 ID 的映射；同基因座的不同蛋白分别保留。
- `reaction_evidence.json`：EC 候选、来源、歧义、候选基因和可保留的 GPR。
- `draft.xml`、`model.xml`：质控前后 SBML；探针不会留在导出模型中。
- `quality.json`：探针、反例、任务保护、修复记录、参考生长校准、bounds/方向摘要和静态元素/电荷检查；求解超时会标记为 `incomplete`，不会写成通过。
- `manifest.json`：实际路线、配置、数据库/输入/模型校验和、依赖版本、运行状态。
- `engine-report.json`：Reconstructor 适配分支的 LP 状态、补入反应和耗时。

一个 EC 可对应多种底物、区室和反应。实现记录这种歧义，候选评分为 `1/候选反应数`，
不把所有候选直接包装成确定 GPR；多亚基描述不凭单次命中补造 AND 关系。
这些保守规则会降低 GPR 覆盖，尚未完成完整复合体和底物特异性解析。
Reconstructor 的 ModelSEED 表按公共反应 ID 连接，保存版本校验和，但尚未完成跨版本
逐条化学等价核验。这也是模型继续人工/实验复核的原因。

CarveMe 分支使用公开 carving MILP；该版本 SCIP 路径内部设置 600 秒求解上限并允许可行
次优解，因此本次结果不声称全局最优。`solver_timeout` 控制 COBRApy 的 LP，不覆盖此上限。
Reconstructor 分支使用其公开反应库和 pFBA 代价思想，在原库上直接求解，省去相同反应的
删除/重加；采用用户声明的绝对最小生长约束，结果不等同于原 Reconstructor CLI。
这是适配与候选算法实现，不能仅凭一个运行就宣称文献意义上的新算法或速度优势。

`quality=audit` 只检查；`repair` 使用独立 CER，在反例中限制一个反应方向并检查生长任务，
最终强制执行不使用缓存的复验。反应库和参考支架导入都会先应用审计过的共享代谢物定义：
`rbflvrd_c` 的黄素氢计量，以及 `murein5px4p_p`/`murein4px4p_p` 的肽聚糖电荷会统一到
库的 canonical 值；参考模型的旧属性只写入冲突记录，不覆盖总库定义。ATP/NTP 闭环审计识别出的
`rxn00362_c`、`PYNP1` 和 `NDPK6` 反向捷径也会在严格运行视图中设为单向，并在
`quality.json` 的 `energy_direction_guards` 中留痕。除这些明确的审计修正外，静态元素/电荷不平衡
不会通过猜测化学计量“修好”。
当 `reference_growth_ceiling=true` 时，流程还会在完全相同的 minimal、rich 或用户培养基下计算
`reference_support_path` 公开模型的最大生长；`repair` 模式把该值作为 biomass 上限，同时在
`quality.json.growth_calibration` 保留校准前增长、参考增长、校准后增长和来源。参考模型不能在所选
培养基生长或无法解析该培养基时会直接失败，不会静默跳过保证。
当前探针覆盖严格关闭全部边界后的联合物质生成和明确列出的辅因子：BiGG 的 ATP/GTP/
NADH/NADPH，ModelSEED 的 ATP；并在用户声明的培养基下逐个检测 biomass 反应物是否可合成，
检测时关闭非 exchange 的 demand/sink，避免内部可逆 sink 造成假阳性。没有覆盖全部能量货币、
全部区室、真实培养条件或膜电势。
`completed_with_findings` 表示流程完成但有问题，`declared_checks_passed` 也仅指所列检查。
模型始终是待生物学验证的草稿。早期 PGAP 运行未启用 MEMOTE；后续公开 MEMOTE 评分见本文末尾更新。

## 验证与研究状态

```powershell
.\.venv-research\Scripts\python.exe -m pytest tests experiments/counterexample_repair/code/test_cer.py -q
.\.venv-research\Scripts\ruff.exe check src tests experiments/counterexample_repair/code
```

实测汇总见 [端到端验证记录](genome-validation.zh-CN.md)。样本来自公开 NCBI，
不是用户私有基因组，也不是以真实表型为标准的准确率基准。FAA/HMM、FNA/原生注释、
NCBI 注释导入、两种重建适配、后台任务和报告分别验证；完整 PGAP 已在独立 WSL2/Docker
中完成公开 FNA → 注释 → CarveMe → 质控 → SBML 实测。在线 DeepSeek 工具调用仍未通过
余额检查，不能将其算成已验证。

## MEMOTE 与合并反应库（2026-09-12 更新）

项目接入公开 `memote==0.17.0`，使用官方默认权重和独立 CLI/API。Protokaryon 的 EC 快照只作为
反应别名和来源审计输入；MQC 通过独立适配器运行。MEMOTE、MQC 与本项目 `quality.json` 的声明探针
分开记录，不能把任何单一分数解释为生物学准确率或 CER/MQC 证明。由于当前 COBRApy/MEMOTE 依赖
组合有少数上游测试兼容错误，报告会明确标记 `completed_with_test_errors`，不会伪造缺失分数。

BiGG（CarveMe）与 ModelSEED（Reconstructor）反应被整理到 `data/reaction_library_v6/`。catalog 保留 41,770 条来源记录；v3 兼容 union 的历史基线为 25,752 条反应、16,033 个代谢物。v6 在修正 ModelSEED alias 唯一化、等价方程去重和结构冲突隔离后，canonical full union 为 25,339 条反应、15,972 个代谢物；严格质控后 24,358 条，active operational 库为 24,406 条、15,774 个代谢物。合并要求唯一 BiGG alias、分子式、电荷、区室和无已知 InChIKey 冲突；不平衡、化学信息不足、冲突和备用 biomass 仍可追溯但只留在 catalog。方程等价使用方向/缩放不敏感的化学计量指纹。native 和 CarveMe 适配器默认读取严格质控的 `universe.xml.gz`；显式 `reaction_library_mode: "full"` 时才读取完整 union。两者均保留来源映射和 SHA256 清单。

真实验证：PGAP/CarveMe 原模型 MEMOTE 总分 75.8729%；合并库 v2 Reconstructor 模型 60.9777%；v6 高精度库已接通 NCBI/PGAP 注释并完成 Reconstructor 重建，模型 MEMOTE 为 63.4667%。三个运行均应结合 `summary.json`、`quality.json` 和 `manifest.json` 解读，仍属于待生物学验证草稿。



## v6 统一高精度反应库与 NCBI 注释衔接

`data/reaction_library_v6/` 是统一入口，不再把 BiGG 和 ModelSEED 当作两个互不相干的建模库。`universe_full.xml.gz` 保存完整 union，`universe.xml.gz` 是严格化学质控库，`universe_bigg.xml.gz` 是同一质控规则下的 CarveMe 子集。每个反应同时保留 BiGG/ModelSEED 来源、方向、边界、EC 和化学质控状态；`reaction_quality.tsv/json` 记录完整审计。严格库不再因为某个富培养基生长解使用了不守恒反应而将其恢复；未知化学信息只可在显式 `allow_unverified_gapfill` 的最高成本隔离层中使用并写入报告。

质控规则是独立实现的公开化学检查：内部反应要求有限且有序的上下界、可解析分子式/电荷和 COBRApy 元素/电荷守恒；EX/DM/SK、Growth/biomass 明确作为边界或伪反应例外。历史 v6 数字仅用于旧版本回溯，新库应以其自身 `manifest.json` 和 `reaction_quality.json` 为准。
代谢物唯一化优先选择 BiGG canonical ID；多个 ModelSEED ID 只作为 alias/provenance 保留。反应方程唯一化先保留 BiGG 代表，再把等价 ModelSEED 方程、方向和边界差异写入 catalog。

可用公开 BiGG 模型集合继续扩充总库：同方程的反向或倍数写法会先换算到 canonical flux
坐标后合并真实 bounds，不再固定扩大到 `±1000`；同 ID 不同方程以稳定 `BIGGVAR_*` ID
隔离；公开模型一致提供、而基础库缺失的 formula/charge 会被补齐，来源冲突则保持未知并进入
隔离层。每条来源记录、换算比例和文件 SHA256 保存在 catalog。命令示例：

```bash
gemagents --prepare-reaction-library data/reaction_library_next \
  --bigg-models-directory /path/to/public_bigg_models
```

随后使用 PGAP 的 GenBank NCBI 注释导入（504 proteins、179 条带 EC）进行功能证据映射。v6 Reconstructor 实际输出 2,664 reactions、2,631 metabolites、5 GPR genes，MEMOTE 为 63.4667%，状态为 `completed_with_test_errors`。该分数只描述模型一致性/注释测试，不能替代实验验证。

CarveMe 对稀疏高精度子集的 MILP 约束更敏感。当前默认 `reaction_library_mode` 为 `"strict"`，读取 v8 catalog 的严格质控 `universe.xml.gz`；需要审计完整 union 时显式设置 `reaction_library_mode: "full"`。运行 manifest 会保存实际模式。历史 v6 的 NCBI/CarveMe 实测数字仅用于版本回溯。

## Native v6 建模流程（2026-09-13）

当前默认建模器为项目自有的 native v6 流程，不再依赖 CarveMe 或 Reconstructor。输入先由扩展名和序列字母表校验为 FAA/FNA；FNA 可走已部署 PGAP/NCBI GenBank 导入，FAA 可走 NCBI GenBank 精确蛋白导入或 NCBI equivalog HMM。注释证据经 EC/反应映射进入 v6 统一库，模板规则再按参考蛋白精确哈希、基因符号、EC 和序列 identity/coverage 编译 GPR。`and` 要求全部子单元有证据，`or` 只保留可证实的替代支路，不凭 EC 猜复合体。

默认允许一个 EC 命中多个候选反应时生成带 `ambiguous` 标记的 OR-GPR，以提高基因覆盖率；这不会把歧义证据伪装成确定反应。需要严格唯一 EC 映射时，在配置中设置 `allow_ambiguous_ec_gpr: false`。

默认配置会在仓库及其相邻的 CLEAN 工作目录中发现可用的 CLEAN 运行时，并对每次输入生成的候选 FASTA 运行预测，再按前 30% 置信度纳入证据；不会自动复用 iML1515 的历史预测表。同时在公开 iML1515 参考资产存在时启用 `reference_assisted` 的 `reference_scaffold`，将参考网络及其可复核来源写入 manifest。若需要 de novo 建模，可设置 `reference_support: false` 或 `reference_scaffold: false`；需要复核既有结果时可显式填写 `clean_predictions`。

公开 biomass catalog 由 `--prepare-biomass-library` 编译，当前的 iML1515 catalog 还保存了从公开 BiGG 模型独立重建的 Fe-S template support 反应；这些反应经过独立守恒检查并保留来源 SHA256。catalog 保存模型、参考 GenBank/FNA、序列 sketch、反应 ID 映射、GPR 模板和 SHA256。模板选择只作为相似度排序依据，不宣称 ANI 或菌株准确率。

选择 biomass 后，native 引擎读取用户培养基并按 `reaction_library_mode` 选择 v8 反应库（默认严格质控，显式 `full` 才使用完整 union），执行加权 LP gap-filling，导出 `initial_model.xml`、`biomass-selection.json`、`gapfill-report.json`、`draft.xml` 和 `model.xml`。当 `reference_support` 开启时，流程还读取公开 iML1515 反应方程作为独立候选池：共享代谢物采用公开模型的标准 BiGG ID 和化学定义，只有守恒方程进入候选，且候选不会进入 NCBI 证据初始模型；参考反应使用 `REF_iML1515_` 前缀保留来源。biomass 使用 `fad_c`、`ribflv_c`、`murein5px4p_p`、`udcpdp_c` 等标准 ID，不构造隔离的 biomass 前体池。

2026-09-17 的多条件回归从大肠杆菌厌氧失败定位到单条件 gap-fill：有氧解使用耗氧的
`PDX5POi`，去氧后 `pydx5p_c` 路径中断。通用参考表型任务从公开 iML1515 候选中选择
`PDX5PO2` 和必需的伴随转运，删除 1 个不必要候选后，最终模型在有氧 minimal 下生长
0.876997、厌氧 minimal 下生长 0.059963，无葡萄糖时 biomass 为数值 0；ATP、GTP、NADH、
NADPH 和联合物质净生成探针全部通过。该实现没有写死上述反应 ID；非大肠杆菌命名的玩具
回归证明条件任务通过氧端口语义和参考表型触发。

当前端到端验证输出为 [`runs/iml1515_native_v32`](../runs/iml1515_native_v32/)：3964 reactions、3588 metabolites、1334 genes，严格 gap-fill 完成，加入 56 条参考候选反应，Growth 为 3.9894；静态不平衡和未知化学信息均为 0，joint-material、ATP、GTP、NADH、NADPH 探针全部通过。公开 MEMOTE 默认权重得分为 66.9343%，但存在 4 个 MEMOTE 0.17 Windows/模型注释兼容性测试错误；该分数只描述一致性和注释覆盖，不能替代实验验证，也不能据此宣称相对 iML1515 的独立准确率优势。
