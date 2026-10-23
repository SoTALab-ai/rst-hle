import hashlib
import json
from typing import Literal

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


class Solution(StrictModel):
    """Created before the question, so truth isn't inferred from solver votes."""
    topic: str
    assumptions: list[str] = Field(min_length=1)
    derivation: list[str] = Field(min_length=2)
    answer: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    reasoning_delta: str = Field(min_length=20)
    design: TaskDesign | None = None


class BlueprintAudit(StrictModel):
    valid: bool
    answer_correct: bool
    derivation_complete: bool
    source_grounded: bool
    reason: str = Field(min_length=10)

    def passed(self):
        return self.valid and self.answer_correct and self.derivation_complete and self.source_grounded


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


class SubstitutionBinding(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    expression: str = Field(min_length=1, max_length=3000)


class SubstitutionRelation(StrictModel):
    lhs: str = Field(min_length=1, max_length=3000)
    relation: Literal["equal", "not_equal", "greater", "greater_equal"]
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
    blueprint_audit: BlueprintAudit | None = None
    blueprint_request: str = ""
    blueprint_actual_model: str = ""
    trials: list[Trial] = Field(default_factory=list)
    status: Literal["quality_seed", "rejected", "screened", "accepted"]
    reasons: list[str] = Field(default_factory=list)
    run_id: str
    authoring_complete: bool = True
    authoring_repairs_completed: int = Field(default=0, ge=0)
