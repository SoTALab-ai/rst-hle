# PDF正文与补充材料库

新材料按“正式论文正文PDF＋明确关联的补充PDF”组织。正文自带的附录/补充章节随原PDF保留；单独发布的补充PDF单独保存。一个材料包含一份正文与零至多份补充材料，不物理合成大PDF，也不把源码包中的TeX、草稿、注释和重复PDF转录拼成正文。

旧试产的代码、全文和结果保持原冻结身份。本文提供新的独立采集入口，不覆盖历史Source，也不在本轮启动付费出题。

## PDF与TeX的取舍

PDF适合作为正式版面和阅读范围的依据：保留作者实际发表的顺序、公式、图表和页码。问题在于PDF没有可靠的语义层，普通抽字对分式、上下标、双栏和扫描页可能失真；不能把“能抽出1000个字”当成公式正确。参见[pypdf的说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html#why-text-extraction-is-hard)。

新入口保留原PDF，按文件和页输出辅助转录。`identity_review=pending`、`formula_review=pending`、`ready_for_authoring=false`固定保留；普通抽字不自动放行。后续需要验证公式读取和模型输入接口，才能将材料导入正式出题源。当前没有宣称已实现或验收数学公式OCR。

补充材料状态显式区分`not_checked`、`embedded`、`separate`、`none_confirmed`。没有发现补充链接，只能记未检查，不能推断不存在。声明已内嵌或不存在时需要可定位依据；声明单独补充时必须有对应文件。多PDF附件不自动推断角色，同一PDF也不能同时充当正文与补充。

## 资源入口与期刊

| 入口 | 本项目用途 | 依据 |
|---|---|---|
| OpenAlex | 按DOI查找出版方、机构仓储、arXiv等位置，记录版本和开放PDF地址 | [Locations](https://help.openalex.org/data/locations/) |
| Numdam | 数学期刊的正文档案，如IHÉS、ENS等；关注开放时间和扫描质量 | [数学全文档案](https://www.numdam.org/) |
| Centre Mersenne | 数学开放全文；IHÉS从2026年起由该平台发行 | [IHÉS说明](https://www.ihes.fr/pub-maths-mersenne/) |
| SCOAP³ | 理论高能物理等领域的出版PDF，如PRL、PRD、JHEP | [仓储说明](https://scoap3.org/scoap3-repository/) |
| HAL和机构仓储 | 查找作者公开的接受稿或出版稿；必须核对DOI、版本及附件对应 | [HAL API](https://api.hal.science/docs/search) |
| arXiv | 数学/理论物理的公开PDF，可含附录；记录实际下载版本和哈希 | [arXiv](https://arxiv.org/) |
| Unpaywall | 按DOI补充寻找开放位置；API要求调用者邮箱，本轮不自动申请或填造邮箱 | [API](https://data.unpaywall.org/products/api) |

期刊优先队列为[12刊配置](../configs/papers/library-journals.json)：数学Annals、JAMS、Inventiones、IHÉS、ENS、Duke；物理CMP、PRL、PRX、PRD、JHEP、Annals of Physics。它是策展优先级，不是客观排名或理论适用性的保证。期刊验证与下载地址分开：用DOI/ISSN核实发表记录，再到不同仓储获取相应全文，不只依赖出版社下载页。

OpenAlex目前支持少量匿名调用，规模使用可配置`OPENALEX_API_KEY`；密钥只用环境变量与Authorization请求头，不放入URL或结果文件。[认证说明](https://help.openalex.org/api/authentication/)。SCOAP³接口使用其[JSON API说明](https://github.com/SCOAP3/scoap3/wiki/JSON-API-(legacy-schema))，返回后仍逐条精确核对DOI，不信任查询命中本身。

## 使用入口

先获取候选元数据，不能将其计为下载全文：

```bash
uv run python scripts/discover_journal_papers.py \
  --journals configs/papers/library-journals.json \
  --since 2023-01-01 --until 2026-10-24 --per-journal 8 \
  --output work/library-discovery.json

uv run python scripts/resolve_pdf_library.py \
  --discovery work/library-discovery.json \
  --output work/library-plans.jsonl --per-journal 2
```

解析器按实际DOI匹配；SCOAP³多PDF需要另行区分正文/补充，OpenAlex保留开放位置的版本。自动队列均为`theory_screen=unreviewed`，人工排除实验文章、勘误等不适合目标的材料后，再冻结下载清单。没有匹配开放位置的DOI单列，不凭空构造下载地址。

采集清单为严格`PaperPdfPlan` JSONL，一条一篇。每个`documents`项明确`role`、`location`、`version`、`provenance_url`；location支持公开HTTPS和明确的本地PDF路径。正文必须在第一项且只有一项；没有TeX输入选项。

```bash
uv run python scripts/collect_pdf_library.py \
  --manifest work/library-plans.jsonl \
  --directory work/pdf-library/COLLECTION-ID
```

每份PDF分别保存原文件、逐页辅助转录及SHA256，完成的单文件有检查点，整篇有完整性收据。复用时验证原文件、转录和计划，损坏缓存拒绝恢复。补充采集失败时不把半份材料计成整篇成功；已有合法正文检查点可复用。HTTP重定向逐跳限制为公开HTTPS，不转发API凭据；HTML错误页、压缩包、重复正文/补充及加密PDF不进入成功包。

CLI对同一目录加排他写锁，并冻结清单、采集代码和pypdf版本；变化须用新目录。所有计划计入`planned`，分别记录`downloaded`、`failed`、`ready_for_authoring`。此次入口是文件暂存层，`database_writes=0`、`model_calls=0`，不修改PostgreSQL schema或既有论文库，不把待审全文混入正式题源。

## 验证边界

针对性测试覆盖：正文/补充分存、扫描/空抽字仍待审、合法缓存复用、文件/收据损坏、正文成功而补充失败的恢复、HTML/私网重定向拒绝、单篇失败不影响其他计划、重复PDF拒绝、DOI精确匹配、开放版本选择、多PDF角色不猜测。真实网络解析和个人VM的PDF采集另记实际覆盖，不用mock测试代替资源可用性或公式正确性。

## 2026-10-24个人VM实际采集

首批20篇全部取得PDF，失败0，共1060页：数学9篇、物理11篇，按IHÉS名称别名归一后覆盖10种期刊。20份PDF的内容哈希互不相同。17份从公开地址获取，3份导入已有PDF；不存在20篇全部来自出版社的假设。

| 来源 | 实际份数 |
|---|---:|
| arXiv公开PDF | 5 |
| Centre Mersenne | 2 |
| Numdam | 2 |
| Springer开放PDF地址 | 2 |
| CERN/SCOAP³附件 | 6 |
| 已有arXiv PDF缓存导入 | 3 |

这不是96条DOI全自动入库：12刊实际发现96条去重元数据，第一批24条DOI自动定位得到9份计划；再用明确仓储记录和现有PDF补充并人工冻结为20篇采集清单。其中两篇的下载位置改选Mersenne出版PDF，记录其出版日期尚未核实。SCOAP³的全文清单和附件实际可读，但通用DOI检索的兼容仍有限，精确匹配不到时不冒充成功。

1篇确认补充材料内嵌在固定PDF中，19篇仍为`not_checked`；实际没有采集单独的补充PDF。分文件补充采集、重复文件拒绝和失败恢复有受控测试，但不能据此声称已完成真实补充材料的全量覆盖。所有20篇仍然身份/公式待审、可直接出题0篇，理论适用性也未自动批准。模型调用0、数据库写入0。

在个人VM上用真实20份文件做了禁止网络的缓存恢复，20份完整性收据均复用成功，PDF及转录哈希全部验证通过。原文件、转录、代码和冻结输入归档已下载并核对SHA256；压缩包解包恢复未另测。

- 运行身份：用户个人VM `liaojunteng-vm`；采集未使用模型API凭据。
- collection：`pdf-library-collection-20261024-v1`；源代码：`28f30e86b3f5e5ff0f6359db8f81d91b5ccb8bb7`。
- 输入SHA256：`6271a012a81ec7c660129eef6f197e36bf986a53b069857d2df317223e2f76f3`。
- VM独立目录：`~/Developer/rst-hle/work/pdf-library-collection-20261024-v1/work/library`；正式PostgreSQL、旧试产与其收据未改写。
- pypdf版本：6.21.2。正文原PDF与逐页辅助转录均保留，不将转录当成公式校验结果。
- 本地ruff和doctor通过，pytest 213通过、14项PG因本地未配置专用库跳过；[CI](https://github.com/SoTALab-ai/rst-hle/actions/runs/40090854270)共227项通过，包含14项独立真实PG。数据库实现没有变动。

逐篇DOI、来源、页数、PDF哈希和待审状态见[脱敏采集结果](pdf-paper-library-results-2026-10-24.json)。PDF原文件、转录和原始请求记录不进入Git。
