# 原始 HLE 题目难度校准

参考题评估使用独立 `calibrate` 入口，不需要论文证据 schema，不调用出题/改题/作者复核，也不向生产候选池或 2,000 道采购配额写题。判分直接对照保留的原始参考答案，疑似金标问题应单独复查。

用户于 2026-10-23 将判分模型改为 **gpt-6-luna medium**，优先于原需求中的 gpt-5.6-luna；官方 HLE 判分模板与字段不变。生产 QC 和参考校准的 solver 均使用同一官方 HLE 系统提示，按 GLM5.2 公开采样设置采用 `temperature=1.0`、`top_p=0.95`。用户随后指定每次解题以 600 秒为限，移除客户端输出 token 上限；这属于本项目的重复采样协议，不能称为官方排行榜的完全复现。

可从需求文档的 120 道原始 Diamond 示例中分层抽样：默认 8 个大类各 3 题，各类优先选不同子类，随机种子 `20261023`。当前先以 `--per-category 1` 选八题完成 16 次采样，控制试跑费用。这个样本受原文示例选择与采购子类限制，**不是全部 728 道 Diamond 文本题的随机样本**，不能据此估计全量题数。每类仅一至三题，只用于早期方向判断。

```bash
python3 scripts/select_reference_examples.py work/private/requirements/source.md
uv run diamond --config configs/calibration.toml calibrate \
  work/private/calibration/selection24.jsonl --run-id YOUR_CALIBRATION_RUN
```

24 × 16 = 384 次解题和 384 次判分，默认 16 个 worker。可用 `--max-new-samples 8` 先检查八组解题/判分，再以同一 run-id 续跑。逐次保存逻辑请求、实际模型版本和判分，支持同 run-id 续跑。完整 16 次后才归类为 0/16、1–8/16 或 9–16/16；网络异常、截断和不合 schema 均归为未完成，不当作错误答案。

运行结果写入 `outputs/<run-id>.json`，包含领域统计、每题正确次数和错误列表。PostgreSQL 的 run 元数据保存逐次索引；完整原始请求/响应保存在数据库指向的产物库。原题、标签和完整回答不进公开 Git。

## 公开可复用资料检索

已检查以下公开来源，尚未找到符合“同一 GLM5.2、逐题 16 次独立文本回答、可重新判分”的现成轨迹集：

- [Z.ai 官方 GLM5.2 发布说明](https://z.ai/blog/glm-5.2)：有 HLE 总分和采样设置，不能由总分反推每题 1–8/16。其公开结果的最大生成预算也与本次试跑不同。
- [Z.ai 的 glm-simple-evals-dataset/hle](https://huggingface.co/datasets/zai-org/glm-simple-evals-dataset/tree/main/hle)：公开题目集，不能直接当作 GLM5.2 的重复回答。
- [yoonsanglee/hle-react](https://huggingface.co/datasets/yoonsanglee/hle-react)：列出的 GLM 版本为 GLM-4.7-Flash，且是工具轨迹设置，不能代替本轮 GLM5.2 文本采样。
- `AletheiaResearch/GLM-5.2-Bench`：检查到的是终端任务材料，不是目标 HLE 逐题重复样本。

以上只是已检查范围内的发现，不声称全网不存在相应结果。后续取得匹配的原始回答时，可复用回答并按当前 judge 重判；模型版本、题库版本、推理预算、工具使用和抽样方式都应保留。

## 成本

估算按解题与判分两部分 token 分别计费，缓存命中单列。个人网关的费率与账单读取保存在本地 `outputs/calibration-price-assumptions.json`，不使用公开标价冒充实际结算价。

移除 32k 上限后，之前的费用范围不再是预算封顶。600 秒约束时间，不能确定 token 用量或收费上限，因此先做少量请求检查实际耗时和 usage，再扩展采样。最终报告按已返回 usage 与当时费率折算，并单列失败/重试；超时取消不保证供应商停止计费，未返回 usage 的请求费用记为未知。已有 VM 的固定费用另计。

## 时长、联网与工具权限

官方来源固定为 HLE 仓库 `22ed3074b1e7b134bcbc09028d0ba320839b0655`，核对日期 2026-10-23：

- [直接 API 评估代码](https://github.com/centerforaisafety/hle/blob/22ed3074b1e7b134bcbc09028d0ba320839b0655/hle_eval/run_model_predictions.py) 设置客户端超时 600 秒、最多重试一次，未提供模型可调用的搜索或代码工具。这是客户端设置，不能推断出整个 HLE 统一规定每题硬限十分钟。
- [官方 README](https://github.com/centerforaisafety/hle/blob/22ed3074b1e7b134bcbc09028d0ba320839b0655/README.md) 说明评估使用可用的生成预算，也提示模型可能发生生成退化。没有查到统一的每题墙钟时限。
- 用户明确选择**每次解题最多 600 秒，不由客户端设置输出 token 上限**。实现为从获得并发槽开始计时，覆盖所有请求尝试和重试等待；排队不计时。判分另有同样的 600 秒期限，不占解题时间。不发送 `max_tokens` 或 `max_completion_tokens`；模型/供应商固有默认上限仍可能截断回答，必须记录。
- 当前配置 `evaluation_mode = "no_tools"`：请求只含题目与官方系统提示，不附工具定义，不接入搜索、浏览器、Python 或 shell，也不使用在线搜索模型别名。VM 联网调用 API、下载论文是管线基础设施访问，不是被测模型的查资料权限。
- [HLE-Diamond 官方工具评估规则](https://github.com/centerforaisafety/hle/blob/22ed3074b1e7b134bcbc09028d0ba320839b0655/docs/evaluation-with-tools.md) 另允许受控 `web_search` / `web_fetch` 加离线 Python 环境；代码沙箱不得直接联网或下载安装包，网页访问必须执行官方 blocklist、题目原文搜索和缓存检查。**不能笼统说 HLE 禁止一切联网，也不能让解题模型任意上网。**该模式尚未在本项目实现，当前结果只能标为无工具结果。

超时、供应商截断、空回答、实际模型不匹配或判分格式错误均保留失败记录，不算作答错。只有完整得到 16 次有效判分才判断题目是否满足 1–8/16；未完成项单独列出。截断响应保留已返回 usage，超时收据明确标记 usage 未知。

## VM 长任务与小样本复用

为避免 SSH 中断终止长任务，可脱离连接运行；凭据只经加密连接进入进程环境，不写入日志、命令参数或新建密钥文件：

```bash
python3 scripts/vm_run.py --host YOUR_SSH_ALIAS --detach \
  --log work/runs/YOUR_RUN/runner.log -- \
  diamond --config configs/calibration.toml calibrate \
  work/private/calibration/selection8.jsonl --run-id YOUR_RUN
```

`--reuse-run PREVIOUS_RUN` 可复用同一数据库里的参考评估请求，适用于从预检查的大候选集合缩小到八题。模型、采样参数、时限和提示必须完全匹配；请求缓存还逐字比较题目、judge 金标提示与角色参数，并保留样本编号。复用同一个样本不会变成新的独立采样。已有超时、截断收据同样复用并列为未完成，不重新付费抽取一个更好结果。SQLite 本地烟测没有跨运行共享的请求库，实际复用使用 VM 的 PostgreSQL。

进度和用量统计可只读查询，不发起模型调用：

```bash
uv run python scripts/calibration_status.py work/private/calibration/selection8.jsonl \
  --run-id YOUR_RUN --output outputs/YOUR_RUN.progress.json
```

可另传 `--rates` 指定本地网关费率快照，估算已返回 usage 的费用；不能把供应商返回的上游 `usage.cost` 直接当作个人网关账单。复用请求按唯一 request-id 去重，费用不重复累计。没有返回 usage 的超时请求及未留完整响应的重试费用列为未知。
