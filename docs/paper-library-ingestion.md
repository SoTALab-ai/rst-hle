# 直接读取论文库

先完成论文库，再启动出题。保留原PDF，用6Luna读取正文、公式、数学表格和脚注，跳过插图及图注。模型一次读取每批1–2个物理页；没有第二模型逐页验收。新入口与旧MinerU实验分开，不回写历史审查结果。

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
