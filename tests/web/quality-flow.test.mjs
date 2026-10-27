import test from "node:test";
import assert from "node:assert/strict";
import { buildQualityFlow } from "../../src/diamond_factory/web/quality-flow.mjs";

function fixture() {
  return {
    summary: {
      id: "test",
      correct: null,
      samples: 0,
      answered: 0,
      deadlines: 0,
      sample_error: false,
      consistency_passed: false,
      verifier_passed: false,
      reasons: [],
    },
    candidate: {
      id: "test",
      source_ids: [],
      source_notes: [],
      source_fidelity: "advisory",
      generation: 0,
      solution: { derivation: ["first", "second"] },
      item: { required_outputs: ["result"], assumptions: ["premise"] },
      audit: {
        valid: true,
        self_contained: true,
        uniquely_answerable: true,
        answer_supported: true,
        substantive_delta: true,
        category_correct: true,
        no_shortcut: true,
        reason: "recorded review",
      },
      difficulty_rounds: [],
      trials: [],
      deadline_outcomes: [],
      status: "quality_seed",
      reasons: [],
      root_id: "test",
      run_id: "run",
    },
    sources: [],
    workflow: {
      mode: "pilot",
      settings: {
        synthesis_protocol: "standard",
        early_screen_samples: 4,
        difficulty_policy: "ease_only",
        require_difficulty_adversary: false,
      },
      events: [],
    },
  };
}
const stage = (data, id) =>
  buildQualityFlow(data).stages.find((s) => s.id === id);
const check = (verdict = "pass") => ({
  verdict,
  reasoning: "specific check evidence",
});

test("完整流程覆盖材料、科学检查、verifier、采样、后审与交付，顺序稳定", () => {
  const flow = buildQualityFlow(fixture());
  assert.equal(flow.groups.length, 7);
  assert.equal(flow.stages.length, 25);
  assert.equal(new Set(flow.stages.map((s) => s.id)).size, 25);
  const ids = flow.stages.map((s) => s.id);
  for (const [before, after] of [
    ["material-reading", "result-lock"],
    ["proof-review", "item-review"],
    ["blind-solve", "consistency-review"],
    ["certificate-translation", "exact-substitution"],
    ["freeze", "verifier-adversary"],
    ["verifier-adversary", "early-sampling"],
    ["full-sampling", "post-review"],
    ["post-review", "delivery-contract"],
  ])
    assert.ok(ids.indexOf(before) < ids.indexOf(after));
});
test("缺失收据不由综合审查、分数或终态补造", () => {
  const d = fixture();
  d.candidate.status = "accepted";
  for (const id of ["material-reading", "planning", "post-review", "dedup"])
    assert.equal(stage(d, id).status, "missing");
  assert.equal(stage(d, "delivery-contract").status, "recorded");
  assert.match(stage(d, "delivery-contract").summary, /未提供导出/);
});
test("advisory来源差异只提示，但推导步数或科学错误仍阻断", () => {
  const d = fixture();
  d.candidate.blueprint_audit = {
    valid: true,
    answer_correct: true,
    derivation_complete: true,
    source_grounded: false,
    reviewed_step_count: 2,
    issues: [{ part: "source", claim: "source-only difference" }],
    source_notes: [],
  };
  assert.equal(stage(d, "proof-review").status, "recorded_pass");
  d.candidate.source_fidelity = "strict";
  assert.equal(stage(d, "proof-review").status, "failed");
  d.candidate.source_fidelity = "advisory";
  d.candidate.blueprint_audit.reviewed_step_count = 1;
  assert.equal(stage(d, "proof-review").status, "failed");
});
test("最难步骤单项失败保留具体定位", () => {
  const d = fixture();
  d.candidate.bottleneck_assessment = {
    review: Object.fromEntries(
      [
        "selection_supported",
        "hidden",
        "necessary",
        "recoverable",
        "not_answer_only",
      ].map((k) => [k, check()]),
    ),
  };
  assert.equal(stage(d, "bottleneck").status, "recorded_pass");
  d.candidate.bottleneck_assessment.review.selection_supported = check("fail");
  assert.equal(stage(d, "bottleneck").status, "failed");
  assert.equal(
    stage(d, "bottleneck").checks.find((c) => c.label.includes("所选")).verdict,
    "fail",
  );
});
test("RST根题不套用子题净负担；六维任一削弱或不确定不通过", () => {
  const d = fixture();
  d.candidate.rst_origin = {};
  d.workflow.settings.synthesis_protocol = "rst_curriculum";
  assert.equal(stage(d, "rst-proposal").status, "not_applicable");
  d.candidate.generation = 1;
  assert.equal(stage(d, "rst-proposal").status, "missing");
  const axes = [
    "parameter_scope",
    "branch_coverage",
    "boundary_cases",
    "inference_information",
    "output_obligations",
    "shortest_solution",
  ];
  const review = {
    net_verdict: "no_offset",
    feedback: "specific burden comparison",
    comparisons: axes.map((axis) => ({
      axis,
      verdict: "strengthened",
      evidence: "preserved parent work",
    })),
  };
  d.candidate.rst_origin.non_regression = {
    proposal: structuredClone(review),
    question: structuredClone(review),
  };
  assert.equal(stage(d, "rst-proposal").status, "recorded_pass");
  d.candidate.rst_origin.non_regression.proposal.comparisons[0].verdict =
    "reduced";
  assert.equal(stage(d, "rst-proposal").status, "failed");
  d.candidate.rst_origin.non_regression.question.comparisons[5].verdict =
    "uncertain";
  assert.equal(stage(d, "rst-question").status, "uncertain");
});
test("盲解不确定、翻译失败、执行未覆盖与科学反例分别显示", () => {
  const d = fixture();
  d.candidate.consistency = {
    independent_solution: {
      outcome: "uncertain",
      issues: ["unresolved branch"],
    },
    certificate_audit: {
      faithful_translation: false,
      reason: "translation failed",
    },
    substitution: {
      verdict: "unsupported",
      uncovered_outputs: ["result"],
      results: [],
      reason: "unsupported expression",
    },
    certificate_attempts: [],
  };
  assert.equal(stage(d, "blind-solve").status, "uncertain");
  assert.equal(stage(d, "certificate-translation").status, "failed");
  assert.equal(stage(d, "exact-substitution").status, "uncovered");
  d.candidate.consistency.substitution.verdict = "fail";
  assert.equal(stage(d, "exact-substitution").status, "failed");
});
test("verifier pending或凭证失效不由旧passed状态覆盖", () => {
  const d = fixture();
  d.candidate.verifier_assessment = {
    status: "passed",
    versions: [{ revalidation_pending: false, result: null }],
    frozen_item_sha256: "hash",
  };
  assert.equal(stage(d, "verifier-adversary").status, "uncovered");
  d.summary.verifier_passed = true;
  assert.equal(stage(d, "verifier-adversary").status, "recorded_pass");
  d.candidate.verifier_assessment.versions[0].revalidation_pending = true;
  assert.equal(stage(d, "verifier-adversary").status, "incomplete");
});
test("4/4停在初筛，RST研究种子与普通生产拒绝分别说明", () => {
  const d = fixture();
  d.candidate.trials = Array.from({ length: 4 }, (_, sample_id) => ({
    sample_id,
    judgment: { correct: "yes" },
  }));
  Object.assign(d.summary, { samples: 4, answered: 4, correct: 4 });
  assert.equal(stage(d, "early-sampling").status, "halted");
  assert.match(stage(d, "early-sampling").summary, /普通生产过易拒绝/);
  assert.equal(stage(d, "full-sampling").status, "incomplete");
  d.candidate.rst_origin = {};
  assert.match(stage(d, "early-sampling").summary, /RST研究父题/);
  d.workflow.settings.early_screen_samples = 0;
  assert.equal(stage(d, "early-sampling").status, "not_applicable");
});
test("完整16次落带仍需后审，不把未知和异常样本记为答错", () => {
  const d = fixture();
  Object.assign(d.summary, {
    samples: 16,
    answered: 15,
    deadlines: 1,
    correct: 4,
  });
  assert.equal(stage(d, "full-sampling").status, "recorded");
  assert.match(stage(d, "full-sampling").summary, /仍需后置复核/);
  assert.equal(stage(d, "post-review").status, "missing");
  d.summary.sample_error = true;
  d.summary.correct = null;
  assert.equal(stage(d, "full-sampling").status, "uncovered");
});
test("后审只使用该候选独立阶段事件，不把覆写audit当成前审", () => {
  const d = fixture();
  d.workflow.events = [
    {
      event: "post_evaluation_audit",
      passed: false,
      request_id: "post-1",
      at: "2026-10-27T00:00:00Z",
    },
  ];
  assert.equal(stage(d, "post-review").status, "failed");
  assert.equal(stage(d, "item-review").status, "missing");
  assert.equal(stage(d, "item-review").checks.length, 0);
  assert.match(stage(d, "item-review").summary, /不能把后审当作前审/);
});

test("历史忠实反例不能被后一次反代通过覆盖", () => {
  const d = fixture();
  d.candidate.consistency = {
    substitution: {
      verdict: "pass",
      uncovered_outputs: [],
      results: [],
      reason: "latest pass",
    },
    certificate_attempts: [
      {
        certificate: { faithful_translation: true },
        substitution: { verdict: "fail" },
      },
    ],
  };
  assert.equal(stage(d, "exact-substitution").status, "failed");
  assert.match(stage(d, "exact-substitution").summary, /真实反例/);
});

test("异常采样不输出首4次结论", () => {
  const d = fixture();
  d.candidate.trials = Array.from({ length: 4 }, (_, sample_id) => ({
    sample_id,
    judgment: { correct: "yes" },
  }));
  Object.assign(d.summary, {
    samples: 4,
    answered: 4,
    correct: null,
    sample_error: true,
  });
  assert.equal(stage(d, "early-sampling").status, "uncovered");
  assert.doesNotMatch(stage(d, "early-sampling").summary, /首4次正确/);
});
