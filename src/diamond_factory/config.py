import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import StrictModel


class ModelConfig(StrictModel):
    model: str
    base_url_env: str = "NEW_API_BASE_URL"
    api_key_env: str = "NEW_API_KEY"
    reasoning_effort: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    accepted_response_models: list[str] = Field(default_factory=list)


class Settings(StrictModel):
    generator: ModelConfig
    auditor: ModelConfig
    consistency: ModelConfig = Field(default_factory=lambda: ModelConfig(
        model="gpt-6-luna", reasoning_effort="high"))
    solver: ModelConfig
    judge: ModelConfig
    judge_schema_repairs: int = Field(default=1, ge=0, le=2)
    consistency_schema_repairs: int = Field(default=1, ge=0, le=2)
    certificate_max_repairs: int = Field(default=0, ge=0, le=2)
    early_screen_samples: Literal[0, 4] = 0
    difficulty_profile_path: str | None = None
    require_solution_design: bool = False
    require_difficulty_design: bool = False
    preverify_blueprint: bool = False
    require_stepwise_blueprint_review: bool = False
    require_known_result: bool = False
    use_locked_proof_draft: bool = False
    source_fidelity: Literal["strict", "advisory"] = "strict"
    require_bottleneck_mask: bool = False
    require_difficulty_adversary: bool = False
    difficulty_adversary_mode: Literal["depth_gate", "shortcut_blocking"] = "depth_gate"
    # paper_grounded：以论文推导为正确性依据，只有具体错误才拦截；不确定、验算未覆盖和深度意见只记录。
    correctness_basis: Literal["self_verified", "paper_grounded"] = "self_verified"
    difficulty_references_path: str | None = None
    scientific_goal_references_path: str | None = None
    difficulty_design_max_revisions: int = Field(default=2, ge=0, le=2)
    difficulty_mask_max_revisions: int = Field(default=2, ge=0, le=2)
    require_verifier_adversary: bool = False
    freeze_question_at_verifier: bool = True
    verifier_max_revisions: int = Field(default=2, ge=0, le=2)
    quotas_path: str = "configs/quotas.v3.json"
    prompts_dir: str = "prompts"
    source_routes_path: str | None = None
    samples: int = 16
    correct_min: int = 1
    correct_max: int = 8
    max_requests: int = Field(default=150, ge=1)
    request_cap_enabled: bool = True
    difficulty_policy: Literal["ease_only", "bidirectional"] = "ease_only"
    solver_deadline_counts_as_incorrect: bool = True
    max_retries: int = Field(default=1, ge=0)
    request_timeout: float = Field(default=180.0, gt=0)
    request_deadline_seconds: float | None = Field(default=None, gt=0)
    evaluation_mode: Literal["no_tools"] = "no_tools"
    concurrency: int = Field(default=4, ge=1)
    solver_concurrency: int | None = Field(default=None, ge=1)
    max_source_chars: int = 45000
    max_rounds: int = 3
    attempts_per_round: int = 2
    max_repairs: int = 1
    max_per_root: int = 1
    semantic_batch_size: int = 12
    source_max_candidates: int = 2
    operators: list[str] = Field(default_factory=lambda: [
        "constraint_composition", "inverse_reasoning", "counterfactual", "boundary_case"])
    judge_contract: str = "configs/judge-contract.json"
    dedup_corpora: list[str] = Field(default_factory=list)
    external_dedup_required: bool = False

    @model_validator(mode="after")
    def validate_role_capacity(self):
        if self.scientific_goal_references_path and not (
                self.correctness_basis == "paper_grounded" and self.require_known_result):
            raise ValueError("Scientific goal references require paper-grounded result locking")
        if self.difficulty_adversary_mode == "shortcut_blocking" and not self.require_difficulty_adversary:
            raise ValueError("Shortcut blocking requires the difficulty adversary")
        if self.correctness_basis == "paper_grounded" and (
                self.require_difficulty_adversary or not self.require_verifier_adversary
                or not self.require_bottleneck_mask):
            # 难度交给GLM实测，不再叠加6Luna深度否决；verifier与挖空审查保留。
            raise ValueError("Paper-grounded basis measures depth with GLM and requires verifier and mask review")
        if self.require_difficulty_adversary and not (
                self.source_fidelity == "advisory" and self.use_locked_proof_draft
                and self.require_bottleneck_mask and self.require_verifier_adversary
                and self.freeze_question_at_verifier and self.difficulty_references_path
                and self.difficulty_policy == "ease_only"):
            raise ValueError("Difficulty adversary requires advisory construction, locked proof, masking, references and frozen verifier")
        if self.use_locked_proof_draft and not self.require_known_result:
            raise ValueError("Locked proof drafts require a locked known result")
        if self.source_fidelity == "advisory" and not (
                self.require_stepwise_blueprint_review and self.require_known_result
                and self.require_verifier_adversary):
            raise ValueError("Advisory provenance requires answer locking, independent proof review and verifier adversary")
        if self.solver_concurrency is not None and self.solver_concurrency>self.concurrency:
            raise ValueError("solver_concurrency must not exceed global concurrency")
        if self.require_difficulty_design and (not self.require_solution_design or not self.difficulty_profile_path):
            raise ValueError("Difficulty design requires solution design and a difficulty profile")
        if self.require_stepwise_blueprint_review and not self.preverify_blueprint:
            raise ValueError("Stepwise blueprint review requires blueprint verification")
        if self.require_known_result and not self.require_stepwise_blueprint_review:
            raise ValueError("Known result locking requires stepwise blueprint verification")
        if self.require_bottleneck_mask and (not self.require_known_result or not self.require_difficulty_design):
            raise ValueError("Bottleneck masking requires a locked known result and difficulty design")
        return self


def load(path: str | Path) -> Settings:
    return Settings.model_validate(tomllib.loads(Path(path).read_text()))
