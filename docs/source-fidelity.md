# 论文启发与题目自洽分开验收

首20份在新协议下继续生产、复用旧答卷与待审种子的方式见[续产](continuation.md)。

用户2026-10-25调整口径：论文用于发现机制和构造材料，题目不必逐字或逐结论复现PDF。新论文库管线默认使用[新配置](../configs/library-source-inspired.toml)，`source_fidelity="advisory"`；旧配置缺省仍为`strict`，历史run不追溯改判。

| 检查 | 新口径 |
|---|---|
| 来源原件、版本/哈希、元数据登记、材料页完整性 | 保留结构检查与溯源 |
| 引文逐字匹配、改造题与原论文结论一致 | 记录提示，不单独拒题 |
| 依据论文机制构造新模型/新结论 | 允许，标为构造，不冒充原文定理 |
| 题面自足、定义/参数域、完整推导、golden与实际输出一致 | 独立审查的硬门槛 |
| 金标/选定模型前提锁定、答案反代、verifier合法/非法对照 | 保留，不能用来源放宽绕过 |
| 原材料没给、题面也没定义却必需的实测数据或前提 | 仍是题面缺信息；明确的假想模型假设可以自行规定 |

仍然从候选答案出发，写完整参考证明，独立judge按步骤返回问题，修好后挖空必要的困难步骤，再检查实际题面与答案的一致性。`known_result`字段为兼容旧存储保留：在新模式中是**锁定的构造提案**，不是“论文已证明”的凭证。其答案、目标和模型前提一旦选定便不在修复/降难时静默替换；错误提案另开版本。

候选和导出保存`source_fidelity`与`source_notes`，蓝图审查另保存`source_grounded/source_notes`。新模式下`source_grounded=false`本身不拒绝；数学/物理正确性、完整推导任一未通过仍阻断。来源提示与错误引文不能被写成科学反例，也不能把暂未完成的科学检查写成通过。

verifier的定位格式与数学验收规则分开。程序将受限排版/语法差异投影回题面原句，保留原spec、哈希和修改位置；不改规则、条件或golden。格式修复独立于最多两轮数学对抗修改。随后继续用golden、等价合法答案、错误近邻检查误杀、漏检与固定HLE一致性。题面要求证明但golden只有结论等真实输出合同缺陷仍需修题面；不能靠宽松判分掩盖。

旧候选不能只换一个模式标记就复用所有质量检查：模式进入冻结指纹、蓝图、盲解/自洽及verifier上下文；运行配置与候选模式不一致时要求新版本。文件哈希和原请求封印照常校验，不重新购买原GLM答卷。

## 本轮离线回放

同一批首20份冻结材料，之前9篇规划失败共有25条提案。严格的受限排版修复恢复23条；按新口径，另2条引文改写降为来源提示，**25条均可进入下一阶段**。这不是25道独立审查通过或25道合格题，缺失的多题独立性、完整证明与后续质检没有补造。

只读回放身份为个人VM的`library20-source-advisory-20261025-v1`，原证据清单与[前一轮审计](validation/library20-rejection-audit-2026-10-25.md)相同。源码/提示/新配置在独立快照中冻结，原批次与50份停止状态保持。新增真实模型/GLM调用0，新增API费用$0；没有重写原12份GLM答卷或历史0道留存。

新提示未进行新增真实模型验收，回放只验证已知入口和格式问题及保留硬门槛，不能证明真实新构造一定通过。数据库测试使用专用`rst_hle_rejection_audit_20261025_test`，不连接试产业务库。

个人VM完整测试310 passed，ruff和doctor通过（模型mock、真实PG）。新回放报告摘要为`c247417edf9cf3a0f07b0bac1f933d0d5f794b7bfa11e67eb6e07bdbffe745bb`；代码/测试快照归档SHA256为`dff53f30abc2251b3a4c0c68a546bba1aafd16a21e7dfaf37b101273c5dcb939`。脱敏统计见[回放摘要](validation/library20-source-advisory-2026-10-25.json)。

```bash
uv run python scripts/replay_rejections.py \
  --evidence PRIVATE_EXPORT_DIRECTORY \
  --manifest-sha256 3c9c022ceef7c582fc259cf97d3f43d59571e78bbab39d092656595fba6c7bf7 \
  --source-fidelity advisory --run-id NEW_REPLAY_ID --output PRIVATE_REPLAY_REPORT.json
```

报告包含私有材料定位，不提交Git。后续新试产命令显式使用`--config configs/library-source-inspired.toml`及新run-id，仍先4次筛选，首4次全对拒绝，有错再补16次，只对过难题降难。
