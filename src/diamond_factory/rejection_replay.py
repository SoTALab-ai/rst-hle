"""冻结证据的只读阶段回放；不创建网关，不购买模型调用，不写回原数据库。"""
import hashlib
import json
from pathlib import Path

from .gates import static_errors
from .library_questions import MaterialMilestone, PaperOutline, admit_lines
from .llm import CacheIntegrityError
from .models import Candidate, ResultSelection, Source, VerifierSpec, digest
from .verifier import has_valid_verifier, repair_spec_anchors, round_errors, spec_errors


def load_sealed_evidence(directory, manifest_sha256):
    directory = Path(directory).resolve()
    manifest_file = directory / "manifest.json"
    if hashlib.sha256(manifest_file.read_bytes()).hexdigest() != manifest_sha256:
        raise CacheIntegrityError("Frozen export manifest changed")
    manifest = json.loads(manifest_file.read_text())
    for name, sha in manifest["files"].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise CacheIntegrityError("Frozen evidence object changed or escaped export")
    traces = {}
    for name in manifest["files"]:
        if not name.startswith("traces/"):
            continue
        trace = json.loads((directory / name).read_text())
        if trace.get("integrity_sha256") != digest({k: v for k, v in trace.items() if k != "integrity_sha256"}):
            raise CacheIntegrityError("Unsealed model response")
        if trace["request_id"] in traces:
            raise CacheIntegrityError("Duplicate model response id")
        traces[trace["request_id"]] = trace
    return manifest, traces


def replay_rejections(directory, manifest_sha256, run_id, quotas, *, source_fidelity="strict"):
    directory = Path(directory)
    manifest, traces = load_sealed_evidence(directory, manifest_sha256)
    if run_id == manifest["origin_run"]:
        raise ValueError("Replay must have a new run-id")
    read = lambda name: json.loads((directory / (name + ".json")).read_text())
    milestone = MaterialMilestone.model_validate(read("milestone"))
    papers = {p.id: p for p in milestone.papers}
    planning = read("planning")
    limit = read("planning-manifest")["settings"]["max_source_chars"]
    allowed = {(q["category"], q["subtopic"]) for q in quotas["quotas"]}
    recovered = []
    for row in planning["papers"]:
        if row["status"] != "planning_failed":
            continue
        outline = PaperOutline.model_validate(row["outline"])
        selected, receipts = admit_lines(papers[row["paper_id"]], outline.lines, allowed, limit,
                                         source_fidelity=source_fidelity)
        recovered.append({"paper_id": row["paper_id"], "doi": row["doi"],
            "paper_domain": papers[row["paper_id"]].domain,
            "original_outline_sha256": digest(row["outline"]), "admission": receipts,
            "admitted_lines": len(selected),
            "admitted_by_category": {c: sum(line.category == c for line, _ in selected) for c in ("Math", "Physics")},
            "next_stage": "independence_review" if len(selected) > 1 else "reference_proof",
            "scientific_pass": False})
    sources = {s["id"]: Source.model_validate(s) for s in read("sources")}
    candidates = [Candidate.model_validate(c) for c in read("candidates")]
    cases, saved_trials = [], []
    for candidate in candidates:
        row = {"id": candidate.id, "category": candidate.item.category,
            "original_status": candidate.status, "original_reasons": candidate.reasons,
            "question_sha256": digest(candidate.item.model_dump()),
            "golden_sha256": digest(candidate.item.answer), "static_errors": static_errors(candidate, sources, quotas,
                source_fidelity=source_fidelity)}
        drafts = [t for t in traces.values() if t["salt"].startswith(candidate.id + ":verifier:")
                  and t["salt"].endswith(":draft")]
        if not candidate.verifier_assessment and drafts:
            if len(drafts) != 1:
                raise CacheIntegrityError("Ambiguous unreviewed verifier draft")
            trace = drafts[0]
            raw = VerifierSpec.model_validate_json(trace["content"])
            fixed, repairs = repair_spec_anchors(raw, candidate.item)
            row.update(original_draft_errors=spec_errors(raw, candidate.item),
                repaired_draft_errors=spec_errors(fixed, candidate.item),
                anchor_repairs=[r.model_dump() for r in repairs], original_request=trace["request_id"],
                next_stage="verifier_adversary", scientific_pass=False)
        elif candidate.verifier_assessment:
            row["original_verifier_round_errors"] = [round_errors(v.result, v.item)
                if v.result else ["missing_round"] for v in candidate.verifier_assessment.versions]
            row["historical_verifier_valid"] = has_valid_verifier(candidate, sources)
        for trial in candidate.trials:
            trace = traces.get(trial.solver_request)
            judge = traces.get(trial.judge_request)
            if (not trace or trace["role"] != "solver" or trace["content"] != trial.response
                    or trace["actual_model"] != trial.solver_actual_model or trace.get("response_error")
                    or not judge or judge["role"] != "judge" or judge.get("response_error")
                    or json.loads(judge["content"]) != trial.judgment.model_dump()):
                raise CacheIntegrityError("Saved solver/judge result does not match paid receipt")
            saved_trials.append({"candidate_id": candidate.id, "sample_id": trial.sample_id,
                "solver_request": trial.solver_request, "judge_request": trial.judge_request,
                "response_sha256": digest(trial.response), "correct": trial.judgment.correct})
        cases.append(row)
    # 已规划但尚未成题的物理线：回放来源锚点，不伪造后续作者/科学审查。
    physics = []
    for root in read("experiment")["roots"]:
        if root["category"] != "Physics":
            continue
        selections = []
        for trace in traces.values():
            if ":known-result:" not in trace["salt"]:
                continue
            selection = ResultSelection.model_validate_json(trace["content"])
            result = selection.result
            if result and result.source_id == root["source_id"]:
                from .difficulty import known_result_errors
                selections.append({"request_id": trace["request_id"], "salt": trace["salt"],
                    "source_anchor_errors": known_result_errors(result, sources, source_fidelity=source_fidelity)})
        physics.append({"root": root["root"], "original_state": root["state"],
                        "known_result_replays": selections, "scientific_pass": False})
    return {"run_id": run_id, "origin_run": manifest["origin_run"],
        "source_fidelity": source_fidelity,
        "evidence_manifest_sha256": manifest_sha256, "sealed_files": len(manifest["files"]),
        "sealed_responses": len(traces), "planning_replays": recovered, "candidate_replays": cases,
        "physics_replays": physics, "saved_trials": saved_trials,
        "new_http_requests": 0, "new_glm_requests": 0, "additional_api_cost_usd": 0,
        "scope": "Stage replay only. No missing model verdicts inferred, no new scientific/difficulty passes."}
