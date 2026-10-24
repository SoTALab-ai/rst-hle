# 本地与个人VM验证

## 环境与入口

Python3.11+、uv；PDF直接读取还需要Poppler的`pdftoppm`。密钥只由个人环境变量或忽略的环境文件提供。个人VM使用独立PostgreSQL和对象目录，不连接ClawGym业务资源。

```bash
uv sync --frozen --python 3.12
uv run ruff check src tests scripts
uv run pytest -q
uv run diamond doctor
```

没有`DIAMOND_TEST_DATABASE_URL`时PG测试会跳过，不能据此宣称真实数据库已验证。真实测试库名必须以`_test`结尾；不要把试产连接字符串当测试连接。

```bash
uv run pytest -q tests/test_postgres.py tests/test_paper_ingest.py
```

数据库初始化/升级先在所选独立库明确执行`uv run diamond db-init`。应用启动只核对结构和迁移校验和；禁止修改旧SQL文件。部署方法见[数据库](database.md)。

## 各阶段验证

| 改动 | 验证 |
|---|---|
| 文档组织 | 链接、命令、历史路径引用及`git diff --check` |
| 论文下载 | 真实开放仓储下载、DOI/哈希、失败与候选不足、缓存恢复 |
| 直接PDF读取 | 真实PDF与真实模型、物理页覆盖、公式/正文抽样对照、PG和对象写回；抽样不是全量正确性认证 |
| 入库恢复 | 已付费回答不重购、损坏缓存拒绝、配置/代码变化换版本、缺页与未读清不冒充完整 |
| 科学质检 | 原有反代与verifier对抗测试、代表性真实模型链路 |
| 规模实验 | 冻结清单、个人VM身份、全体目标的阶段结果/失败/成本与未覆盖部分 |

先验证最早受影响行为。没有新改动或风险，不反复重跑完整旧批次。PDF入口与配置见[直接论文库](paper-library-ingestion.md)，出题实验见[协议](theory-experiment.md)。

## 个人VM执行

`scripts/vm_run.py`通过加密SSH标准输入转发已有模型环境变量，不放入argv。远端`work/private/runtime.env`保存本项目自己的数据库配置。例：

```bash
uv run python scripts/vm_run.py --host YOUR_PERSONAL_VM \
  --directory '~/Developer/rst-hle' --runtime-env work/private/runtime.env \
  -- python scripts/ingest_paper_library.py run \
  --plan work/library-plan.json --run-id read-RUN-ID \
  --directory work/read/RUN-ID --objects work/library-objects \
  --rates work/private/prices.json --pages-per-call 2
```

正式批次使用独立代码快照，记录commit及内容哈希；禁止将本地新源码覆盖到在途快照。需要断开SSH继续运行时可用启动器的`--detach --log work/read.log`，它只表示已启动，不能作为完成证据。

## 恢复与观察

观察所选批次直到终态。下载报告、读取`manifest.json/report.json`和PG状态相互核对。未完成时记录当前数量，不用手工修改页面状态、补造通过标志或无收据重买成功回答。

同时备份数据库与内容寻址库并在独立`_test`库验证恢复。原论文、完整转录与轨迹保持私有运行产物，公开PR只包含代码、脱敏配置、文档和统计。历史真实验证见[索引](validation/README.md)。
