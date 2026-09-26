# MQC 与 Protokaryon 集成

GemAgents 保留自己的质量证书和质量阈值；外部 MQC 作为用户显式调用的独立复核器。MQC 输出、原始日志和基于 MQC 输出模型的 biomass FBA/pFBA 会写入同一个新目录，不覆盖输入模型或 GemAgents 的 `quality.json`。

```bash
conda run -n gemagents env PYTHONPATH=src \
  python scripts/run_mqc_quality.py \
  --model bigg_model/builds/GCF_000006925.2/model.xml \
  --mqc-root <MQC_ROOT> \
  --output-dir bigg_model/builds/GCF_000006925.2/mqc-output
```

如果当前 Python 环境没有 MQC 的可选依赖，可以显式指定一个已经安装了 `cobra`、`cplex`、`d3flux` 和 `openpyxl` 的解释器：

```bash
--python /path/to/python
```

`mqc-biomass-simulation.json` 记录输入模型和 MQC 输出模型的 SHA-256、MQC 各检查分数、目标反应、FBA biomass、pFBA biomass 和总绝对通量。MQC 的 score 只描述 MQC 检查范围；它不会被转换成 GemAgents 的 `declared_checks_passed`，也不会被当作生物学验证。

建库时，如果工作区旁存在用户授权的 Protokaryon 快照
`<EC_ALIAS_SOURCE>`，GemAgents 会在准备反应库时自动发现它。该表只补充反应的 EC 注释，并写出 `ec-aliases.tsv` 和输入哈希；未命中的行、边界反应和歧义 EC 都保留在审计记录中，不会直接生成序列证据。

存在完整 manifest 的 `data/reaction_library_v8_ec_alias_20260924` 时，默认反应库会优先使用
这个公开路径；策略受限的 `data/reaction_library_v8_pear_ec_20260924` 即使存在也不会被默认选择。
缺失时回退到 `reaction_library_v8`、`v7` 或 `v6`。这让已有检出库保持可用，也让包含
Protokaryon EC 别名的本地快照能够被新建模任务复用。

## Protokaryon 文件层级审计

`Protokaryon/big_model.xml` 不能作为化学计量真值：它的默认目标是
`DM_ppi_c`（demand 目标），并且存在批量放大的计量系数。审计记录见
`artifacts/mqc/protokaryon-big-model-reference-audit.json`：27,867 个反应中有
3,946 个反应含有绝对值不小于 100 的系数，与同目录候选模型比较可见 600×、300× 和
101× 的整反应缩放。`path_to_your_new_model.xml` 也含有真核/聚合物层的高系数反应，
因此同样不能未经逐反应审查就并入严格原核库。

运行 `scripts/audit_protokaryon_reference.py` 会检查计量系数、默认目标和可选的同 ID
反应缩放；失败返回码为 2。Protokaryon 在 GemAgents 中只作为 EC 注释别名来源，
不自动导入其反应方程、边界、GPR 或目标函数。
