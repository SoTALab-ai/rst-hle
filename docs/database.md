# 数据库：参考 ClawGym，先部署个人 VM 的独立实例

已查阅用户既有 ClawGym 本地代码快照 `ba7a7a990ef873d29771293bd0f56bc0bb014237` 的存储架构、锁约束、schema 和 dispatch repository。借用其职责分离，不复制其业务数据、云资源配置或生产密钥。

| 事项 | ClawGym 现有方案 | 本项目首版 |
|---|---|---|
| 状态真源 | Cloud SQL PostgreSQL | 个人 VM 独立 PostgreSQL 17 容器 |
| 大文件/阶段产物 | GCS，数据库记录 URI、SHA-256、大小 | 独立内容寻址文件产物库，同样记录 key、SHA-256、大小；云 adapter 待接 |
| 核心记录 | runs、tasks、stage executions/attempts、artifacts、exports | runs、candidates、sources、model_requests、qc_samples、stage_events、exports |
| 调度 | Management 写队列，runner lease 领取 | jobs 表与领取/过期/令牌隔离逻辑已实现，管理 API/长期 worker 尚未接入 |
| schema | 显式升级，启动只校验 | `db-init` 显式初始化，启动只校验版本和迁移文件哈希 |
| 写入顺序 | 先保存对象，再写元数据 | 同样顺序；内容不符/文件损坏时拒绝恢复 |
| 模型调用 | 不占用数据库事务 | 短事务预约预算与写回；模型等待期间不持有事务 |
| 并发保护 | 队列租约/连接池/领域锁 | PostgreSQL 全流程 advisory writer lock，防止配额竞态；内部模型并发 |

当前默认单写入管线，数据库支持跨进程互斥。jobs repository 具备 `FOR UPDATE SKIP LOCKED`、过期重领和令牌隔离；它是后续多 worker 调度基础，**尚不是已接通的分布式执行系统**。暂不引入 Redis 或独立消息系统。

```mermaid
erDiagram
    RUNS ||--o{ CANDIDATES : contains
    RUNS ||--o{ MODEL_REQUESTS : records
    RUNS ||--o{ STAGE_EVENTS : records
    CANDIDATES ||--o{ CANDIDATE_SOURCES : grounded_in
    SOURCES ||--o{ CANDIDATE_SOURCES : supports
    CANDIDATES ||--o{ QC_SAMPLES : has_16
    MODEL_REQUESTS ||--o{ QC_SAMPLES : solver_and_judge
    CANDIDATES ||--o{ CANDIDATES : parent
```

`sources` 保存来源标题、地址、全文哈希和正文对象收据；`candidates` 保存分类、父子谱系、material_key、状态和 bundle 收据；`qc_samples` 唯一约束 `(candidate_id, sample_id)`，并关联 solver/judge 请求。`model_requests` 保存模型、usage 和原始请求/响应对象收据。`quotas` 保存不可混淆的配额清单版本；`exports` 保存导出 manifest。

两个部分唯一索引保证 accepted 题中 root 和 material_key 只计一次；这比纯应用内计数更可靠。首版仍用全流程写锁串行执行配额保留，未宣称支持任意多写入生产。调整每谱系计数策略时必须先更新数据库约束和相应测试。

## 首次启动

在本项目独立 VM 目录中创建 `deployment/.env`，权限设为 600，填入随机长密码和未占用端口。文件被 Git 忽略。

```text
POSTGRES_PASSWORD=<unique-random-password>
POSTGRES_PORT=55439
```

```bash
docker compose --env-file deployment/.env -f deployment/compose.yaml up -d
export DIAMOND_DATABASE_URL=postgresql://rst_hle:...@127.0.0.1:55439/rst_hle
export DIAMOND_OBJECTS_DIR=work/objects
uv run diamond db-init
uv run diamond doctor
```

端口只绑定 `127.0.0.1`，使用独立数据库、Docker volume 和项目名 `rst-hle`。不得连接 ClawGym 的共享数据库来测试新 schema。应用不自动升级；迁移文件有 checksum，改历史迁移会启动失败。后续升级应新增编号迁移，不能覆盖已经执行过的 SQL。

个人 VM 使用 `work/private/runtime.env` 保存数据库连接设置，API 凭据由 `scripts/vm_run.py` 通过 SSH 临时注入。启动器不会把密钥放入命令行参数。内容寻址对象先写临时文件再原子替换，写后和读时检查 SHA-256 与大小。数据库已引用的损坏对象不会被默默忽略；先恢复对应备份。

## 备份与检查

同时备份 PostgreSQL 和 `DIAMOND_OBJECTS_DIR`；只备份数据库不包含来源正文与回答轨迹。当前本机对象目录仍是单机故障点，正式长跑前应接 GCS/S3 或做离机快照。不要将含完整题库/回答的数据库 dump 放进公开 Git。

测试库必须是独立的、名称以 `_test` 结尾的数据库；测试拒绝清理其他数据库。设置 `DIAMOND_TEST_DATABASE_URL` 后运行 `uv run pytest tests/test_postgres.py -q`，覆盖题目和 16 次判分写回、对象损坏、预算跨连接原子性、租约过期/旧 worker 隔离及写锁互斥。

未设置 `DIAMOND_DATABASE_URL` 时仍可用 SQLite 做快速离线测试。VM 试跑配置启用 PostgreSQL 后，请求缓存、HTTP 尝试预算、题目和状态均以数据库记录为准，本地 `work/runs` 只是便于查看的镜像。
