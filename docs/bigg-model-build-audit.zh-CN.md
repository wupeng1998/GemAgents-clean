# BiGG 批量建模审查（2026-09-24）

## 结论

历史批次的主要损失发生在证据连接，而不是反应库只有几百个反应：

1. 14 个 manifest 都指向同一个 `iml1515_gemagents_20260922_maxsep.csv`。该表含
   3,152 个 K-12 `NP_/YP_` accession，只有 `GCF_000005845.2` 有 3,152 个 ID
   交集；其他批次交集为 0，所以 CLEAN 选中数全部为 0。
2. 这些批次使用 `annotation=ncbi-hmm`。该路线是 PGAP HMM equivalog 子集，不是
   完整 PGAP 注释；历史结果的 NCBI EC 蛋白数为 317–1,146，不能直接当成全基因组
   的功能注释总数。
3. `reaction_library_v8` 有 41,571 个严格活跃反应（full union 48,197 个），但
   历史模型映射中大多数 EC 是多反应候选：例如 `GCF_000006925.2` 的 3,441 个
   候选反应中 3,344 个带歧义，616 个注释基因只有歧义映射，另有 275 个 EC 基因
   没有任何反应。该漏斗解释了“NCBI 有 EC、最终模型基因却少”的差距。
4. 所有历史批次启用了 `reference_scaffold=true`；模型中的公共参考支架反应不能
   计为该菌株的序列证据。`GCF_000010485.1` 还停在 `running`，缺少完整模型和质控产物。

## 已完成的修复

- EC 统一规范化支持 `EC:` 前缀、部分 EC（`-`/`*`）和通配匹配，已用于 NCBI、
  CLEAN、ModelSEED 和反应证据映射。
- CLEAN 批处理新增逐株文件解析：`--clean-predictions` 或
  `--clean-predictions-directory`。未提供逐株文件时不再自动套用仓库旁的 MG1655
  预测表；ID 无交集会以 `no_id_overlap` 失败并写入哈希与交集统计。
- 反应库支持显式 EC alias 表，只增加反应注释，不改方程、系数、边界或方向；未命中
  和边界反应会留在 `ec-aliases.tsv` 审计表中。
- MQC 风格质控新增无碳源净生成，以及 ATP/GTP/CTP/UTP/ITP、NAD(P)H、FADH2、
  FMNH2、Q8H2 的封闭边界循环探针。

## 新反应库

已生成隔离库 `data/reaction_library_v8_ec_alias_20260924`，来源为 pear
`Protokaryon/ec-code_annotation.csv`：15,095 行，5,312 行命中，9,778 行未命中，
另有 5 行边界反应被排除。严格库的唯一 EC 类从 4,720 增至 4,852；以历史
`GCF_000006925.2` 注释复算时，无反应基因从 198 降至 106（`GCF_000010385.1`
从 215 降至 109），但歧义会增加，因此没有自动把多反应 EC 强行变成唯一 GPR。

四个 SBML 文件的哈希均与新库 manifest 一致，并逐反应核对了原库的化学计量和边界；
不一致数为 0。新库可直接作为 `reaction_library` 输入 native 重建。

## 推荐重跑顺序

先对每个 `builds/GCF_*.1/.2` 的 `clean-input-no-ncbi.faa` 单独运行 CLEAN，把结果按
`GCF_*.csv` 放入一个目录，再执行：

```bash
conda run -n gemagents env PYTHONPATH=src python scripts/run_bigg_gemagents_batch.py \
  --mapping bigg_model/preparation/mapping.explicit.auto.json \
  --biomass-library data/prokaryotic_biomass_library_v2 \
  --reaction-library data/reaction_library_v8_ec_alias_20260924 \
  --hmm-directory data/ncbi_hmm \
  --output-directory bigg_model \
  --clean-predictions-directory /path/to/per_genome_clean
```

重跑后优先查看每个 manifest 的 `clean_prediction.status`、`candidate_id_overlap`、
`mapping_audit` 和 `evidence_reconciliation`，再解释最终模型基因数；历史目录不应
被覆盖。
