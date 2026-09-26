# CLEAN 预测接入

主流程会先写出 `clean-input-no-ncbi.faa` 和 `clean-input-no-ncbi.tsv`，其中只包含没有 NCBI EC 证据的蛋白。提供 CLEAN 结果后，流程先按唯一蛋白去重，再保留排序前 30%（可用 `clean_top_fraction` 调整），最后通过反应库映射到 GPR；未映射的预测不会写入模型。

支持两种结果格式：

* 带表头的 CSV/TSV：`protein_id`/`Entry`、`ec_number`/`ec`、`confidence`（或 `distance`）。
* 官方无表头格式：`WP_xxx,EC:1.2.3.4/0.9974`。官方 GMM 分数按 confidence（越大越好）处理；原始 CLEAN 距离（通常大于 1）按 distance（越小越好）处理。距离不会被伪装成校准概率。

配置示例：

```json
{
  "clean_predictions": "runs/clean_predictions.tsv",
  "clean_top_fraction": 0.30
}
```

native 默认配置会发现仓库旁的 `<CLEAN_ROOT>/app` 和其 CLEAN
Python 环境，并在每次重建中对本次生成的 `clean-input-no-ncbi.faa` 运行一次预测。
预测原始表会复制到当前 run 目录并记录命令日志。没有可用 CLEAN 环境时仍会生成
CLEAN 输入文件，并把状态记为 `not_configured`；不会使用其他基因组或 iML1515 的
历史预测表。显式填写 `clean_predictions` 会跳过运行时预测并载入指定结果，适合可复现
的复核任务；也可用 `clean_runtime`、`clean_python` 和 `clean_timeout` 指定运行环境。

本机已部署官方 CLEAN：代码在 `<CLEAN_ROOT>`，环境为 `<CLEAN_PYTHON>`，权重在其 `app/data/pretrained/`。运行示例（输入文件放到 `app/data/inputs/`）：

```bash
cd <CLEAN_ROOT>/app
<CLEAN_PYTHON> CLEAN_infer_fasta.py \
  --fasta_data clean-input-no-ncbi
```

输出 `app/results/inputs/clean-input-no-ncbi_maxsep.csv` 后，将其作为 `clean_predictions` 传给重建配置即可。manifest 会分别记录 NCBI EC 数、CLEAN 候选数、选中数、映射数以及与 iML1515 的基因数差。

## 每个基因组单独运行 CLEAN

`clean_predictions` 只能用于生成它的那一份 FASTA。流程在载入结果时必须按
`protein_id`（并保留输入序列的 accession 版本号）做严格连接；不要把
`iml1515_gemagents_*.csv` 当作其他菌株的通用预测表。该文件中的 `NP_414xxx` 等
K-12 accession 属于 `GCF_000005845.2`，直接用于其他基因组会出现
`candidate_proteins>0` 但 `selected=0/mapped_genes=0`，从而静默丢失 CLEAN 证据。

每个输入基因组都使用自己的 `clean-input-no-ncbi.faa`。运行时模式会自动完成 CLEAN
调用；显式结果模式则要求用户把该 FASTA 的结果传入，不能把
`iml1515_gemagents_*.csv` 当作其他基因组的通用预测表。输入 FASTA 的 SHA-256、预测
文件的 SHA-256、输入/输出蛋白 ID 数和交集数都会写入 manifest。若显式预测文件与当前
输入的蛋白 ID 交集为空，流程会输出显式 `no_id_overlap`，跳过这份外部表并继续使用
当前基因组的其他注释证据；需要把交集为空视为硬错误时，显式设置
`clean_predictions_require_overlap: true`。映射后还要区分 `mapped` 与
`selected_unmapped`，后者表示 CLEAN 预测已选中但当前反应库没有形成可用 GPR，不能
计入模型基因数。

## 反应库 EC 补充

如果需要吸收 pear 的 `ec-code_annotation.csv` 或其他本地 EC 表，建库时显式传入
`ec_alias_source`。该表只按反应 ID/BiGG alias 补充 `ec-code`，不改方程、系数或方向；
建库会写出 `ec-aliases.tsv`，并在 manifest 中记录输入文件哈希、命中和未命中行数。
未命中的反应仍保留在审计表中，不能当作已建立的序列证据。

本次已生成隔离库 `data/reaction_library_v8_ec_alias_20260924`。如需重建同一快照，可运行：

```bash
conda run -n gemagents env PYTHONPATH=src python scripts/augment_reaction_library_ec.py \
  --library data/reaction_library_v8 \
  --ec-alias-source <EC_ALIAS_SOURCE> \
  --output data/reaction_library_v8_ec_alias_20260924
```

该库的四个 SBML 文件保持原库 41,571/48,197/22,014/28,640 个反应及原有方程和边界，
只增加 EC 注释；新库 manifest 的四个 SHA-256 均已核验。

批处理可使用 `--clean-predictions-directory DIR`，目录内按 `GCF_...csv/tsv/txt` 保存每株结果；
也可逐株传 `--clean-predictions FILE`。未提供这两个参数时，批处理不会自动套用仓库旁的 MG1655 表。
