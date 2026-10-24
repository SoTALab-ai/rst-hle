"""对既有题面的独立质量副本试验verifier；不购买解题、不认证难度。"""
import argparse
import asyncio
import json
from pathlib import Path

from diamond_factory.config import load
from diamond_factory.costs import MeteredGateway, measure_cost
from diamond_factory.models import Candidate, digest
from diamond_factory.pipeline import Pipeline
from diamond_factory.postgres import open_store
from diamond_factory.runtime import atomic_json


class QualityOnlyGateway(MeteredGateway):
    async def call(self, role, *args, **kwargs):
        if role not in ("generator", "auditor", "consistency", "judge"):
            raise ValueError("Verifier diagnostic forbids solver calls before cache or network")
        return await super().call(role, *args, **kwargs)


parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--rates", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--source-candidate", required=True)
parser.add_argument("--source-sha256", required=True)
parser.add_argument("--reuse-diagnostic-identity", action="store_true")
args = parser.parse_args()
if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
    raise ValueError("Invalid run-id")
raw = json.loads(Path(args.source_candidate).read_text())
if digest(raw) != args.source_sha256:
    raise ValueError("Frozen source candidate hash mismatch")
original = Candidate.model_validate(raw)
settings = load(args.config)
if not settings.require_verifier_adversary:
    raise ValueError("Explicitly enable the adversarial verifier protocol")
store = open_store("work/factory.sqlite")
directory = Path("work/runs") / args.run_id
pricing = json.loads(Path(args.rates).read_text())
gateway = QualityOnlyGateway(settings, directory / "requests", store, args.run_id,
                             pricing=pricing, visible_cost_limit_usd=None)
pipe = Pipeline(settings, store, args.run_id, "pilot", directory, gateway)
identity = digest({"diagnostic_run": args.run_id, "source_candidate_sha256": args.source_sha256})[:24]
if args.reuse_diagnostic_identity:
    if (original.operator != "verifier_quality_diagnostic" or original.trials or original.deadline_outcomes
            or original.id != original.root_id or original.run_id == args.run_id):
        raise ValueError("Only a frozen unmeasured diagnostic may retain its request identity in a new run")
    identity = original.id
with store.lock():
    pipe.start({"command": "verifier-adversary-diagnostic", "source_candidate_sha256": args.source_sha256,
                "pricing_sha256": digest(pricing), "difficulty_sampling_allowed": False,
                "reuse_diagnostic_identity": args.reuse_diagnostic_identity})
    candidate = next((c for c in store.candidates() if c.id == identity), None)
    if candidate is None:
        # 明确创建新ID的质量诊断副本；原题、样本和难度记录保留在冻结来源中。
        candidate = original.model_copy(deep=True)
        candidate.id = candidate.root_id = identity
        candidate.parent_id = None
        candidate.generation = 0
        candidate.run_id = args.run_id
        candidate.operator = "verifier_quality_diagnostic"
        candidate.trials, candidate.deadline_outcomes = [], []
        candidate.verifier_assessment, candidate.verifier_history = None, []
        candidate.status, candidate.reasons, candidate.authoring_complete = "quality_seed", [], False
        store.save(candidate)
    try:
        passed = asyncio.run(pipe.preflight(candidate))
        pipe.metadata("completed" if passed else "completed_with_errors")
    except BaseException:
        pipe.metadata("interrupted")
        raise
    cost = measure_cost(store, args.run_id, directory / "requests", settings, pricing)
    if cost["by_role"].get("solver", {}).get("http_attempts", 0):
        raise ValueError("Forbidden solver HTTP attempt in diagnostic")
    assessment = candidate.verifier_assessment
    report = {"run_id": args.run_id, "source_candidate_id": original.id,
              "source_candidate_sha256": args.source_sha256, "planned_quality_probes": 1,
              "candidate_id": candidate.id, "quality_passed": passed,
              "difficulty_samples": 0, "retained_delivery_items": 0,
              "original_question_sha256": digest(original.item.model_dump()),
              "current_question_sha256": digest(candidate.item.model_dump()),
              "golden_unchanged": candidate.item.answer == original.item.answer,
              "reference_proof_unchanged": candidate.solution == original.solution,
              "verifier_versions": len(assessment.versions) if assessment else 0,
              "question_revisions": sum(bool(v.question_repair_request) for v in assessment.versions) if assessment else 0,
              "reasons": candidate.reasons, "cost": cost}
    atomic_json(directory / "verifier-probe-report.json", report)
    print(json.dumps(report, ensure_ascii=False))
