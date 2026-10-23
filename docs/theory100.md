# 新管线100条理论出题尝试

用户要求在前置自洽与程序反代管线下再试100题，并积极隐藏推理、尝试删减条件。配置为[theory100.toml](../configs/theory100.toml)，计划为[theory100.plan.json](../configs/papers/theory100.plan.json)，设计与提示见[question-design](question-design.md)。

复用已核实、固定版本的10篇顶刊理论论文，数学50条、物理50条，每篇10种构造方向。物理材料仍偏理论电磁学，不代表物理全学科。100条是计划尝试，不预称100道成功题；同论文样本相关，不当作100个独立随机样本。换数字不计独立交付，最终留存仍执行语义/material_key/谱系去重。

新批次开启require_solution_design和preverify_blueprint：先生成来源结论、完整推导和逆向设计，再审参考蓝图，通过后才构造题面。之后执行综合审查、盲推导、自洽审查、验算公式对齐和精确反代。可以让参数范围/结构/分支都成为待推导对象；删减导致矛盾或非唯一的版本先修正确性，不进入难度演化。

每条最多两次难度改写，每份作者草案最多一次内部修复；作者修复不增加出题线数。首4次全对则加难，有有效错答则补到16次；其余验收口径沿用20条协议。目标模型GLM5.2，无工具、600秒、不发输出token上限；HLE判分为gpt-6-luna medium。

并行8条出题线，共享16个模型调用槽；解题最多14个，为判分/质检保留容量。请求上限12,000次HTTP尝试，已返回用量软阈值450美元，为原20条90美元配置的5倍；未知和在途费用仍可能存在，这不是账单硬上限。原20条实际约¥200只作量级参考，不能作为本轮最终成本承诺。

运行冻结代码、提示、配置、价格、输入清单及独立PostgreSQL数据库；不覆盖20条历史快照。统计首轮与有限迭代留存、蓝图拒绝、题面/金标拒绝、公式翻译问题、反代失败/未覆盖、接口故障、完整难度分布、去重后留存和全费用。预算/运行未完成不称验收通过。

已启动run-id为theory100-verified-inverse-20261024，冻结代码87a212b，个人VM独立数据库rst_hle_theory100_20261024。当前是运行记录，不是100道完成题。前置门禁的真实回放及误拒边界见[回放结果](consistency-replay-results-2026-10-24.md)。

```bash
uv run python scripts/run_question_experiment.py \
  --config configs/theory100.toml --plan configs/papers/theory100.plan.json \
  --rates work/private/gateway-prices.json --run-id theory100-verified-inverse-20261024
```
