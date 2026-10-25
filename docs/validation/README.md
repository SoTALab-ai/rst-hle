# 历史验证与实验结果

以下文件记录各自冻结代码和当时协议，不能代替当前版本验证，也不因后续规则改变而追溯重分类。

| 批次 | 结果入口 |
|---|---|
| 首次个人VM链路 | [初始验证](initial-vm-validation.md) |
| 原HLE八领域校准 | [校准结果](calibration-results-2026-10-23.md) |
| 首次数学/物理20条 | [20条失败归因](theory-pilot-results-2026-10-23.md) |
| 前置自洽回放 | [固定样本复核](consistency-replay-results-2026-10-24.md) |
| 100条逆向出题 | [100条结果](theory100-results-2026-10-24.md) |
| 最难步骤挖空10条 | [方案与阶段记录](theory10-masked-2026-10-24.md)、[结果](theory10-results-2026-10-24.md) |
| verifier真实代表例 | [代表例](verifier-probe-2026-10-24.md) |
| verifier20条 | [完整记录](theory20-verifier-2026-10-24.md)、[脱敏统计](theory20-verifier-results-2026-10-24.json) |
| PDF首20篇采集 | [统计](pdf-paper-library-results-2026-10-24.json) |
| PDF20篇旧MinerU审查/试产 | [阶段统计](pdf20-material-trial-results-2026-10-25.json)、[历史识别协议与归因](../pdf-recognition.md) |

新批次的结果在完成后追加，运行中和未完成状态明确标注。

2026-10-25新增[直接论文库的真实验证与200篇批次状态](paper-library-direct-reader-2026-10-25.md)。

2026-10-25新增[首20份试产与50份读取上限](library20-reader-cap-2026-10-25.md)，记录最终0道留存、50份停止及累计费用。

2026-10-25新增[9篇规划失败与6道质检阻断的回放审计](library20-rejection-audit-2026-10-25.md)，逐项区分入口误杀、题目合同缺陷和验证未覆盖；原难度答卷未重跑。

2026-10-25用户最新口径的[来源启发模式与25条入口回放](../source-fidelity.md)、[脱敏统计](library20-source-advisory-2026-10-25.json)：来源对齐降为提示，新增科学通过0、GLM调用0。

2026-10-25新增[锁定私有金标解释冲突的真实诊断](library20-gold-explanation-probe-2026-10-25.json)：两次6Luna蓝图调用，无GLM调用，不是完整质检或难度验收。

2026-10-25[首20份来源启发续产](library20-continuation-2026-10-25.md)已结束，状态`completed_with_errors`；25个候选、3个前置质检通过、最终留存0，完整16次物理题12/16偏简单拒绝。含独立诊断的累计API约$13.8715，旧答卷未重跑；[脱敏统计](library20-continuation-results-2026-10-25.json)。

2026-10-25新增[逐题首测与补测并行](streaming-evaluation-2026-10-25.md)：独立作者/评测池、缺失样本恢复、错误隔离和并发去重；既有在途快照保持原调度。

2026-10-25新增[作者与前置质检损耗审计](author-qc-yield-audit-2026-10-25.md)：逐条归因金标表达冲突、对照生成与验算覆盖，进入verifier后冻结题面/金标/完整推导，阻止外层作者重写。

2026-10-25新增[首题桥梁挖空与验算分流验证](bridge-masking-preflight-2026-10-25.md)：四类初始目标、依赖回溯和定向证书修复；真实PG356测试及6次真实6Luna组件调用，不代表难度或留存率提升。含此前诊断的本批已知API累计$13.90634528。
