// 业务流程的展示投影：只读取已有证据，不重判、不补造缺失的阶段审批。
export const FLOW_STATUS = {
  recorded_pass: ["记录通过", "green"],
  failed: ["未通过", "red"],
  uncertain: ["不确定", "amber"],
  uncovered: ["未覆盖", "amber"],
  missing: ["无阶段记录", "gray"],
  not_applicable: ["不适用", "gray"],
  recorded: ["已有记录", "blue"],
  incomplete: ["尚未完成", "amber"],
  halted: ["按规则停止", "amber"],
  observed: ["部分信息已登记", "blue"],
};

const boolCheck = (label, value, detail = "") => ({
  label,
  verdict: value === true ? "pass" : value === false ? "fail" : "missing",
  detail,
});
const verdictCheck = (label, value) => ({
  label,
  verdict: value?.verdict || "missing",
  detail: value?.reasoning || value?.reason || "",
});
function checksStatus(checks) {
  if (!checks.length) return "missing";
  if (checks.some((c) => ["fail", "reduced"].includes(c.verdict)))
    return "failed";
  if (checks.some((c) => c.verdict === "uncertain")) return "uncertain";
  if (checks.some((c) => c.verdict === "unsupported")) return "uncovered";
  if (checks.some((c) => c.verdict === "missing")) return "incomplete";
  return "recorded_pass";
}
function stage(id, title, role, description, gate, options = {}) {
  return {
    id,
    title,
    role,
    description,
    gate,
    status: "missing",
    summary:
      "当前记录未覆盖此阶段；无法据此区分未执行、未同步或不适用，不推断为通过。",
    checks: [],
    evidence: null,
    ...options,
  };
}
function reviewed(
  id,
  title,
  role,
  description,
  gate,
  evidence,
  checks,
  extra = {},
) {
  return stage(
    id,
    title,
    role,
    description,
    gate,
    evidence
      ? {
          status: checksStatus(checks),
          summary:
            evidence.reason ||
            evidence.feedback ||
            "展示已有审查结论；记录通过不代表当前版本全部审批有效。",
          checks,
          evidence,
          ...extra,
        }
      : extra,
  );
}
const axes = {
  parameter_scope: "参数范围",
  branch_coverage: "分支覆盖",
  boundary_cases: "边界与例外",
  inference_information: "推理信息",
  output_obligations: "目标义务",
  shortest_solution: "最短正确解",
};
const auditNames = {
  valid: "题目有效",
  self_contained: "自包含",
  uniquely_answerable: "答案唯一",
  answer_supported: "金标有据",
  substantive_delta: "实质性推理",
  category_correct: "学科与子类准确",
  no_shortcut: "无泄露或旁路",
};
const coreNames = {
  premises: "独立前提",
  definitions: "对象定义",
  parameter_domain: "参数域与边界",
  reference_answer: "金标与全部输出",
  physical_regime: "物理条件与守恒",
};
const maskNames = {
  selection_supported: "所选确为最难步骤",
  hidden: "最难步骤已隐藏",
  necessary: "桥梁对正确解必要",
  recoverable: "可由题设恢复",
  not_answer_only: "隐藏推理而非只隐藏答案",
};

function burdenStage(phase, evidence, applicable) {
  const value = evidence?.[phase];
  const id = `rst-${phase}`;
  const title =
    phase === "proposal" ? "完整解提案净负担审查" : "实际题面净负担复查";
  const checks = value
    ? [
        ...value.comparisons.map((c) => ({
          label: axes[c.axis] || c.axis,
          verdict: c.verdict,
          detail: c.evidence,
        })),
        {
          label: "整体不得抵销",
          verdict:
            value.net_verdict === "no_offset"
              ? "pass"
              : value.net_verdict === "uncertain"
                ? "uncertain"
                : "fail",
          detail: value.feedback,
        },
      ]
    : [];
  return reviewed(
    id,
    title,
    "独立净负担审查",
    "逐项比较父题与子题的参数范围、分支、边界、推理信息、目标义务和最佳合法短解。新增推理不能抵销任何一项简化。",
    "任一削弱或不确定即阻断；题面定向修复后必须重新审查。审批绑定父快照、锁定推导与实际题面。",
    value,
    checks,
    !applicable
      ? {
          status: "not_applicable",
          summary: "RST 子题专用阶段；当前不是可识别的 RST 子题。",
        }
      : {},
  );
}

export function buildQualityFlow(data) {
  const c = data.candidate,
    q = data.summary,
    context = data.workflow || {},
    settings = context.settings || {};
  const events = context.events || [];
  const lastEvent = (name) => events.filter((e) => e.event === name).at(-1);
  const cs = c.consistency,
    va = c.verifier_assessment,
    finalVerifier = va?.versions?.at(-1),
    result = finalVerifier?.result;
  const rst =
    Boolean(c.rst_origin) || settings.synthesis_protocol === "rst_curriculum";
  const child = rst && c.generation > 0;
  const known = c.known_result,
    blueprint = c.blueprint_audit;
  const proposal = c.difficulty_origin?.selected;
  const proposalAdversary =
    settings.require_difficulty_adversary === true ||
    Boolean(c.difficulty_origin);
  const blueprintChecks = blueprint
    ? [
        boolCheck("推导有效", blueprint.valid),
        boolCheck("答案正确", blueprint.answer_correct),
        boolCheck("推导完整", blueprint.derivation_complete),
        {
          label:
            c.source_fidelity === "advisory"
              ? "来源对齐（溯源提示）"
              : "来源对齐",
          verdict: blueprint.source_grounded
            ? "pass"
            : c.source_fidelity === "advisory"
              ? "advisory"
              : "fail",
          detail: blueprint.source_notes?.join("；") || "",
        },
        boolCheck(
          "逐步覆盖与推导条数一致",
          blueprint.reviewed_step_count == null
            ? undefined
            : blueprint.reviewed_step_count === c.solution.derivation.length,
          `审查 ${blueprint.reviewed_step_count ?? "未记录"} 条 / 推导 ${c.solution.derivation.length} 条`,
        ),
        ...blueprint.issues
          .filter(
            (i) => c.source_fidelity !== "advisory" || i.part !== "source",
          )
          .map((i) => ({
            label: `问题：${i.part}${i.step_index ? ` · 第${i.step_index}步` : ""}`,
            verdict: "fail",
            detail: i.claim,
          })),
      ]
    : [];
  const sourceRecords = data.sources || [];
  const independent = cs?.independent_solution;
  const independentChecks = independent
    ? [
        {
          label: "盲推导结果",
          verdict:
            independent.outcome === "solved"
              ? "pass"
              : independent.outcome === "invalid"
                ? "fail"
                : "uncertain",
          detail: independent.issues.join("；"),
        },
      ]
    : [];
  const report = cs?.substitution;
  const priorCounterexample = cs?.certificate_attempts?.some(
    (attempt) =>
      attempt.certificate.faithful_translation &&
      attempt.substitution.verdict === "fail",
  );
  const certificate = cs?.certificate_audit;
  const correct = q.correct,
    total = q.samples;
  const early = settings.early_screen_samples;
  const earlyExplicitlyDisabled = early === 0;
  const first = [...c.trials, ...c.deadline_outcomes].filter(
    (t) => t.sample_id < 4,
  );
  const firstIds = first.map((t) => t.sample_id);
  const firstComplete =
    first.length === 4 &&
    new Set(firstIds).size === 4 &&
    firstIds.every((id) => id >= 0 && id < 4);
  const firstCorrect = firstComplete
    ? c.trials.filter((t) => t.sample_id < 4 && t.judgment.correct === "yes")
        .length
    : null;
  const samplesBroken = q.sample_error === true;
  const fullComplete = total === 16 && !samplesBroken && correct != null;
  const post = lastEvent("post_evaluation_audit");
  const finished = lastEvent("candidate_finished");
  const sourceEvidence = {
    sources: sourceRecords,
    source_ids: c.source_ids,
    source_notes: c.source_notes,
  };
  const freeze = va?.frozen_item_sha256;
  const observations = result?.observations || [];
  const agreementChecks = observations.map((o) => ({
    label: o.case_id,
    verdict:
      o.oracle.correct === "uncertain" || o.verifier.correct === "uncertain"
        ? "uncertain"
        : o.oracle.correct === o.verifier.correct &&
            o.oracle.correct === o.hle.correct
          ? "pass"
          : "fail",
    detail: `Oracle ${o.oracle.correct} / Verifier ${o.verifier.correct} / HLE ${o.hle.correct}`,
  }));
  const protocolLabel = rst
    ? "RST 逐轮生长研究"
    : proposalAdversary
      ? "两阶段难度对抗"
      : "普通生产 / 冻结历史协议";
  const groups = [
    {
      id: "materials",
      title: "材料准备与规划",
      subtitle: "先核实材料身份，再确定不同推理结构；完整转录不代表科学合格。",
      stages: [
        stage(
          "source-identity",
          "来源登记与版本冻结",
          "来源登记 / 程序校验",
          "规范化 DOI 去重；登记正文与明确关联补充材料的文件、版本、物理页和内容哈希；同 DOI 或同 PDF 不重复计独立材料。",
          "身份不明、哈希损坏或来源未登记时阻断。",
          {
            status: sourceRecords.length ? "observed" : "missing",
            summary: `此版本关联 ${c.source_ids.length} 个来源，当前可读元数据 ${sourceRecords.length} 份。来源记录不证明正文/补充齐全。`,
            evidence: sourceEvidence,
          },
        ),
        stage(
          "material-reading",
          "全文读取与页级完整性",
          "6Luna 直接读取 + 程序核对",
          "新论文库直接读取一次；程序检查文件身份、页码覆盖、JSON结构和未读清标记，补充材料单独核对。",
          "缺页、未读清或未核对的补充材料不能当完整；本页没有跨库页级收据时保留未知。",
        ),
        stage(
          "planning",
          "出题计划与结构独立性",
          "规划作者 + 独立规划复核",
          "冻结材料后按每篇1–5条不同推理结构规划；检查目标、来源锚点与选页，改数字或改写不增加独立量。",
          "按该批冻结材料数触发；规划拒绝、读取未完成和接口失败分别计数，不计为成功出题。",
        ),
      ],
    },
    {
      id: "construction",
      title: "构造目标与完整题解",
      subtitle: "先锁定答案和完整推导，再形成题面；研究生长使用独立版本。",
      stages: [
        stage(
          "result-lock",
          "目标、Golden 与模型前提锁定",
          "作者提出 / 程序绑定",
          "定位具体机制，先建立候选答案、全部输出义务、模型前提与完整参考解；来源启发模式明确标注新构造。",
          "锁定后不能静默更换金标；错误锚点返回上游或拒绝。",
          known
            ? {
                status: "recorded",
                summary: "已有锁定结果记录；锁定本身不是科学正确性审批。",
                evidence: known,
              }
            : {},
        ),
        reviewed(
          "proof-review",
          "完整推导逐步科学复核",
          "独立题解审查",
          "逐条核查等价变换、分支、参数域、边界、可达到性、全部输出和金标，记录具体问题与修复要求。",
          "完整推导必须独立复核；advisory 来源差异仅作提示，科学结论仍是硬门禁。",
          blueprint,
          blueprintChecks,
        ),
        reviewed(
          "proposal-challenge",
          "锁定前构造难度对抗",
          "独立短解挑战",
          "对新首题的临时提案寻找最短正确路线、熟悉套路和浅目标；需要更强构造时使用新 proposal 身份并重新科学复核。",
          "仅新首题且配置启用时执行；已有GLM成绩或已进verifier的版本禁止据此加难。",
          proposal?.challenge,
          proposal?.challenge
            ? [
                {
                  label: "短解路线核定",
                  verdict:
                    proposal.challenge.route_validity === "verified"
                      ? "pass"
                      : proposal.challenge.route_validity === "uncertain"
                        ? "uncertain"
                        : "fail",
                  detail: proposal.challenge.reason,
                },
                {
                  label: "构造深度挑战",
                  verdict:
                    proposal.challenge.verdict === "sufficient"
                      ? "pass"
                      : proposal.challenge.verdict === "uncertain"
                        ? "uncertain"
                        : "fail",
                  detail: proposal.challenge.reference_comparison,
                },
              ]
            : [],
          !proposalAdversary &&
            (rst || settings.require_difficulty_adversary === false)
            ? {
                status: "not_applicable",
                summary: "当前冻结配置未启用该首题构造对抗分支。",
              }
            : {},
        ),
        reviewed(
          "rst-growth",
          "RST 必要依赖增量审查",
          "独立生长审查",
          "比较父子完整题解，核定父机制保留、新依赖确被新目标使用、不是改数字或堆子问，并检查最短正确路线。",
          "只有科学有效父题可生长；每版新身份、新目标/金标，旧父题与旧成绩保留。",
          c.rst_origin?.growth_audit,
          c.rst_origin?.growth_audit
            ? [
                boolCheck(
                  "保留父核心",
                  c.rst_origin.growth_audit.parent_core_preserved,
                ),
                boolCheck(
                  "新增依赖必要",
                  c.rst_origin.growth_audit.necessary_new_dependency,
                ),
                boolCheck(
                  "实质变化",
                  c.rst_origin.growth_audit.substantive_change,
                ),
                boolCheck("紧凑目标", c.rst_origin.growth_audit.compact_target),
              ]
            : [],
          !child
            ? {
                status: "not_applicable",
                summary: rst
                  ? "当前是 R0 基线；必要增量审查适用于后续子题。"
                  : "当前不是 RST 生长分支。",
              }
            : {},
        ),
        burdenStage("proposal", c.rst_origin?.non_regression, child),
      ],
    },
    {
      id: "question",
      title: "题面形成与前置质检",
      subtitle: "题面必须自包含，同时保留必要的独立前提并隐藏可恢复的推理。",
      stages: [
        stage(
          "structure",
          "结构、来源证据与输出合同",
          "严格模型 + 程序检查",
          "校验结构、分类表、必要定义、来源登记、引用身份、题面与推导答案一致、required_outputs和前提充分性；来源启发差异仅作溯源提示。",
          "未登记来源、分类/合同错误或缺信息时阻断；Pydantic可解析不能代替全套结构/证据门禁。",
          {
            status: "observed",
            summary:
              "当前候选已通过读取结构校验；未单独保存的结构/证据阶段收据不补判为通过。",
            evidence: {
              source_fidelity: c.source_fidelity,
              source_notes: c.source_notes,
              required_outputs: c.item.required_outputs,
              assumptions: c.item.assumptions,
            },
          },
        ),
        reviewed(
          "item-review",
          "题面综合独立审查",
          "综合审查模型",
          "检查有效、自包含、唯一可答、金标有据、实质推理、分类准确，以及是否泄露答案或存在旁路。",
          "任何必要项目未通过都阻止后续采样；分类失败不等于科学反例。",
          c.audit,
          Object.entries(auditNames).map(([key, name]) =>
            boolCheck(name, c.audit[key]),
          ),
          post
            ? {
                status: "missing",
                checks: [],
                evidence: null,
                summary:
                  "当前 audit 可能由后置复核写回；未单独保留最初综合审查结果，不能把后审当作前审证据。",
              }
            : { summary: c.audit.reason },
        ),
        reviewed(
          "bottleneck",
          "最难步骤选择、隐藏与可恢复性",
          "独立挖空审查",
          "核查所选步骤是否为完整证明中最难且必要的推理、是否真正隐藏、可从原始题设恢复，避免只藏答案或保留捷径。",
          "五项分别检查；新增的一步未必是全证明最难的一步，不能按步骤数量替代审查。",
          c.bottleneck_assessment?.review,
          Object.entries(maskNames).map(([key, name]) =>
            verdictCheck(name, c.bottleneck_assessment?.review?.[key]),
          ),
        ),
        burdenStage("question", c.rst_origin?.non_regression, child),
        stage(
          "mask-challenge",
          "题面后的盲短解与难度挑战",
          "独立题面挑战",
          "只针对可推导提示和挖空方式挑战最短正确路线、剩余必要发现、泄露与旁路；结果锁定后不换金标。",
          "按冻结配置限制修订；RST由真实GLM测绝对难度，必要桥梁与净负担仍为硬门禁。",
          c.difficulty_rounds.length
            ? {
                status: "recorded",
                summary: `保留 ${c.difficulty_rounds.length} 轮题面对抗历史，请展开逐轮证据。`,
                evidence: c.difficulty_rounds,
              }
            : !proposalAdversary &&
                (rst || settings.require_difficulty_adversary === false)
              ? {
                  status: "not_applicable",
                  summary:
                    "当前冻结配置未启用此首题题面难度对抗分支；其他题面质检照常执行。",
                }
              : {},
        ),
      ],
    },
    {
      id: "correctness",
      title: "独立解答、自洽与精确验算",
      subtitle: "盲解、科学核对、公式翻译和程序执行是不同阶段，分别记录结果。",
      stages: [
        reviewed(
          "blind-solve",
          "只看题面的独立盲推导",
          "独立解题调用",
          "仅发送实际题面，不提供金标、完整参考解或历史通过率；独立推导答案，记录无效/不确定与疑点。",
          "盲解不确定或解不出不自动证明题目错误，也不能当作GLM难度答错。",
          independent,
          independentChecks,
        ),
        reviewed(
          "consistency-review",
          "题面、定义、金标与物理条件核对",
          "另一科学审查调用",
          "结合独立解答，逐项核查前提、定义、参数域、分支边界、金标输出与物理适用条件。",
          "科学错误或不确定阻断；多个模型意见一致不能替代证明。",
          cs?.audit,
          Object.entries(coreNames).map(([key, name]) =>
            verdictCheck(name, cs?.audit?.[key]),
          ),
        ),
        reviewed(
          "certificate-translation",
          "验算公式翻译与锚点核对",
          "独立公式翻译调用",
          "核对受限算术证书是否忠实翻译原题与金标；锚定原题片段、金标片段、目标输出与合法域条件，不能用自证恒等式替代反代。",
          "翻译失败只定向修证书，不改题面/Golden/推导；修订按该批冻结上限，真实反例不能被抹去。",
          certificate,
          certificate
            ? [
                boolCheck(
                  "忠实翻译原题与金标",
                  certificate.faithful_translation,
                  certificate.reason,
                ),
              ]
            : [],
        ),
        stage(
          "exact-substitution",
          "受限精确反代与输出覆盖",
          "程序精确算术检查器",
          "执行证书中的合法参数点、域条件、等式/不等式与输出覆盖；不执行模型生成代码。",
          "有限点可以证伪，不能证明全域、唯一性或连续全局最优；unsupported/未覆盖不是科学反例。",
          report
            ? {
                status:
                  priorCounterexample || report.verdict === "fail"
                    ? "failed"
                    : report.verdict === "unsupported" ||
                        report.uncovered_outputs.length
                      ? "uncovered"
                      : report.verdict === "not_applicable"
                        ? "not_applicable"
                        : "recorded_pass",
                summary: priorCounterexample
                  ? "历史证书保留了忠实原式的真实反例，后续结果不能覆盖该失败。"
                  : report.reason,
                checks: report.results.map((r) => ({
                  label: r.name,
                  verdict: r.verdict,
                  detail: r.reason,
                })),
                evidence: {
                  substitution: report,
                  certificate_attempts: cs.certificate_attempts,
                  preflight_repair: c.preflight_repair,
                },
              }
            : {},
        ),
      ],
    },
    {
      id: "verifier",
      title: "冻结版本与 Verifier 对抗",
      subtitle:
        "前置检查完成后进入 verifier；进入后只能修 verifier 或补足对照。",
      stages: [
        stage(
          "freeze",
          "冻结题面、Golden 与完整推导",
          "程序版本合同",
          "记录进入verifier时的题面哈希与上下文绑定，锁住题面、答案和完整推导，修订只作用于verifier/对照。",
          "出现真实题面、证明或金标缺陷拒绝当前版本，另建新版本重做前置质检。",
          va
            ? {
                status: freeze ? "recorded" : "uncovered",
                summary: freeze
                  ? `已保存冻结题面哈希；verifier历史 ${va.versions.length} 版。`
                  : "已进入verifier，但未记录冻结哈希；不能推断符合当前冻结协议。",
                evidence: {
                  frozen_item_sha256: freeze,
                  context_sha256: va.context_sha256,
                  protocol_sha256: va.protocol_sha256,
                },
              }
            : {},
        ),
        stage(
          "verifier-controls",
          "Golden、合法等价与错误近邻对照",
          "对照作者 + 独立 Oracle",
          "构造锁定Golden、合法等价表达与错误近邻；由独立oracle核定各对照真值，检查全部输出、条件与正负样例覆盖。",
          "对照不足先补足，不当成题目科学错误；原案例不能删改以规避检查。",
          result
            ? {
                status: "recorded",
                summary: `保存 ${observations.length} 个执行对照，${result.control_attempts.length} 次对照集尝试。对照生成记录本身不是通过结论。`,
                evidence: {
                  challenges: result.challenges,
                  oracle: result.oracle,
                  control_attempts: result.control_attempts,
                },
              }
            : {},
        ),
        stage(
          "verifier-adversary",
          "执行 Verifier 并交叉核对 HLE 判分",
          "Verifier + 固定HLE判分 + 独立复核",
          "比较oracle、verifier与官方HLE判分；检查误杀/漏检、全部输出覆盖、题目与verifier一致及无隐藏额外条件。",
          "误杀/漏检只定向修verifier，按冻结上限（新协议至多两次）；HLE冲突、不确定、重验证未完或修订用尽均不采样。",
          va
            ? {
                status:
                  va.status === "rejected"
                    ? "failed"
                    : va.status === "pending" ||
                        finalVerifier?.revalidation_pending
                      ? "incomplete"
                      : q.verifier_passed
                        ? "recorded_pass"
                        : "uncovered",
                summary:
                  va.reason ||
                  "展示verifier已有执行证据；当前有效性另由版本凭证校验。",
                checks: [
                  ...agreementChecks,
                  ...(result
                    ? [
                        verdictCheck(
                          "题目与verifier一致",
                          result.review.question_verifier_consistent,
                        ),
                        verdictCheck(
                          "全部输出已覆盖",
                          result.review.requested_outputs_covered,
                        ),
                        verdictCheck(
                          "无隐藏额外条件",
                          result.review.no_hidden_extra_conditions,
                        ),
                      ]
                    : []),
                ],
                evidence: va,
              }
            : {},
        ),
      ],
    },
    {
      id: "sampling",
      title: "真实难度采样与分流",
      subtitle: "所有前置科学门禁与冻结 verifier 通过后才采购 GLM 样本。",
      stages: [
        stage(
          "early-sampling",
          "GLM 首轮 4 次盲答与判分",
          "GLM5.2 + 官方HLE / 6Luna judge",
          "只看题面、无工具/联网，600秒墙钟含重试，不设客户端输出token上限；答案由官方HLE模板独立判分。",
          "首4次全对时普通生产直接拒绝；RST研究可作过易父题，以新身份生长，不计交付。",
          {
            status: earlyExplicitlyDisabled
              ? "not_applicable"
              : samplesBroken
                ? "uncovered"
                : firstComplete
                  ? firstCorrect === 4
                    ? "halted"
                    : "recorded"
                  : total
                    ? "incomplete"
                    : "missing",
            summary: earlyExplicitlyDisabled
              ? "该批冻结配置禁用4次早筛，按完整样本协议执行。"
              : samplesBroken
                ? "样本身份或判分记录异常，不输出首轮结论，也不将缺失记录当作答错。"
                : firstComplete
                  ? `首4次正确 ${firstCorrect}/4。${firstCorrect === 4 ? (rst ? "RST研究父题；当前版本不计交付。" : settings.difficulty_policy === "ease_only" ? "普通生产过易拒绝，不继续加难。" : "按原冻结历史协议分流，不追溯改判。") : "出现错答或有凭证的解题超时，按协议补足16次。"}`
                  : total
                    ? "首轮样本尚未完整或记录异常，不生成4次结论。"
                    : "当前无采样记录；未测不等于答错。",
            evidence: {
              answered: q.answered,
              deadlines: q.deadlines,
              samples: total,
              correct,
              solver_requests: c.trials.map((t) => t.solver_request),
            },
          },
        ),
        stage(
          "full-sampling",
          "补足 16 次与难度区间判定",
          "GLM5.2 + 固定HLE判分",
          "按协议补足16个独立样本；有收据的解题600秒墙钟超时计错且单列，接口/截断/空输出/JSON/判分失败不当答错。",
          "1–8/16进入后审去重；9–16/16普通生产拒绝；0/16先科学复核，再按普通生产降难或RST停止生长。",
          {
            status: fullComplete
              ? correct >= 1 && correct <= 8
                ? "recorded"
                : "halted"
              : samplesBroken
                ? "uncovered"
                : total
                  ? "incomplete"
                  : "missing",
            summary: fullComplete
              ? `完整记录 ${correct}/16；${correct === 0 ? "零正确，先做科学复核，不能当作加难成功。" : correct <= 8 ? "处于难度候选区间，仍需后置复核、去重与交付检查。" : rst ? "过易研究父题，不能作为合格交付。" : "高于目标正确数上限，普通生产拒绝。"}`
              : `已有 ${q.answered} 份答卷、${q.deadlines} 份解题超时，共 ${total}/16 条记录；不输出完整难度结论。`,
            evidence: {
              samples: total,
              correct,
              sample_error: samplesBroken,
              deadline_outcomes: c.deadline_outcomes,
            },
          },
        ),
      ],
    },
    {
      id: "delivery",
      title: "后置复核、独立性与交付",
      subtitle:
        "难度进入区间只是候选条件；pilot 初筛与 production 留存分别统计。",
      stages: [
        stage(
          "post-review",
          "采样后科学与判分歧义复核",
          "独立后置审查调用",
          "复核原题、Golden、完整推导及全部已判答卷；挑战合法等价答案、约定不明、金标错误、物理反例与judge抽取/评分歧义。",
          "后审失败拒绝；判分错误与题目科学错误分别归因，不静默修改历史答卷。",
          post
            ? {
                status:
                  post.passed === true
                    ? "recorded_pass"
                    : post.passed === false
                      ? "failed"
                      : "recorded",
                summary:
                  "该候选已有独立后审阶段事件；不从当前综合audit反推后审已执行。",
                evidence: { event: post, current_audit: c.audit },
              }
            : {},
        ),
        stage(
          "dedup",
          "批次语义去重与推理结构独立性",
          "程序字面检查 + 语义去重调用",
          "比较同批及配置的参考题，核查同root/同material与语义重复；同论文的不同题必须有不同推理结构。",
          "改数字/改写不增加独立量；是否对外部题库去重以该批冻结配置为准。不要从最终状态拆造每项独立审批。",
          {
            summary: finished
              ? "存在候选终态事件，但没有独立去重回执字段；本阶段保持未知。"
              : "未提供独立去重阶段回执，不从得分或终态推断已通过。",
            evidence: finished || null,
          },
        ),
        stage(
          "delivery-contract",
          "生产配额、独立根与导出合同",
          "生产准入 / 导出程序",
          "核对完整科学门禁、难度区间、后审、批次去重、root/material独立性和配额；导出时验证审批仍绑定当前版本并保存冻结清单。",
          "pilot只记screened；只有production且全部准入条件满足才可accepted。未运行导出/发布不标为已交付。",
          {
            status:
              c.status === "accepted" || c.status === "screened"
                ? "recorded"
                : "missing",
            summary: `当前状态 ${c.status}；${context.mode === "pilot" ? "该run为pilot，初筛不算生产交付。" : context.mode === "production" ? "该run为production，保留原准入状态，不由页面重新放行。" : "运行模式未记录。"} 当前页面未提供导出/发布收据。`,
            evidence: {
              mode: context.mode,
              status: c.status,
              reasons: c.reasons,
              root_id: c.root_id,
              run_id: c.run_id,
            },
          },
        ),
      ],
    },
  ];
  const metadata = {
    "proof-review": [
      ["审查请求", c.blueprint_request],
      ["实际模型", c.blueprint_actual_model],
    ],
    bottleneck: [
      ["审查请求", c.bottleneck_assessment?.request_id],
      ["实际模型", c.bottleneck_assessment?.actual_model],
    ],
    "blind-solve": [
      ["独立解题请求", cs?.independent_request],
      ["实际模型", cs?.independent_actual_model],
    ],
    "consistency-review": [
      ["科学复核请求", cs?.review_request],
      ["实际模型", cs?.review_actual_model],
    ],
    "certificate-translation": [
      ["翻译请求", cs?.certificate_request],
      ["实际模型", cs?.certificate_actual_model],
    ],
    "rst-growth": [["增量审查请求", c.rst_origin?.growth_audit_request]],
    "rst-proposal": [
      ["净负担请求", c.rst_origin?.non_regression?.proposal_request],
      ["实际模型", c.rst_origin?.non_regression?.proposal_actual_model],
    ],
    "rst-question": [
      ["净负担请求", c.rst_origin?.non_regression?.question_request],
      ["实际模型", c.rst_origin?.non_regression?.question_actual_model],
    ],
    "post-review": [
      ["后审请求", post?.request_id],
      ["实际模型", post?.actual_model],
    ],
  };
  for (const group of groups)
    for (const item of group.stages) {
      item.metadata = (metadata[item.id] || []).filter(([, value]) => value);
    }
  return {
    protocol: protocolLabel,
    groups,
    stages: groups.flatMap((g) => g.stages),
    events,
  };
}
