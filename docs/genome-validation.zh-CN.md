# 端到端验证记录 — 2026-09-10

验证环境是独立 `.venv-research`，Windows / Python 3.13.9。以下记录属于早期工程集成测试，
当时未读取 MQC 或 pear，不能代表当前新增的外部 MQC/Protokaryon 复核流程，也不是代谢表型准确率基准。

## 已完成的输入与模型输出

| 公开样本 / 路线 | 蛋白数 | 带 EC 的蛋白 | 候选反应 | 模型反应 / 代谢物 / GPR 基因 | 总耗时 |
|---|---:|---:|---:|---:|---:|
| NC_000908.2，FNA + NCBI 注释导入 → CarveMe | 504 | 181 | 142 | 371 / 310 / 0 | 611.4 s |
| NC_000908.2，FAA → NCBI HMM → CarveMe | 504 | 117 | 115 | 441 / 382 / 19 | 624.5 s |
| NC_000908.2，FNA → Pyrodigal 表 4 → NCBI HMM → CarveMe | 527 | 117 | 115 | 441 / 382 / 19 | 628.3 s |
| NC_000913.3，FNA + NCBI 注释导入 → Reconstructor 适配 | 4300 | 1454 | 5587 | 7695 / 4945 / 256 | 89.1 s |
| NC_000908.2，FNA → 完整 PGAP → CarveMe | 504 | 179 | 139 | 367 / 320 / 23 | 1430.7 s |

第一行是开发早期的完整运行，彼时导入证据全部按复合体未解析处理，所以不生成 GPR。
随后已修正为保留蛋白标识、CDS 位置和复合体描述；第四行验证了新版导入路径。
前两次运行开始于输出字段更名前，保留了旧的 `release_ready=false` 字段；当前代码
使用范围更准确的 `declared_checks_passed`。这些差异保留在记录中，不回写原始结果。

基因组来源与校验和：
[支原体来源记录](../data/public_mgen/source.json)、
[大肠杆菌来源记录](../data/public_ecoli/source.json)。数据从
[NCBI EFetch](https://www.ncbi.nlm.nih.gov/books/NBK25501/) 获取，记录 accession、实际 URL 与 SHA256。
运行结果目录依次为：

- [Mgen 注释导入](../runs/mgen_ncbi_import_v3/manifest.json)
- [Mgen FAA/HMM](../runs/mgen_faa_hmm_v1/manifest.json)
- [Mgen FNA/原生流程](../runs/mgen_fna_hmm_v1/manifest.json)
- [E. coli / Reconstructor 适配](../runs/ecoli_ncbi_reconstructor_v3/manifest.json)
- [Mgen 完整 PGAP](../runs/mgen_full_pgap_v1/manifest.json)

导出复查重新读取 SBML，校验文件哈希、模型计数、培养基摄取边界、实际生长可解性，
以及导出模型中无临时质控探针。五个模型均通过这些导出检查。
这不等于五个模型都通过了质控。

## 质控发现

### native v6 端到端复验（2026-09-13）

同一份 NC_000913.3 基因组分别走 FNA 和 FAA 输入，均使用精确 NCBI GenBank 导入、
BiGG+ModelSEED v3 native 库和公开 biomass 模板。两条路线均生成 4,300 条蛋白、
1,454 条带 EC 蛋白、2,944 个候选反应和 5,194 个模型反应；FNA 用时约 71 秒，FAA 约 61 秒。
LP 缺口填补各加入 `PKETX`，该反应属于 v6 质控隔离的 `rescue_for_growth`，所以输出明确为
`completed_with_findings`，并不会把它宣称为严格守恒通过。ATP/GTP 探针仍需人工复核；
  这证明端到端链路已打通，同时保留了化学质量警报。

### IML1515 对比（2026-09-13）

v20 在 v19 的基础上加入了用户授权的 pear `2fe2s_c` 部分来源记录。导入范围仅为
`I2FE2SR/I2FE2ST/I2FE2SS`、`S2FE2SR/S2FE2ST/S2FE2SS`、`BTS5` 和 `LIPOS` 八条反应；
它们与公开 iML1515 记录重复，因此作为 alternate provenance 保存，不覆盖 iML1515 的 GPR，
也不把 pear 的未知 GPR 当作序列证据。catalog 现在会把 `2fe2s_c` 识别为经过支持网络验证的
biomass 代谢物，`usable=true`。

已使用同一 NC_000913.3 基因组构建 native v6 模型，并直接使用
`BIOMASS_Ec_iML1515_core_75p37M`。native v6 v19 共有 1,298 个 GPR 基因和 1,671 个带 GPR
反应；iML1515 为 1,516 个基因和 2,266 个带 GPR 反应。native v6 的 biomass 方程直接
使用 iML1515 版本，不再替换为 textbook biomass。v19 从公开 iML1515/BiGG 记录独立加入
27 条通过守恒检查的 Fe-S template support 反应；但其他辅因子前体仍缺少可验证网络，严格 gap-fill 不可行，
所以输出的是明确标记的 non-growing draft。

iML1515 的 ATP/GTP 探针为 0，native v6 v19 的联合物质、ATP、GTP、NADH、NADPH
五个探针也均为 0。v19 将只有模糊 EC 且没有可验证 GPR 的反应留在 catalog，避免它们
进入序列支持模型形成内部能量循环。`2fe2s_c` 现在可以由 support 网络产生，但当前版本仍没有生物学性能优势，因为 biomass 的
严格 gap-fill 不可行、模型生长值为 0；修正后的主要进展是 biomass 方程正确、AND 复合体
逻辑不再降级、失败状态不再伪装成成功。完整结果见
v20 的端到端结果见 [`runs/iml1515_native_v20/manifest.json`](../runs/iml1515_native_v20/manifest.json)、
[`runs/iml1515_native_v20/quality.json`](../runs/iml1515_native_v20/quality.json) 和
[`runs/iml1515_native_v20/biomass-selection.json`](../runs/iml1515_native_v20/biomass-selection.json)。

前三个 CarveMe 输出均通过本次严格边界闭合下的联合净物质、ATP、GTP、NADH、NADPH
探针；重新载入后的最大生长分别约为 0.7447、0.8990、0.8990。
静态守恒检查分别标出 39、80、80 条有残差的反应。例如公开 CarveMe 通用库的 PFK
本身存在电荷残差，本次没有擅自猜测电荷或重写反应式以消除报错。

大肠杆菌 Reconstructor 适配模型的联合物质探针通过，但 **ATP 探针最大值为 1000**，
明确存在本次检测条件下的 ATP 生成反例；还有 5 条反应缺少可完整检查的化学元数据。
报告状态为 `needs_review`，没有把能生长或能导出 SBML 当作合格条件。
当前 `rich` 培养基和生物量均为建模假设，未与实验培养条件或真实生长率校准。

完整 PGAP 的模型也通过上述五种严格闭合探针，最大值均为 0；另有 41 条静态守恒残差，
状态为 `completed_with_findings`，`declared_checks_passed=false`。重新载入 SBML 后的
目标值为 10.7051，这是未校准的富集培养基建模结果，不能解释为真实生长率或质量提升。
PGAP 流程本次为 `audit`，未执行 CER 修复。

Reconstructor 的 5,587 个 EC 候选说明 EC 到反应的一对多歧义很大，不能将其视为
5,587 条可信的菌株特异反应。后续需要更精细的底物、复合体、区室和反应等价证据。

## 修复与性能诊断

第一轮真实 CER 修复在 10 次方向限制预算后保留了生长任务，但仍有 ATP 反例，状态为
`budget_exhausted`。修复用时 433.5 秒、搜索 LP 34 次、缓存命中 10 次；全流程 474.0 秒。
不能把这次结果写成“已消除能量生成”。

运行中的 `py-spy` 栈采样定位到 `Auditor._key` → SymPy 字符串打印 → 加法/乘法项排序。
在同一导出模型上，改用 `srepr(expression, order="none")` 序列化所有约束约 1.034 秒。
新版内核保留完整表达式、约束上下限、变量类型和当前边界检查，同时避免展示用排序；
自定义约束变化导致缓存失效、缓存开关结果一致等回归测试继续通过。
序列化单项耗时不是整个修复的速度提升倍数，后续真实重跑单独记录。

同配置重跑已完成：修复阶段 **433.47 → 125.24 秒**，全流程 **473.98 → 162.30 秒**。
两次都是 34 次搜索 LP、10 次缓存命中和相同的 10 个编辑，仍为 `budget_exhausted`。
进一步比较了模型的反应式、边界、GPR、代谢物元数据和目标函数，草稿与最终模型分别一致；
未按 SBML 字节是否相同判断，因为文件序列化元数据可以变化。
[逐项对照记录](../experiments/counterexample_repair/results/genome_fingerprint_comparison.json)。
这是同机单次运行对，不是受控、多物种速度基准，也不是生物学质量提升。
[新版完整修复记录](../runs/ecoli_ncbi_reconstructor_repair_v2/quality.json) 保留了剩余 ATP 反例。

Reconstructor 适配中的 pFBA gap-filling 本次约 8.785 秒、加入 33 条反应。
旧适配在原反应库删除/重加相同反应的阶段持续消耗 CPU，开发运行被明确停止并保留记录；
新版省去这一步，并改用声明的绝对最小生长约束。两次的算法约束不同，不能当作严格
Reconstructor 原版与新算法的公平速度比较。CarveMe 三次主要耗时接近其 SCIP 600 秒上限。

## 自动化与外部依赖

- 93 项测试通过，覆盖现有 Agent 行为、新输入路径、重复 CDS、HMM 阈值文件完整性、
  EC 歧义、LP 缺口填补、培养基、任务权限、异常退出、13 项 PGAP 集成测试，
  以及 16 项原有 CER 内核测试。
- `ruff check src tests experiments/counterexample_repair/code` 通过；不扫描或改动被排除项目。
- `metabolic_start` / `metabolic_status` 已实际启动并查询大肠杆菌后台重建，完成状态正确。
- 初期完整 PGAP 预检因缺少容器运行时而失败，保留了[历史记录](../runs/pgap_runtime_check_v1/manifest.json)。
  随后部署独立 WSL2、Docker 和完整数据库，完整 PGAP 已通过真实样本验收。
  版本为 `2026-06-18.build8602`，2 CPU / 6 GB。流程开始至进入构建约 825.2 秒，
  总耗时不含首次下载。输出序列与原始 FNA 完全一致，CDS 为遗传密码表 4。
  CheckM 为 98.00 completeness / 0.00 contamination，不代表代谢模型准确率。
  [PGAP 文件验证](../runs/mgen_full_pgap_v1/pgap-validation.json)、
  [部署说明](pgap-deployment.zh-CN.md)、
  [PGAP 模型复查](../experiments/counterexample_repair/results/pgap_pipeline_summary.json)。
- DeepSeek `/models` 成功，但 Agent 实际请求返回 402 余额不足，在线工具调用未完成验证。
  [连接记录](../runs/deepseek_connectivity.json)。
- 在源码、测试、文档、运行报告和后台日志中检查了用户密钥的意外写入，未发现匹配；
  密钥只保存在用户环境变量中。早期记录未生成 MEMOTE 分数；2026-09-12 更新已补充公开 MEMOTE 实测结果。

机器可读汇总和复查脚本：
[汇总 JSON](../experiments/counterexample_repair/results/genome_pipeline_summary.json)、
[复查脚本](../experiments/counterexample_repair/code/genome_smoke_summary.py)、
[环境版本](../experiments/counterexample_repair/results/genome-environment-freeze.txt)。
实际结果和未通过项是后续算法实验的起点，不构成全面质量保证或算法准确率优势的证据。

## 2026-09-12：MEMOTE 与双库合并实测

公开 MEMOTE 0.17.0 已接入 FAA/FNA → NCBI/PGAP 注释 → CarveMe 或 Reconstructor → SBML 流程。结果目录会生成 `results.json`、`scored-results.json`、`report.html`、`summary.json` 和 `score-config.yml`；默认权重来自 MEMOTE，3 个已知依赖兼容错误会保留在 `test_errors`，加权项目完整时状态为 `completed_with_test_errors`。

BiGG/ModelSEED v3 合并库清单见 [`data/reaction_library_v3/manifest.json`](../data/reaction_library_v3/manifest.json)。完整 union 25,752 条 active reactions，CarveMe 子库 5,532 条 BiGG reactions；来源 catalog 共 41,770 条，包含被化学平衡、结构冲突和 biomass 策略排除的可追溯记录。

本次实测分数：PGAP/CarveMe 原模型 75.8729%，合并库 Reconstructor 60.9777%，合并库 CarveMe 76.1439%。分数只反映公开 MEMOTE 的一致性和注释测试，不能替代实验表型或质量认证，也没有复用 MQC/pear。



## 2026-09-12 v6 统一库验收

- 来源仍为 BiGG 5,532 + ModelSEED 36,238，catalog 41,770；canonicalized union 为 25,339 reactions/15,972 metabolites。
- 严格守恒/方向质控通过 24,358 条；为保持富培养基 Growth 可行性恢复 48 条并标记 `rescue_for_growth`，operational 库为 24,406 reactions/15,774 metabolites。
- 同一化学实体的多个 ModelSEED 代谢物 ID 现在统一指向 BiGG canonical ID；ModelSEED ID 和等价反应仍完整保存在 catalog/provenance 中。
- PGAP/NCBI GenBank → v6 Reconstructor 成功完成，2,664 reactions、2,631 metabolites、5 GPR genes，MEMOTE 63.4667%。

CarveMe v6 的严格 BiGG 子集在该公开基因组上 MILP 不可行；同一 v6 统一 catalog 的 `reaction_library_mode=full` 路线完成了 NCBI/PGAP → CarveMe，耗时约 614.6 秒。该运行使用完整 BiGG 兼容子集，不改变 v6 的统一来源和质控记录，结果为 operational compatibility run。

## 2026-09-13 native v6 实测

- 默认 engine 已切换为 `native`；CarveMe/Reconstructor 仅保留兼容模式。
- biomass catalog：[prokaryotic_biomass_library](../data/prokaryotic_biomass_library/catalog.json)，仅使用项目内置的可复现 biomass 模板。
- FNA + NCBI GenBank → native v6：[native_ecoli_v1](../runs/native_ecoli_v1/manifest.json)，4300 proteins、1454 EC proteins、2944 候选反应、2 条 gap-fill、Growth 5.4367，MEMOTE 55.9346%。
- FAA + NCBI GenBank → native v6：[native_ecoli_faa_v1](../runs/native_ecoli_faa_v1/manifest.json)，4300 proteins、2944 候选反应、2 条 gap-fill，Growth 可行。
- native CarveMe/Reconstructor 适配均不调用对应建模器；旧路线仍可复现实验对照。






