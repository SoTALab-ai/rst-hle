import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(StrictModel):
    id: str
    title: str
    uri: str
    kind: Literal["paper", "primary", "rulebook", "synthetic"] = "paper"
    license: str = "unspecified"
    text: str = Field(min_length=100)
    sha256: str = ""
    retrieved_at: str = ""

    @model_validator(mode="after")
    def seal(self):
        actual = hashlib.sha256(self.text.encode()).hexdigest()
        if self.sha256 and self.sha256 != actual:
            raise ValueError("Source content hash mismatch")
        self.sha256 = actual
        return self


class Evidence(StrictModel):
    source_id: str
    locator: str = Field(min_length=1)
    quote: str = Field(min_length=20, max_length=2000)
    supports: str = Field(min_length=10)


class InferenceStep(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    depends_on: list[str]
    inference: str = Field(min_length=15)


class KnownResult(StrictModel):
    source_id: str
    locator: str = Field(min_length=3)
    statement: str = Field(min_length=20)
    target: str = Field(min_length=10)
    answer: str = Field(min_length=1)
    assumptions: list[str] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


class ResultSelection(StrictModel):
    outcome: Literal["found", "unavailable"]
    result: KnownResult | None
    reason: str = Field(min_length=10)

    @model_validator(mode="after")
    def selected_result(self):
        if (self.outcome == "found") != (self.result is not None):
            raise ValueError("Found requires a result; unavailable must not invent one")
        return self


class BottleneckMask(StrictModel):
    step_id: str
    proof_step_indices: list[Annotated[int, Field(ge=1)]] = Field(min_length=1)
    hardest_reason: str = Field(min_length=30)
    alternative_steps: list[str] = Field(min_length=1)
    hidden_information: list[str] = Field(min_length=1)
    retained_information: list[str] = Field(min_length=1)
    recoverability: str = Field(min_length=30)

    @model_validator(mode="after")
    def unique_indices(self):
        if len(set(self.proof_step_indices)) != len(self.proof_step_indices):
            raise ValueError("Masked proof indices must be unique")
        return self


class DifficultyDesign(StrictModel):
    pattern_ids: list[str] = Field(min_length=1, max_length=2)
    skills: list[str] = Field(min_length=2, max_length=3)
    skill_bridge: str = Field(min_length=20)
    steps: list[InferenceStep] = Field(min_length=3, max_length=8)
    bottleneck_step: str
    baseline_shortcut: str = Field(min_length=20)
    shortcut_counterexample: str = Field(min_length=20)
    easier_control: str = Field(min_length=20)
    mask: BottleneckMask | None = None

    @model_validator(mode="after")
    def connected_reasoning(self):
        # 只检查申报的推理图结构，不能据此证明题目真的难或数学正确。
        prior = set()
        for step in self.steps:
            if step.id in prior or len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError("Inference steps and dependencies must be unique")
            if not set(step.depends_on) <= prior:
                raise ValueError("Dependencies must reference earlier inference steps")
            prior.add(step.id)
        needed = {self.steps[-1].id}
        for step in reversed(self.steps):
            if step.id in needed:
                needed.update(step.depends_on)
        if needed != prior or self.bottleneck_step not in needed:
            raise ValueError("All steps and the bottleneck must contribute to the final target")
        if len(set(self.skills)) != len(self.skills) or len(set(self.pattern_ids)) != len(self.pattern_ids):
            raise ValueError("Skills and pattern IDs must be distinct")
        return self


class TaskDesign(StrictModel):
    source_claim: str = Field(min_length=20)
    claim_locator: str = Field(min_length=3)
    target: str = Field(min_length=10)
    underlying_conditions: list[str] = Field(min_length=1)
    hidden_steps: list[str] = Field(min_length=1)
    inferable_conditions: list[str]
    preserved_invariants: list[str] = Field(min_length=1)
    inverse_construction: str = Field(min_length=20)
    answer_check: str = Field(min_length=20)
    difficulty: DifficultyDesign | None = None


class Solution(StrictModel):
    """Created before the question, so truth isn't inferred from solver votes."""
    topic: str
    assumptions: list[str] = Field(min_length=1)
    derivation: list[str] = Field(min_length=2)
    answer: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    reasoning_delta: str = Field(min_length=20)
    design: TaskDesign | None = None


class BlueprintIssue(StrictModel):
    part: Literal["derivation", "answer", "assumptions", "source", "difficulty_design", "known_result"]
    step_index: int | None = Field(default=None, ge=1)
    claim: str = Field(min_length=10)
    evidence: str = Field(min_length=10)
    repair_requirement: str = Field(min_length=10)


class AssumptionCorrespondence(StrictModel):
    source_assumption: str
    solution_assumption: str
    equivalent: bool
    reason: str = Field(min_length=20)


class BlueprintAudit(StrictModel):
    valid: bool
    answer_correct: bool
    derivation_complete: bool
    source_grounded: bool
    reason: str = Field(min_length=10)
    reviewed_step_count: int | None = Field(default=None, ge=1)
    issues: list[BlueprintIssue] = Field(default_factory=list)
    assumption_correspondence: list[AssumptionCorrespondence] = Field(default_factory=list)

    def passed(self):
        return (self.valid and self.answer_correct and self.derivation_complete and self.source_grounded
                and not self.issues)


class StepwiseBlueprintAudit(BlueprintAudit):
    reviewed_step_count: int = Field(ge=1)
    issues: list[BlueprintIssue]

    @model_validator(mode="after")
    def actionable_issues(self):
        if not self.passed() and not self.issues:
            raise ValueError("A rejected proof must include actionable issues")
        for issue in self.issues:
            if issue.part == "derivation" and issue.step_index is None:
                raise ValueError("Derivation issues must identify a one-based step index")
            if issue.step_index is not None and issue.step_index > self.reviewed_step_count:
                raise ValueError("Issue step index exceeds the reviewed proof")
        return self


class Question(StrictModel):
    question: str = Field(min_length=50)
    answer: str = Field(min_length=1)
    answer_type: Literal["exactMatch", "multipleChoice"]
    rationale: str = Field(min_length=50)
    category: str
    subtopic: str
    material_key: str = Field(min_length=5)
    required_outputs: list[str] = Field(min_length=1)
    assumptions: list[str] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


class Audit(StrictModel):
    valid: bool
    self_contained: bool
    uniquely_answerable: bool
    answer_supported: bool
    substantive_delta: bool
    category_correct: bool
    no_shortcut: bool
    reason: str = Field(min_length=10)

    def passed(self) -> bool:
        return all(getattr(self, k) for k in type(self).model_fields if k != "reason")


class IndependentSolution(StrictModel):
    outcome: Literal["solved", "invalid", "uncertain"]
    derivation: list[str] = Field(min_length=2)
    answer: str = Field(min_length=1)
    issues: list[str]


class ConsistencyCheck(StrictModel):
    verdict: Literal["pass", "fail", "uncertain", "not_applicable"]
    reasoning: str = Field(min_length=10)
    witness: str = Field(min_length=10)


class BottleneckReview(StrictModel):
    step_id: str
    selection_supported: ConsistencyCheck
    hidden: ConsistencyCheck
    necessary: ConsistencyCheck
    recoverable: ConsistencyCheck
    not_answer_only: ConsistencyCheck

    @model_validator(mode="after")
    def no_skipped_checks(self):
        if any(getattr(self, name).verdict == "not_applicable" for name in self.check_names()):
            raise ValueError("Bottleneck checks cannot be skipped")
        return self

    @staticmethod
    def check_names():
        return ("selection_supported", "hidden", "necessary", "recoverable", "not_answer_only")

    def passed(self):
        return all(getattr(self, name).verdict == "pass" for name in self.check_names())


class BottleneckAssessment(StrictModel):
    input_sha256: str
    prompt_sha256: str
    model_config_sha256: str
    review: BottleneckReview
    request_id: str
    actual_model: str


class SubstitutionBinding(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    expression: str = Field(min_length=1, max_length=3000)


class SubstitutionRelation(StrictModel):
    lhs: str = Field(min_length=1, max_length=3000)
    relation: Literal["equal", "not_equal", "greater", "greater_equal", "less", "less_equal"]
    rhs: str = Field(min_length=1, max_length=3000)


class SubstitutionCase(SubstitutionRelation):
    name: str = Field(min_length=1, max_length=160)
    target_output: str
    question_anchor: str = Field(min_length=5)
    answer_anchor: str = Field(min_length=1)
    domain_justification: str = Field(min_length=10)
    bindings: list[SubstitutionBinding] = Field(max_length=64)
    domain_conditions: list[SubstitutionRelation] = Field(min_length=1, max_length=20)


class SubstitutionResult(StrictModel):
    name: str
    target_output: str
    verdict: Literal["pass", "fail", "unsupported"]
    lhs_value: str
    rhs_value: str
    reason: str
    domain_results: list[str] = Field(default_factory=list)


class SubstitutionReport(StrictModel):
    verdict: Literal["pass", "fail", "unsupported", "not_applicable"]
    results: list[SubstitutionResult]
    uncovered_outputs: list[str]
    reason: str

    def passed(self):
        return self.verdict in ("pass", "not_applicable")


class ConsistencyAudit(StrictModel):
    premises: ConsistencyCheck
    definitions: ConsistencyCheck
    parameter_domain: ConsistencyCheck
    reference_answer: ConsistencyCheck
    physical_regime: ConsistencyCheck
    task_kind: Literal["computational", "proof", "noncomputational"]
    substitution_checks: list[SubstitutionCase] = Field(max_length=64)

    @model_validator(mode="after")
    def applicable_core_checks(self):
        for name in ("premises", "definitions", "parameter_domain", "reference_answer"):
            if getattr(self, name).verdict == "not_applicable":
                raise ValueError("Core correctness checks cannot be skipped")
        return self

    def passed(self) -> bool:
        return (all(getattr(self, name).verdict == "pass" for name in
                    ("premises", "definitions", "parameter_domain", "reference_answer"))
                and self.physical_regime.verdict in ("pass", "not_applicable"))

    def failures(self) -> list[str]:
        return [name + ":" + check.verdict + ":" + check.reasoning
                for name in ("premises", "definitions", "parameter_domain", "reference_answer", "physical_regime")
                if (check := getattr(self, name)).verdict in ("fail", "uncertain")]


class CertificateAudit(StrictModel):
    faithful_translation: bool
    reason: str = Field(min_length=10)
    checks: list[SubstitutionCase] = Field(max_length=64)


class CertificateAnchorPatch(StrictModel):
    check_index: int = Field(ge=0, le=63)
    question_anchor: str = Field(min_length=5)
    answer_anchor: str = Field(min_length=1)
    target_output_index: int = Field(ge=0)


class CertificateAnchors(StrictModel):
    patches: list[CertificateAnchorPatch] = Field(min_length=1, max_length=64)


class ConsistencyAssessment(StrictModel):
    protocol: Literal["preflight-v1"] = "preflight-v1"
    input_sha256: str
    prompt_hashes: dict[str, str]
    model_config_sha256: str
    independent_solution: IndependentSolution
    audit: ConsistencyAudit
    independent_request: str
    review_request: str
    independent_actual_model: str
    review_actual_model: str
    substitution: SubstitutionReport
    certificate_audit: CertificateAudit | None = None
    certificate_prompt_sha256: str = ""
    certificate_request: str = ""
    certificate_actual_model: str = ""
    certificate_anchor_request: str = ""
    certificate_anchor_actual_model: str = ""
    certificate_anchor_prompt_sha256: str = ""
    verifier_sha256: str = ""

    def passed(self):
        return (self.audit.passed() and self.substitution.passed()
                and self.certificate_audit is not None and self.certificate_audit.faithful_translation)


class Judgment(StrictModel):
    extracted_final_answer: str
    reasoning: str
    correct: Literal["yes", "no"]
    confidence: int = Field(ge=0, le=100)
    strict: Literal[True]


class Submission(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    item: Question
    solution: Solution
    parent_id: str | None = None


class DedupVerdict(StrictModel):
    duplicate_ids: list[str]
    reason: str


class Record(StrictModel):
    id: str
    question: str
    material_key: str = ""


class Trial(StrictModel):
    sample_id: int
    response: str
    solver_request: str
    judge_request: str
    solver_actual_model: str
    judge_actual_model: str
    judgment: Judgment


class DeadlineOutcome(StrictModel):
    """解题墙钟超时按协议计错；没有答卷，也没有模型judge收据。"""

    sample_id: int = Field(ge=0, le=15)
    solver_request: str
    requested_model: str
    reason: Literal["deadline_exceeded"] = "deadline_exceeded"
    deadline_seconds: float = Field(gt=0, allow_inf_nan=False)
    elapsed_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def reached_deadline(self):
        if self.elapsed_seconds < self.deadline_seconds:
            raise ValueError("A deadline outcome requires the full wall-clock deadline")
        return self


class VerifierRequirement(StrictModel):
    target_output: str
    question_anchor: str = Field(min_length=5)
    acceptance_rule: str = Field(min_length=15)
    equivalent_forms: list[str] = Field(min_length=1)


class VerifierCondition(StrictModel):
    condition: str = Field(min_length=5)
    kind: Literal["explicit", "inferable", "requested"]
    question_anchor: str = Field(min_length=5)
    justification: str = Field(min_length=15)


class VerifierSpec(StrictModel):
    requirements: list[VerifierRequirement] = Field(min_length=1, max_length=20)
    conditions: list[VerifierCondition] = Field(min_length=1, max_length=40)
    interpretation: str = Field(min_length=20)


class VerifierCase(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    kind: Literal["equivalent", "invalid"]
    response: str = Field(min_length=1, max_length=12000)
    target_output_index: int = Field(ge=0, le=19)
    rationale: str = Field(min_length=15)


class VerifierChallenges(StrictModel):
    cases: list[VerifierCase] = Field(min_length=4, max_length=12)
    suspected_question_issues: list[str]

    @model_validator(mode="after")
    def distinct_cases(self):
        if len({c.id for c in self.cases}) != len(self.cases) or any(c.id == "golden" for c in self.cases):
            raise ValueError("Challenge ids must be distinct; golden is reserved")
        if len({c.response for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate challenge answers do not add adversarial coverage")
        return self


class VerifierOracleCase(StrictModel):
    id: str
    verdict: Literal["yes", "no", "uncertain"]
    reasoning: str = Field(min_length=15)


class VerifierOracle(StrictModel):
    cases: list[VerifierOracleCase]
    question_complete: ConsistencyCheck
    target_unambiguous: ConsistencyCheck
    golden_valid: ConsistencyCheck
    hidden_steps_recoverable: ConsistencyCheck


class VerifierDecision(StrictModel):
    correct: Literal["yes", "no", "uncertain"]
    reasoning: str = Field(min_length=15)


class VerifierObservation(StrictModel):
    case_id: str
    oracle: VerifierOracleCase
    verifier: VerifierDecision
    verifier_request: str
    verifier_actual_model: str
    hle: Judgment
    hle_request: str
    hle_actual_model: str


class VerifierReview(StrictModel):
    action: Literal["pass", "repair_verifier", "repair_question", "reject"]
    question_verifier_consistent: ConsistencyCheck
    requested_outputs_covered: ConsistencyCheck
    no_hidden_extra_conditions: ConsistencyCheck
    feedback: str = Field(min_length=20)


class VerifierRound(StrictModel):
    challenges: VerifierChallenges
    oracle: VerifierOracle
    observations: list[VerifierObservation]
    review: VerifierReview
    requests: dict[str, str]
    actual_models: dict[str, str]


class VerifierAnchorRepair(StrictModel):
    group: Literal["requirements", "conditions"]
    index: int = Field(ge=0)
    original: str
    literal: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    method: str


class VerifierVersion(StrictModel):
    item: Question
    spec: VerifierSpec
    spec_request: str
    spec_actual_model: str
    spec_raw_sha256: str = ""
    anchor_repairs: list[VerifierAnchorRepair] = Field(default_factory=list)
    # 题面修复后先存检查点；恢复时必须完成全部前置复核，不能跳过。
    revalidation_pending: bool = False
    question_repair_request: str = ""
    question_repair_actual_model: str = ""
    result: VerifierRound | None = None


class VerifierAssessment(StrictModel):
    protocol: Literal["adversarial-verifier-v1"] = "adversarial-verifier-v1"
    context_sha256: str
    protocol_sha256: str
    versions: list[VerifierVersion] = Field(min_length=1, max_length=3)
    status: Literal["pending", "passed", "rejected"] = "pending"
    reason: str = ""


class VerifierQuestionRepair(StrictModel):
    question: str = Field(min_length=50)
    assumptions: list[str] = Field(min_length=1)
    rationale: str = Field(min_length=50)
    change_reason: str = Field(min_length=20)


class Candidate(StrictModel):
    id: str
    parent_id: str | None
    root_id: str
    generation: int
    operator: str
    source_ids: list[str]
    solution: Solution
    item: Question
    audit: Audit
    consistency: ConsistencyAssessment | None = None
    verifier_assessment: VerifierAssessment | None = None
    verifier_history: list[VerifierAssessment] = Field(default_factory=list)
    blueprint_audit: BlueprintAudit | None = None
    blueprint_request: str = ""
    blueprint_actual_model: str = ""
    blueprint_fingerprint: str = ""
    known_result: KnownResult | None = None
    bottleneck_assessment: BottleneckAssessment | None = None
    trials: list[Trial] = Field(default_factory=list)
    deadline_outcomes: list[DeadlineOutcome] = Field(default_factory=list)
    status: Literal["quality_seed", "rejected", "screened", "accepted"]
    reasons: list[str] = Field(default_factory=list)
    run_id: str
    authoring_complete: bool = True
    authoring_repairs_completed: int = Field(default=0, ge=0)
