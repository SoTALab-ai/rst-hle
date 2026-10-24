# PDF论文解析与公式校验

当前支持MinerU免登录Agent API，以及用户Token对应的精准解析API。正式新批次优先精准解析`vlm`，
每份PDF只上传一次，按返回JSON的零基页码映射到原PDF物理页。开启公式、表格识别。
普通pypdf抽字只用于定位结果，不作为可信公式输入。正式试产仍使用个人VM和个人模型API。

## 服务边界

- 官方文档列出的轻量接口限制为每文件10MB、20页；支持`page_range`。本适配器逐物理页请求，明确保留页码映射，不猜测合并Markdown中的分页。
- 按IP限频；提交429最多四次、每次等待不超过60秒，不轮换IP。其他失败保留任务收据。任务创建结果未知时禁止自动重提；已创建任务续查同一个task-id。
- 轻量API不暴露固定模型版本，也不提供本项目可核对的费用收据。保存请求、结果哈希与时间；外部解析费用记为未知，不能当作零费用。
- 精准API已通过用户Token完成真实单页上传、解析和ZIP/Markdown/逐页JSON下载；返回的解析器版本为3.4.4。
  当前中文管理页列出每文件200MB、200页，每账号每天1000页最高优先级，超出部分降低优先级；本地文件上传单批说明列出50个。
  英文静态文档曾出现不同页数上限，配置按当前中文管理页的保守限制执行。20篇均低于单文件限制，总计1060页。
- 飞桨官方API仍未接通。已知PDF文件、Token、原始解析结果及ZIP仍保存于独立私有文件库，不放入Git。
- 用户已指定不解读论文插图/曲线。精准JSON中的image/chart块和图注不进入作者输入；保留正文、边注中的条件、公式、数学表格与表注。
  图像型公式/表格若没有可用结构化内容，明确拒绝，不猜测其内容。公式字形核验不用于推测科学插图或读取曲线数据。

## 前置流程

1. 固定正文和关联补充PDF的文件哈希。`configs/papers/pdf20-supplements.json`记录本批20篇的核查入口、范围和内嵌附录/补充起始物理页。`no_separate_listed`只表示核查范围内没有列出独立附件，不等同于证明不存在后续材料。
2. 从论文及补充材料中定位一个已有理论结果，选取其结论、完整推导、定义与条件所在页，最多10页。无法覆盖则拒绝材料，不借用其他论文填补。
3. MinerU转成Markdown/LaTeX，保留独立任务收据和逐页映射。另一模型调用对照原页的公式字形检查，忽略插图/图注；只有发现误读时才让作者按反馈修正，最多两轮。原OCR结果不覆盖。
4. 仅将通过检查的页转录组装成新`pdf:`来源身份，保留原PDF页码与哈希。未读页不能声称已核验；这种模型复核也不是数学形式化证明。
5. 材料通过后，继续既有已知答案锁定、完整参考推导、独立正确性质检、最难步骤挖空、verifier对抗和4→16次GLM限时筛选。OCR接口失败不算解题错误。

```sh
uv run python scripts/review_pdf_materials.py \
  --directory work/materials --config configs/theory20-verifier.toml \
  --rates work/private/prices.json --supplements configs/papers/pdf20-supplements.json \
  --run-id pdf20-material-review-20261024-v3 --parser mineru_precision --workers 4
```

运行目录的`input/<plan_sha256>/`须事先保存不可变采集收据及其PDF/抽字文件。
精准解析需要通过进程环境提供`MINERU_TOKEN`；本机私有配置为`work/private/ocr.env`，权限0600。
`uv run --env-file work/private/ocr.env python scripts/vm_run.py ...`通过SSH标准输入转发，Token不进入命令参数或日志。
同一目录只运行一个进程；代码、配置、解析器或材料变化必须另建run-id。复用完整性有效的原收据，不重新购买已成功的模型回答。
云服务实际返回3.4.4的`legacy_content_list_v1`，包含`aside_text`、`chart`、`ref_text`等块，不能直接套用开源新版输出结构。
下载ZIP先保存哈希检查点，再解码；结构适配失败后可在新快照中复用原ZIP，不重新上传或创建解析任务。损坏缓存明确拒绝恢复。

进入出题数据库前，`prepare_pdf_question_plan.py`重放原PDF哈希、页码、转录内容、模型响应、审查图像和原OCR收据，不能只相信`ready`字段。
`configs/papers/pdf20-targets.json`冻结20条计划及数学/物理各10条的出题方向；材料未通过的条目仍保存在`cohort.json`总分母中，不能计为成功题。

```sh
uv run python scripts/prepare_pdf_question_plan.py \
  --directory work/materials --output work/private/pdf20-prepared
```

## 真实预检范围

数学使用Annals论文固定PDF物理页35，物理使用热辐射论文固定PDF物理页2，MinerU均已返回解析结果。
人工抽看发现核心显示公式可以恢复，行内公式仍有误读，因此不直接放行。
个人VM上的gpt-6-luna图像复核真实控制中，两个合法公式被接受，反转物理系数和删除数学负号的两个错误版本被拒绝。
该四例只证明这组检查有检错能力，不代表所有页的识别准确率。

官方入口：[MinerU API](https://mineru.net/doc/docs/index_en/)；
[当前中文API管理文档](https://mineru.net/apiManage/docs)；
[飞桨API配额](https://ai.baidu.com/ai-doc/AISTUDIO/Xmjclapam)；
[飞桨API调用配置](https://ai.baidu.com/ai-doc/AISTUDIO/Cmkz2m0ma)。
