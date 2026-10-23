import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field

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
    solver: ModelConfig
    judge: ModelConfig
    quotas_path: str = "configs/quotas.v3.json"
    prompts_dir: str = "prompts"
    samples: int = 16
    correct_min: int = 1
    correct_max: int = 8
    max_requests: int = 150
    max_retries: int = 1
    request_timeout: float = 180.0
    request_deadline_seconds: float | None = Field(default=None, gt=0)
    evaluation_mode: Literal["no_tools"] = "no_tools"
    concurrency: int = 4
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


def load(path: str | Path) -> Settings:
    return Settings.model_validate(tomllib.loads(Path(path).read_text()))
