# 文档导航

文档按职责组织，参考ClawGym的入口、架构、验证方法与历史记录分工；不复用它的业务环境。

| 需要处理的事情 | 阅读入口 |
|---|---|
| 模块归属、阶段边界、当前实现 | [合成架构](synthesis-architecture.md) |
| 环境配置、测试、续跑与真实验证 | [本地与个人VM验证](local-validation.md) |
| 论文下载、正文与补充材料 | [PDF材料采集](pdf-paper-library.md) |
| 预先批量读取、数据库查询 | [直接读取论文库](paper-library-ingestion.md) |
| 历史MinerU及逐页审查协议 | [PDF识别历史协议](pdf-recognition.md) |
| 数据库、迁移、备份 | [存储部署](database.md) |
| 锁定答案、重建推导、挖空难点 | [出题设计](question-design.md)、[挖空合同](bottleneck-masking.md) |
| 科学正确性、反代、判分争议 | [自洽检查](consistency.md)、[verifier对抗](verifier-adversary.md)、[判分复核](judge-context-recheck.md) |
| 难度测量与降难 | [理论实验协议](theory-experiment.md)、[原始HLE校准](calibration.md) |
| 数据格式、恢复与验收 | [数据契约](data-contract.md)、[恢复](resilience.md)、[验收](acceptance.md) |
| 已执行批次的真实结果 | [历史验证索引](validation/README.md) |
| 方法依据与有效性限制 | [RST评估](rst-assessment.md)、[难题构造研究](harder-question-design-2026-10-24.md) |

持续生效的合同留在上述专题文档；批次数字、失败归因和特定代码版本保存在`validation/`。历史结果不因为当前规则变化而重新分类。
