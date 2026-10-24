# PDF论文解析与公式校验

当前先使用MinerU免登录Agent API直接接收PDF，开启公式、表格识别并输出Markdown。
普通pypdf抽字只用于定位结果，不作为可信公式输入。正式试产仍使用个人VM和个人模型API。

## 服务边界

- 官方文档列出的轻量接口限制为每文件10MB、20页；支持`page_range`。本适配器逐物理页请求，明确保留页码映射，不猜测合并Markdown中的分页。
- 按IP限频；提交429最多四次、每次等待不超过60秒，不轮换IP。其他失败保留任务收据。任务创建结果未知时禁止自动重提；已创建任务续查同一个task-id。
- 轻量API不暴露固定模型版本，也不提供本项目可核对的费用收据。保存请求、结果哈希与时间；外部解析费用记为未知，不能当作零费用。
- 带Token的精准API以及飞桨官方API可作为后续替换选项，未经真实验证不宣称已接通。已知PDF文件仍保存于独立文件库，不放入Git。

## 前置流程

1. 固定正文和关联补充PDF的文件哈希。`configs/papers/pdf20-supplements.json`记录本批20篇的核查入口、范围和内嵌附录/补充起始物理页。`no_separate_listed`只表示核查范围内没有列出独立附件，不等同于证明不存在后续材料。
2. 从论文及补充材料中定位一个已有理论结果，选取其结论、完整推导、定义与条件所在页，最多10页。无法覆盖则拒绝材料，不借用其他论文填补。
3. MinerU按页转成Markdown/LaTeX，保留独立任务收据。另一模型调用对照原页图像逐公式检查；只有发现误读时才让作者按反馈修正，最多两轮。原OCR结果不覆盖。
4. 仅将通过检查的页转录组装成新`pdf:`来源身份，保留原PDF页码与哈希。未读页不能声称已核验；这种模型复核也不是数学形式化证明。
5. 材料通过后，继续既有已知答案锁定、完整参考推导、独立正确性质检、最难步骤挖空、verifier对抗和4→16次GLM限时筛选。OCR接口失败不算解题错误。

```sh
uv run python scripts/review_pdf_materials.py \
  --directory work/materials --config configs/theory20-verifier.toml \
  --rates work/private/prices.json --supplements configs/papers/pdf20-supplements.json \
  --run-id pdf20-material-review-20261024 --parser mineru --workers 4
```

运行目录的`input/<plan_sha256>/`须事先保存不可变采集收据及其PDF/抽字文件。
同一目录只运行一个进程；代码、配置、解析器或材料变化必须另建run-id。复用完整性有效的原收据，不重新购买已成功的模型回答。

## 真实预检范围

数学使用Annals论文固定PDF物理页35，物理使用热辐射论文固定PDF物理页2，MinerU均已返回解析结果。
人工抽看发现核心显示公式可以恢复，行内公式仍有误读，因此不直接放行。
个人VM上的gpt-6-luna图像复核真实控制中，两个合法公式被接受，反转物理系数和删除数学负号的两个错误版本被拒绝。
该四例只证明这组检查有检错能力，不代表所有页的识别准确率。

官方入口：[MinerU API](https://mineru.net/doc/docs/index_en/)；
[飞桨API配额](https://ai.baidu.com/ai-doc/AISTUDIO/Xmjclapam)；
[飞桨API调用配置](https://ai.baidu.com/ai-doc/AISTUDIO/Cmkz2m0ma)。
