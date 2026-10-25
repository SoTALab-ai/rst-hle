# 读取到50份停止，首20份立即试产

2026-10-25当前批次改为：用已完整解析的首20份独立材料立即规划每篇1–5条不同推理结构；材料库达到50份完整材料后停止新增读取请求，保存已发请求的响应再结束读取。首20份试产使用独立run与个人PG库，替代原先等待50份才启动的未付费任务。

## 冻结批次的兼容停止

旧读取器已创建全部页面任务，直接终止会取消在途模型请求。为避免热改其源码/提示/解析身份，使用独立控制run，显式安装`deployment/control/001_reader_limit.sql`到个人论文库的`rst_hle_control` schema。控制面有自己的迁移校验和，不改旧`diamond.schema_migrations`链；旧冻结读取器和已启动的20份试产仍可核对自己的迁移。

门禁只匹配指定PDF读取run，在持久化HTTP预约计数增加前，核对所有正文/已关联补充的页数、未读清状态和可读正文。达到50份后拒绝该读取run的新HTTP预约，其他run仍可调用。未获预约不是接口失败，也没有模型费用；旧程序留下的`BudgetExceeded`事件单独解释为用户控制的未发送工作。

控制器等待所有已预约HTTP均有落盘收据，200响应另需完整逻辑响应已写入PG；随后核对原进程的PID、命令哈希、工作目录和进程组，才结束尚未发请求的排队任务。历史代码/输入/收据不变，状态记`stopped_at_material_limit`，另写控制报告、首50份冻结清单和原费用。已经取得的其余元数据/原PDF不是已完成解析。

50是停止新增请求的条件。已经在途的请求允许自然结束，可能多完成少量材料；额外数量与费用必须如实报告，结果保留，不能删除收据来凑50。该程序用于本项目个人Linux VM，不使用Harbor或ClawGym业务资源。

## 显式使用

```bash
# 先指向自己的论文PG库和内容库，不指向出题库。
uv run python scripts/control_paper_reader.py init --objects work/library-objects
uv run python scripts/control_paper_reader.py run \
  --objects work/library-objects --policy work/private/reader-limit.json \
  --directory work/control/NEW-CONTROL-ID

# 首20份出题使用另一个个人PG库；论文库环境变量只读。
uv run python scripts/run_library_question_campaign.py \
  --library-run-id ORIGINAL-READ-RUN --materials 20 \
  --run-prefix NEW-QUESTION-RUN --directory work/trial/NEW-QUESTION-RUN \
  --rates work/private/prices.json --config configs/library50-questions.toml
```

策略采用严格`ReaderLimit`：`id`、`target_run_id`、`material_limit`、`worker_pid`、`worker_command_sha256`、`snapshot`、`requests_directory`、`reason`。从真实启动收据取得进程身份，不猜测PID。策略、控制实现哈希和原读取指纹冻结；变更须新控制身份。上面配置文件名保留历史名称，不强制50份出题：材料数由`--materials`指定。

原50份等待任务退出前，先确认其没有规划run、模型请求或答卷；如果已经开始付费工作，不能用“未启动”理由直接取消。当前20份材料的规划、推导、独立审查、题面、反代、verifier及GLM4→16规则保持原合同，不启动额外的50份出题批次。

## 费用与验证

累计台账同时包含旧下载/探针、当前读取、20份规划以及出题分片。控制器停止后将尚未索引的失败HTTP收据写回原支付run，未知usage不记0；停止时未发请求的页面不算付费失败。费用收尾任务同时观察原读取、控制器和20份试产，全部结束后退出。

针对性测试覆盖独立迁移幂等/校验和、完整材料才停止、目标run预约原子拒绝且不扣预算、其他run继续、在途未返回不能停止、成功响应未持久化不能停止、失败收据保留和控制后首批材料冻结。个人VM专用PG测试272 passed；模型在程序测试中为mock。真实达到50后的排空结果另列，不能由测试宣称已停止。
