# 本机 PGAP 部署与运行

使用独立 WSL2 发行版 `GemAgents-PGAP`，Ubuntu 24.04.4，内部安装 Docker Engine，
由 Windows 中的 GemAgents 自动调用完整 PGAP。没有使用 MQC 或 pear。
PGAP 固定为 `2026-06-18.build8602`，不在每次运行时自动切换最新版本。

## 已落地的系统变更

- 安装带有效微软签名的 WSL 2.7.13，并启用 VirtualMachinePlatform 和 WSL 系统组件。
- 从 Canonical 官方镜像导入 `GemAgents-PGAP`，镜像按微软发行版清单的 SHA256 校验。
- WSL 虚拟磁盘位于 `data/pgap/wsl/`，PGAP 工作目录位于 Linux `/opt/gemagents-pgap`。
- 安装 Docker Engine、Python 3、curl 和 aria2；未安装 Docker Desktop。
- 本机原有 Windows 代理位于 localhost。为解决 NAT 模式无法访问该代理的问题，
  新建 `C:/Users/<username>/.wslconfig`，设置 mirrored networking、DNS tunneling、autoProxy。
  这份文件原先不存在；未覆盖已有 WSL 配置，未修改 Windows 代理本身。
- Docker daemon 使用 WSL 继承的代理，其配置保存在该独立发行版的 `/etc/docker/daemon.json`。

Windows 安装器返回了建议重启标记，但实际 WSL2、systemd、Ubuntu 和 Docker 已经启动成功。
部署过程未自动重启 Windows。系统安装日志为
`data/pgap/deployment/windows-bootstrap.log` 和 `windows-prerequisites.json`。
网络配置依据 [Microsoft WSL 网络文档](https://learn.microsoft.com/en-us/windows/wsl/networking)，
Docker daemon 代理依据 [Docker 官方文档](https://docs.docker.com/engine/daemon/proxy/)。

## 安装与续传入口

在 `C:/jupyter/GemAgents` 执行：

```powershell
.\.venv-research\Scripts\python.exe -m GemAgents --setup-pgap
```

该命令会检查 WSL、导入缺失的独立发行版、安装所需软件、先拉取 PGAP 镜像，随后下载并
安装参考数据。主参考压缩包约 23.9 GB，使用 8 路连接和 aria2 控制文件支持续传。
完整压缩包才会原子更名为官方安装器识别的文件名；随后由官方 PGAP 安装器解压和完成安装。
过程中若断网，可在原目录重跑此命令，保留 `.download` 和 `.aria2` 文件。
不要同时运行两次安装命令。

首次部署如果需要 Windows 管理员权限，命令会先准备签名校验脚本并返回
`requires_windows_administrator`。管理员执行生成的 `windows-bootstrap.ps1`，
如系统需要重启则保存工作并重启，再运行上述命令。不会将尚未运行的容器登记为就绪。
Linux 原生安装分支针对使用 apt 的 Ubuntu/Debian，需要 root；其他发行版可自行安装 PGAP
并通过 `--pgap-script` 使用现有运行环境。

进度与错误：

- `data/pgap/deployment/setup-state.json`：安装阶段和状态。
- `data/pgap/deployment/linux-install.log`：软件、容器和官方安装器日志。
- `data/pgap/deployment/database-download.log`：本次单独启动的数据库续传日志。
- `data/pgap/runtime.json`：全部安装结束后生成的运行配置。

验证实际就绪状态：

```powershell
.\.venv-research\Scripts\python.exe -m GemAgents --check-pgap
```

只有 Docker daemon 可达、PGAP 数据版本匹配、官方完成标志存在且对应镜像可用时才返回
`ready: true`。尚未就绪时退出码为 2；发生安装命令错误则保留日志并返回失败。
该检查证明基础运行环境可用，完整注释还应通过真实基因组验证。

## FAA/FNA 流程中的使用

安装完成后，从原始 FNA 开始：

```powershell
.\.venv-research\Scripts\python.exe -m GemAgents --reconstruct sample.fna --annotation pgap --organism "Escherichia coli" --output-dir runs/sample_pgap
```

也可省略 `--annotation pgap`：没有注释文件时，FNA 的 `auto` 路线默认使用 PGAP。
自动加载工作区的 `data/pgap/runtime.json`，不必每次填写 launcher 或 WSL 路径。
FAA 仍使用已有的 NCBI HMM 或注释导入路线；它不是完整 PGAP 的基因组输入。

本机默认 PGAP 使用 **2 CPU、6 GB 容器内存**，适配约 16 GB 的 Windows 主机。
可用 `--cpus`、`--pgap-memory` 覆盖；HMM 路线仍默认 4 CPU。
超时默认 7,200 秒，可在重建 JSON 中设置 `timeout`，大型基因组需要按实际资源调整。
官方硬件要求见 [PGAP Quick Start](https://github.com/ncbi/pgap/wiki/Quick-Start)。

Windows 输入先校验并转换为标准 FNA（支持 `.fna.gz`），复制到 Linux ext4 中运行。
PGAP 容器只使用 Linux 路径进行 bind mount；结束后将输出复制回当前运行目录的 `pgap/`。
然后检查 `annot.gbk` 与输入序列是否一致，继续既有反应映射、模型构建、质控和 SBML 导出。
Windows 和 Linux 之间使用参数列表传递路径，不将物种名、空格路径或输入文件名拼进 shell。

每次运行保存 `pgap-command.json`、`pgap-execution.json` 和 `pgap.log`。
失败时也尝试带回 PGAP 的诊断文件；超时只停止该次运行以 UUID 命名的容器。
Linux 工作目录会保留，位置记录在执行报告中，便于检查复制失败或失败步骤。
不使用 `--ignore-all-errors`；不把有残留注释文件的失败运行当作成功。

## 本次验收记录

系统组件、WSL、Ubuntu、Docker、代理和完整参考数据库已实测。
2026-09-10 安装入口重跑成功，`setup-state.json` 为 `ready`，就绪检查无错误；
重跑复用了已下载的数据和镜像。镜像摘要为
`sha256:1f30946971f7285a726f9e19550427dcdb608ce924fe68233d677bf66f2c6dd4`，
已连同 launcher SHA256 写入 `data/pgap/runtime.json`。
公开 NC_000908.2 基因组的完整 PGAP 注释已成功完成，结果目录为 `runs/mgen_full_pgap_v1/`。
从流程开始至进入模型构建约 825.2 秒，包含 PGAP、文件复制、序列校验和反应映射，
不包含首次数据库下载。得到 504 条蛋白，其中 179 条带 EC 编号；CDS 使用遗传密码表 4。
GenBank 中的基因组序列与原始 FNA 完全一致；GBK、FAA、FNA、GFF 和 CheckM 输出已回传。
CheckM 报告 completeness 98.00、contamination 0.00；这些是本次标记基因估计，
不代表代谢模型准确率。文件校验和与序列核对结果保存在 `pgap-validation.json`。
容器日志曾出现 `BlockingIOError` 写入提示，后续步骤继续成功，launcher 最终明确返回
`PGAP completed successfully.`；已保留容器日志和诊断提示。
模块回归测试覆盖 WSL 输出编码、代理提示与路径隔离、gzip 输入、含空格路径、容器超时清理、
复制失败、数据库版本不匹配等情况。93 项项目与内核测试通过，Ruff 通过。
模拟调用测试不算作真实 PGAP 注释通过。

完整下游链路也已完成：PGAP → EC 候选映射 → CarveMe → CER 审计 → SBML，
总耗时 1430.7 秒，不含部署下载。输出模型含 367 个反应、320 个代谢物、23 个 GPR 基因。
重新载入 SBML 后，哈希、模型计数、培养基摄取上限、生长可解性和无遗留探针检查均通过。
五种声明的闭合探针通过，但保留 41 条静态守恒问题，结果为 `completed_with_findings`。
这次完成了工程集成验收，模型仍需生物学验证，未声称已全面修复或提高注释准确率。

- [注释文件与序列验证](../runs/mgen_full_pgap_v1/pgap-validation.json)
- [完整运行记录](../runs/mgen_full_pgap_v1/manifest.json)
- [输出模型](../runs/mgen_full_pgap_v1/model.xml)
- [质控报告](../runs/mgen_full_pgap_v1/quality.json)
- [导出复查](../experiments/counterexample_repair/results/pgap_pipeline_summary.json)

## 2026-09-12 反应库与 MEMOTE 更新

PGAP 部署保持不变；新增公开 MEMOTE 0.17.0 评分和 BiGG/ModelSEED v3 反应库。CarveMe 使用 BiGG 子库以避免 25k 反应联合 MILP 的不可接受求解时间，Reconstructor 使用完整 union。两种路径都在 manifest 中记录来源和 SHA256，未使用 pear 或 MQC。

