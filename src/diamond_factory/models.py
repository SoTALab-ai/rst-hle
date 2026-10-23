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


class Solution(StrictModel):
    """Created before the question, so truth isn't inferred from solver votes."""
    topic: str
    assumptions: list[str] = Field(min_length=1)
    derivation: list[str] = Field(min_length=2)
    answer: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    reasoning_delta: str = Field(min_length=20)


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
    trials: list[Trial] = Field(default_factory=list)
    status: Literal["quality_seed", "rejected", "screened", "accepted"]
    reasons: list[str] = Field(default_factory=list)
    run_id: str
    authoring_complete: bool = True
    authoring_repairs_completed: int = Field(default=0, ge=0)
