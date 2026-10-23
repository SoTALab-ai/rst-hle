# 数据与运行契约

自备来源 JSONL 每行：

```json
{"id":"my-paper-v1","title":"Example Paper","uri":"https://example.org/paper","kind":"paper","license":"unspecified","path":"papers/paper.pdf"}
```

`path` 相对材料清单文件，可指向 UTF-8 文本/Markdown 或 PDF；也可直接传 `text`。`kind` 支持 `paper`、`primary`、`rulebook`；`synthetic` 仅用于离线/试跑。PDF 提取按 `[page N]` 保留页号；仅摘要不会由爬虫当作全文入库。OCR 错误或依赖图像的材料应先人工转写校对。

```bash
uv run diamond ingest work/library/sources.jsonl
```

来源文本有 SHA-256，重复 ID 不允许对应不同文本。修改来源请更换带版本的 ID。证据必须包含 `source_id`、`locator`、原文短引句 `quote` 和 `supports`。程序核对引句存在性；模型复核其支持关系，locator 本身尚未自动验证页码真实性。

题目输入每行符合 `Submission`：

- `id`：字母、数字、下划线或横线；内容和 ID 共同生成内部不可混淆的题目标识。
- `item`：question / answer / answer_type / rationale / category / subtopic / material_key / required_outputs / assumptions / evidence。
- `solution`：topic / assumptions / derivation / answer / evidence / reasoning_delta。至少两个推导步骤，答案与题目标准答案严格一致。
- 可选 `parent_id`：已有合格父题的内部 ID。谱系由系统恢复，调用者不能提交自己的通过标志或分数。

选择题保留 A–E 及更多完整选项；简答题用 `exactMatch`。分类名称采用配置中的 v3 原名，不发明未提供的数值 ID。所有图表/代码/规则必须完整转为题面文本，不能依赖缺失的图像。

外部排除库每行：`{"id":"reference-id","question":"完整题面","material_key":"可选"}`。启用时每个给定参考都进入分批语义比对；不以向量 top-K 结果代替全库检查。该实现适合小批次，扩大到完整训练库需做索引/候选召回和独立漏检评估。

`work/runs/<id>/requests` 保存无密钥的原始请求、响应、usage 和逻辑请求 ID；`events.jsonl` 保存阶段事件。SQLite 保存来源、候选和运行元数据。运行中断保留已成功的逻辑请求；同一 sample_id 的缓存不被当作另一次采样。请求错误不会补写一个 `correct=no`。

导出包含题面、答案、解题依据、分类、证据、谱系和 16 次回答/判分，并附 manifest。整批完成只认 61 类各自满足数量，不允许其他子类抵扣缺口。公开题目包应使用公开发布/下载地址；本阶段尚未发布任何题包。

