"""原始输入、推导依赖及实际题面负担的显式合同；不把图结构当成难度证明。"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DependencyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PrimitivePremise(DependencyModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    statement: str = Field(min_length=10)
    kind: Literal["definition", "model_law", "given_data", "convention"]
    source_locator: str = Field(min_length=3)
    independence_reason: str = Field(min_length=20)


class DerivedClaim(DependencyModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    statement: str = Field(min_length=10)
    depends_on: list[str] = Field(min_length=1)
    source_locator: str = Field(min_length=3)
    derivation: str = Field(min_length=20)


class PremiseContract(DependencyModel):
    primitives: list[PrimitivePremise] = Field(min_length=1)
    derived: list[DerivedClaim] = Field(min_length=1)
    exploration_ids: list[str] = Field(min_length=1)
    target_dependencies: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_dependency_graph(self):
        prior = {p.id for p in self.primitives}
        if len(prior) != len(self.primitives):
            raise ValueError("Primitive IDs must be unique")
        for claim in self.derived:
            if (claim.id in prior or len(set(claim.depends_on)) != len(claim.depends_on)
                    or not set(claim.depends_on) <= prior):
                raise ValueError("Derived claims require unique IDs and earlier dependencies")
            prior.add(claim.id)
        derived = {c.id for c in self.derived}
        if (len(set(self.exploration_ids)) != len(self.exploration_ids)
                or not set(self.exploration_ids) <= derived):
            raise ValueError("Exploration IDs must be distinct derived claims")
        if (len(set(self.target_dependencies)) != len(self.target_dependencies)
                or not set(self.target_dependencies) <= prior):
            raise ValueError("Target dependencies must reference distinct graph nodes")
        needed = set(self.target_dependencies)
        for claim in reversed(self.derived):
            if claim.id in needed:
                needed.update(claim.depends_on)
        if not derived <= needed:
            raise ValueError("Every derived claim must contribute to the target")
        return self


class ClaimProofLink(DependencyModel):
    claim_id: str
    proof_step_indices: list[Annotated[int, Field(ge=1)]] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_indices(self):
        if len(set(self.proof_step_indices)) != len(self.proof_step_indices):
            raise ValueError("Claim proof indices must be unique")
        return self


class DependencyTrace(DependencyModel):
    retained_primitive_ids: list[str] = Field(min_length=1)
    hidden_claim_ids: list[str] = Field(min_length=1)
    proof_links: list[ClaimProofLink] = Field(min_length=1)


class DependencyCheck(DependencyModel):
    verdict: Literal["pass", "fail", "uncertain"]
    reasoning: str = Field(min_length=10)
    witness: str = Field(min_length=10)


class PremiseBoundaryReview(DependencyModel):
    primitive_boundary: DependencyCheck
    dependency_support: DependencyCheck
    exploration_preserved: DependencyCheck

    def blocking_checks(self):
        # 具体分类错误、缺失独立前提或等价提示才阻断；不恢复主观深度否决。
        return [name for name in ("primitive_boundary", "dependency_support", "exploration_preserved")
                if getattr(self, name).verdict == "fail"]


class PremiseBoundaryAssessment(DependencyModel):
    goal_sha256: str
    source_sha256: str
    prompt_sha256: str
    model_config_sha256: str
    review: PremiseBoundaryReview
    request_id: str
    actual_model: str


class DependencyProbe(DependencyModel):
    used_givens: list[str] = Field(min_length=1)
    route: list[str] = Field(min_length=1)
    answer: str = Field(min_length=1)
    remaining_reasoning: str = Field(min_length=20)


class ClaimExposure(DependencyModel):
    claim_id: str
    exposure: Literal["given", "derived", "unused", "unresolved"]
    check: DependencyCheck


class DependencyQuestionReview(DependencyModel):
    claims: list[ClaimExposure] = Field(min_length=1)
    primitive_sufficiency: DependencyCheck
    target_preserved: DependencyCheck
    trace_faithful: DependencyCheck
    probe_validity: DependencyCheck
    shortest_correct_route: str = Field(min_length=20)
    remaining_reasoning: str = Field(min_length=20)

    def blocking_checks(self):
        failures = [name for name in ("primitive_sufficiency", "target_preserved", "trace_faithful")
                    if getattr(self, name).verdict == "fail"]
        return failures + ["claim:" + c.claim_id for c in self.claims if c.check.verdict == "fail"]


class DependencyAssessment(DependencyModel):
    input_sha256: str
    prompt_hashes: dict[str, str]
    model_config_sha256: str
    probe: DependencyProbe
    probe_request: str
    probe_actual_model: str
    review: DependencyQuestionReview
    review_request: str
    review_actual_model: str
