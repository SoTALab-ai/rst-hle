# 直接读取论文库

材料先解析入库，首50份完整独立材料可以触发试产，其余论文继续读取。保留原PDF，用6Luna读取正文、公式、数学表格和脚注，跳过插图及图注。模型一次读取每批1–2个物理页，同时提供同页原PDF文本层作符号消歧，原页图像为版面依据；乱码/空文本层不等于空页，不同义改写函数名。没有第二模型逐页验收。新入口与旧MinerU实验分开，不回写历史审查结果。

## 下载与研究类型

使用现有[期刊队列](../configs/papers/library-journals.json)发现DOI。`acquire_paper_library.py`轮流取各刊候选，精确匹配开放位置的DOI，逐跳遵守下载间隔；同内容不重复计篇。新PDF的研究类型由6Luna依据元数据和原文定位文本筛选，只有理论为主且有可重建推导的材料计入新接收量。该分类不验证公式真值。

正文和补充PDF分别保存。未核对外部补充材料时记录`not_checked`，不宣称不存在；正文内附录随原件保留。原件、收据和运行输出不提交Git。

```bash
uv run python scripts/discover_journal_papers.py \
  --journals configs/papers/library-journals.json \
  --since 2018-01-01 --until 2026-10-24 --per-journal 100 \
  --output work/discovery.json

# 个人VM中，先设置自己的DIAMOND_DATABASE_URL和API环境变量。
# 首次/升级显式执行，应用启动不会迁移。
uv run diamond db-init
uv run python scripts/acquire_paper_library.py \
  --discovery work/discovery.json --rates work/private/prices.json \
  --directory work/acquisition/RUN-ID --objects work/library-objects \
  --run-id acquisition-RUN-ID --target-per-domain 100
```

可用`--reuse-root`导入旧采集收据目录。下载目标与候选尝试分开，接口失败、研究类型拒绝与候选不足明确报告。下载完成生成`library-plan.json`，不会自动开始出题。

## 已有PDF与逐页入库

优先从已采集收据生成清单，保留DOI、期刊和正文/补充角色：

```bash
uv run python scripts/ingest_paper_library.py inventory \
  --receipts-root work/acquisition/RUN-ID/accepted \
  --expected-papers 200 --output work/library-plan.json

uv run python scripts/ingest_paper_library.py run \
  --plan work/library-plan.json --run-id read-RUN-ID \
  --directory work/read/RUN-ID --objects work/library-objects \
  --rates work/private/prices.json --pages-per-call 2
```

没有收据时支持`inventory --pdf-directory /absolute/path`，只按内容去重文件。此入口不能推断正文/补充关联，也不把文件名当作已核实的期刊或论文身份；有关元数据为`unknown/not_checked`。需要明确关联的补充材料应使用收据或严格`LibraryPlan`。

每个PDF的解析身份包含内容哈希、模型配置、端点哈希、提示、输出schema、分页和渲染器版本。批次冻结计划、配置、价格及代码；变化必须新run-id。已完成回答从PG/对象库复用；损坏对象、日志、渲染缓存拒绝恢复。页面未读清不能改成空页，缺页不能当成功。

## 存储和读取

004显式迁移创建`library_papers`、`library_documents`、`library_paper_documents`、`library_parses`、`library_pages`、`library_run_documents`。一个论文版本关联一份正文及补充文件，一个PDF可有多个明确的解析版本。原PDF按二进制哈希保存；转录与请求/响应按完整性收据保存。新论文库的JSON对象启用gzip，原PDF不改变。

数据库保存元数据、状态索引和对象引用，不把整个PDF二进制塞入关系表。成功付费响应和HTTP尝试写入PG及对象库后，可再生的本地请求镜像和页面PNG删除，降低大批次磁盘占用；响应原文仍在对象库。数据库和对象库必须一起备份。

```sql
SELECT p.id, p.title, p.domain, p.journal, d.pages
FROM diamond.library_papers p
JOIN diamond.library_paper_documents r ON r.paper_id=p.id
JOIN diamond.library_documents d ON d.id=r.document_id;
```

下游可调用`LibraryRepository.paper_text(run_id, paper_id)`，或显式导出：

```bash
uv run python scripts/ingest_paper_library.py export \
  --run-id read-RUN-ID --paper-id PAPER-VERSION-ID \
  --objects work/library-objects --output work/selected-paper.txt
```

导出要求实际页号覆盖全部物理页且无`unreadable`。这只保证所存转录的程序合同，不宣称公式准确率、补充材料完整性或科学结论正确；仍不能直接把它标为已通过质检的题源。

## 报告口径

分别记录计划篇数、实际登记篇数、缺少输入篇数、独立PDF数、预期页数、已存页数、空页、未读清页、缺页、接口/格式失败和实际费用。200篇目标若只提供20篇，剩余180篇仍在缺口中。`transcribed`仅表示整批程序合同完成，公式和科学认证字段固定为false。

## 统一管线与下载前去重

元数据发现后立即规范化DOI（大小写、`doi:`/DOI链接前缀），按DOI合并候选，保存所有原始元数据观察，不凭标题相似度合并不同论文。005迁移创建唯一的`library_metadata.doi`和观察版本表，登记在下载前完成。重复元数据、已登记身份、已复用原件、不同PDF版本分别报告；登记身份不代表已经取得全文。

采集缓存核对收据、原件哈希和物理页数。优先保留已策展的种子版本；同DOI不同PDF只算一篇，不同DOI但相同PDF也不重复计篇。下载计划和50份材料里程碑再次检查DOI与正文PDF唯一性。历史已发生的重复下载保留原报告、收据和费用，新run记录修复，不回写历史为“200篇独立论文”。

```bash
# 升级先显式执行diamond db-init，再启动管线。
uv run python scripts/run_paper_library_pipeline.py \
  --discovery work/discovery.json --rates work/private/prices.json \
  --directory work/library/RUN-ID --run-prefix RUN-ID \
  --objects work/library-objects --target-per-domain 100 \
  --preferred-reuse-root /absolute/path/curated-seed-receipts \
  --reuse-root /absolute/path/previous-accepted-receipts \
  --cache-root /absolute/path/terminal-acquisition
```

最后三项可选。`--cache-root`只借用已终态且可校验的下载/开放位置缓存，模型分类通过原始请求指纹复用PG付费响应。不会复制旧决策为新科学结论。统一入口依次完成元数据登记去重、开放下载、理论筛选、清单唯一性核对、直接模型读取与PG/CAS索引。缺少独立论文时阻止全文批量读取；输出`metadata-admission.json`、`reuse-admission.json`、`handoff.json`、各阶段报告及`cost-report.json`。

`--representative-checks`可接收已核对原件哈希/页号的开发反例。先真实读取这些代表论文，再检查固定符号是否保留或明确标记未读清；API/覆盖失败或静默改符号会阻止批量阶段。该检查不宣称整篇公式已认证。代表读取和批量读取共享解析身份，成功响应不重复购买。

## 50份完整材料触发出题

这是“完整解析材料数”，不是元数据条数、PDF文件数或已生成题数。只计正文和已关联补充PDF的所有物理页已存、无`unreadable`且有可读正文的论文；冻结前再核对原件及转录对象。冻结先完成的50篇，后到论文不替换队列。上游终态仍不足50篇时明确报缺口，不伪造出题启动。

```bash
# DIAMOND_LIBRARY_DATABASE_URL/DIAMOND_LIBRARY_OBJECTS_DIR指向论文库，连接只读；
# DIAMOND_DATABASE_URL/DIAMOND_OBJECTS_DIR指向另一个独立个人试产库。
uv run python scripts/run_library_question_campaign.py \
  --library-run-id RUN-ID-read --materials 50 --wait \
  --run-prefix QUESTIONS-RUN-ID --directory work/questions/QUESTIONS-RUN-ID \
  --rates work/private/prices.json --config configs/library50-questions.toml
```

每篇由6Luna规划1–5条不同推理结构的已有结论，检查原文逐字锚点和完整选页；多题时另次调用审查重复结构。材料不足或规划失败可为0条并保留拒绝原因，不强凑题数。50篇最多250条计划，分片进入原`QuestionExperiment`，仍需锁定金标、完整证明、独立审查、最难步骤挖空、科学质检、反代、verifier对抗、GLM首4次筛选及16次验收。简单题拒绝；仅对过难题做降难。同论文题目相关，计划量不等于合格交付量。

论文读取并发8，出题总并发8、solver并发6，为judge留容量。两库分开写锁，出题不会占住论文库写者。

## 费用台账

每阶段固定价格快照，按原HTTP尝试累计返回usage；成功、拒绝、失败和重试都计入。复用收据费用归原支付run，复用run不再次累计。跨库汇总必须为原支付run指定唯一scope，禁止重复加入同一run。尚无usage的尝试与预留未返回请求单列，不能当作0元。

```bash
uv run python scripts/report_campaign_costs.py \
  --manifest work/private/cost-manifest.json --output work/campaign-cost.json
```

清单包含`scopes`（`database_url_env`、`objects`）及`stages`（`scope`、`stage`、`run_id`、`directory`、`config`、完整`pricing`快照）。只读查询，不调用模型。输出分阶段、分角色美元费用、人民币参考、HTTP次数、未知用量和在途预留。VM/存储/网络账单未取得时标null并说明未并入；不能把API估算叫完整账单。历史13页探针、重复版本和补采成本也应纳入同一轮累计。
