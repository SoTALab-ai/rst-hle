"""冻结旧输出作为待审种子，补缺失阶段；不重买已完成的GLM答卷。"""
import asyncio
import json
import re
from pathlib import Path

from .consistency import has_valid_consistency
from .costs import MeteredGateway, measure_cost
from .difficulty import construction_ready
from .experiment import ExperimentPlan, QuestionExperiment, RootPlan
from .gates import correct_count, sample_outcomes
from .library_questions import MaterialMilestone, OutlineReview, PaperOutline, admit_lines
from .llm import CacheIntegrityError, ModelResponseError
from .models import (
    AuthoringSeed,
    Candidate,
    ResultSelection,
    Solution,
    Source,
    StrictModel,
    VerifierSpec,
    digest,
)
from .rejection_replay import load_sealed_evidence, replay_rejections
from .runtime import atomic_json


def old_root_identity(root, origin_run):
    return digest({"run": origin_run, "source": root.source_id, "parent": None,
        "generation": 0, "attempt": 0, "target": {"category": root.category, "subtopic": root.subtopic},
        "brief": {"root": root.name, "focus": root.focus, "parent_measurement": "none"}})[:24]


def authoring_seed(root, origin_run, candidates, traces):
    identity = old_root_identity(root, origin_run)
    candidate = next((c for c in candidates if c.id == identity), None)
    own = [t for t in traces.values() if t["salt"].startswith(identity + ":") and not t.get("response_error")]
    proofs = sorted([t for t in own if re.fullmatch(re.escape(identity) + r":repair:\d+:solution", t["salt"])],
                    key=lambda t: int(t["salt"].split(":")[2]))
    selections = sorted([t for t in own if re.fullmatch(re.escape(identity) + r":known-result:\d+", t["salt"])],
                        key=lambda t: int(t["salt"].split(":")[-1]))
    found = [t for t in selections if ResultSelection.model_validate_json(t["content"]).result is not None]
    if not candidate and not proofs and not found:
        return None
    seed = AuthoringSeed(source_id=root.source_id, original_candidate_id=candidate.id if candidate else None)
    if found:
        seed.known_result = ResultSelection.model_validate_json(found[-1]["content"]).result
        seed.original_request_ids.append(found[-1]["request_id"])
    if proofs:
        seed.solution = Solution.model_validate_json(proofs[-1]["content"])
        seed.prior_authoring_repairs = int(proofs[-1]["salt"].split(":")[2])
        seed.original_request_ids.append(proofs[-1]["request_id"])
    if candidate:
        seed.solution, seed.item, seed.known_result = candidate.solution, candidate.item, candidate.known_result
        matching_proofs = [t for t in proofs if Solution.model_validate_json(t["content"]) == candidate.solution]
        matching_results = [t for t in found if ResultSelection.model_validate_json(t["content"]).result == candidate.known_result]
        if not matching_proofs or not matching_results:
            raise CacheIntegrityError("Historical candidate lacks its bound authoring receipts")
        seed.original_request_ids = [matching_results[-1]["request_id"], matching_proofs[-1]["request_id"]]
        seed.prior_authoring_repairs = candidate.authoring_repairs_completed
        if candidate.source_ids != [root.source_id]:
            raise CacheIntegrityError("Historical candidate/source binding changed")
        if candidate.verifier_assessment:
            version = candidate.verifier_assessment.versions[-1]
            if version.item == candidate.item:
                seed.verifier_spec = version.spec
                seed.verifier_request, seed.verifier_actual_model = version.spec_request, version.spec_actual_model
        else:
            drafts = [t for t in own if ":verifier:" in t["salt"] and t["salt"].endswith(":draft")]
            if len(drafts) > 1:
                raise CacheIntegrityError("Ambiguous historical verifier draft")
            if drafts:
                t = drafts[0]
                seed.verifier_spec = VerifierSpec.model_validate_json(t["content"])
                seed.verifier_request, seed.verifier_actual_model = t["request_id"], t["actual_model"]
        if seed.verifier_request:
            trace = traces[seed.verifier_request]
            if trace.get("response_error"):
                raise CacheIntegrityError("Historical verifier response is not reusable")
            seed.verifier_spec = VerifierSpec.model_validate_json(trace["content"])
            seed.original_request_ids.append(seed.verifier_request)
    return seed


class ContinuationGateway(MeteredGateway):
    """跨run只复用明确绑定样本的原答卷/判分；其余调用仍走标准付费缓存。"""
    def __init__(self, *args, historical_candidates, historical_traces, **kwargs):
        super().__init__(*args, **kwargs)
        self.historical = {}
        self.reused_solver_pairs = {}
        for c in historical_candidates:
            if c.deadline_outcomes:
                raise CacheIntegrityError("This continuation requires answered historical samples")
            for trial in c.trials:
                key = (c.item.question, trial.sample_id)
                if key in self.historical:
                    raise CacheIntegrityError("Ambiguous historical solver sample")
                solver, judge = historical_traces[trial.solver_request], historical_traces[trial.judge_request]
                if (solver["role"] != "solver" or solver.get("response_error") or solver["content"] != trial.response
                        or judge["role"] != "judge" or judge.get("response_error")
                        or json.loads(judge["content"]) != trial.judgment.model_dump()):
                    raise CacheIntegrityError("Historical solver/judge receipt mismatch")
                self.historical[key] = (solver, judge)
        # judge失败之前已成功返回的GLM也可复用；只在字面题面、样本和完整协议相同时接纳。
        for trace in historical_traces.values():
            if trace.get("role") != "solver" or trace.get("response_error") or not trace.get("content"):
                continue
            try:
                if not isinstance(trace["content"], str):
                    raise TypeError("Solver answer must be text")
                if ":solver:" not in trace["salt"]:
                    raise ValueError("Missing solver sample namespace")
                sample = int(trace["salt"].rsplit(":", 1)[1])
                prompt = trace["request"]["messages"][1]["content"]
                if not isinstance(prompt, str) or not 0 <= sample < self.settings.samples:
                    raise ValueError("Invalid solver sample")
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise CacheIntegrityError("Malformed historical solver receipt") from error
            key = (prompt, sample)
            if key in self.historical and self.historical[key][0]["request_id"] != trace["request_id"]:
                raise CacheIntegrityError("Ambiguous historical solver sample")
            self.historical.setdefault(key, (trace, None))

    def _reuse(self, role, prompt, salt, trace, schema, system_prompt, images):
        config, payload, _, _, _ = self._prepare(role, prompt, salt, schema, system_prompt, images)
        _, _, original_id, _, _ = self._prepare(role, prompt, trace["salt"], schema, system_prompt, images)
        if (trace["request"] != payload or trace["actual_model"] not in
                (config.accepted_response_models or [config.model]) or trace["request_id"] != original_id):
            raise CacheIntegrityError("Completed historical sample has incompatible protocol; refusing to rebuy")
        if trace.get("integrity_sha256") != digest({k: v for k, v in trace.items() if k != "integrity_sha256"}):
            raise CacheIntegrityError("Historical sample seal changed")
        if self.store:
            self.store.event(self.run_id, {"event": "historical_response_reused", "role": role,
                "request_id": trace["request_id"], "requested_salt": salt,
                "original_salt": trace["salt"], "payload_sha256": digest(payload)})
        return (schema.model_validate_json(trace["content"]) if schema else trace["content"],
                trace["request_id"], trace["actual_model"])

    async def call(self, role, prompt, salt, schema=None, *, system_prompt=None, images=None):
        if role == "solver":
            sample = int(salt.rsplit(":", 1)[1])
            pair = self.historical.get((prompt, sample))
            if pair:
                result = self._reuse(role, prompt, salt, pair[0], schema, system_prompt, images)
                self.reused_solver_pairs[(salt.split(":")[0], sample)] = pair
                return result
        if role == "judge" and ":judge:" in salt:
            key = (salt.split(":")[0], int(salt.rsplit(":", 1)[1]))
            pair = self.reused_solver_pairs.get(key)
            if pair and pair[1] is not None:
                # 金标/判分提示变化时原答卷仍复用，但judge按新问题合法重审。
                _, payload, _, _, _ = self._prepare(role, prompt, salt, schema, system_prompt, images)
                if pair[1]["request"] == payload:
                    return self._reuse(role, prompt, salt, pair[1], schema, system_prompt, images)
        return await super().call(role, prompt, salt, schema, system_prompt=system_prompt, images=images)


class SolverHistory(StrictModel):
    origins: dict[str, str]
    candidates: list[Candidate]


def import_solver_history(store, directory, manifest_sha256, run_prefix):
    """新库导入已封印答卷；旧HTTP仍归原run，不复制任何科学通过资格。"""
    manifest, traces = load_sealed_evidence(directory, manifest_sha256)
    if "solver-history.json" not in manifest["files"]:
        raise CacheIntegrityError("Historical ownership metadata is not sealed")
    history = SolverHistory.model_validate_json((Path(directory)/"solver-history.json").read_text())
    if set(history.origins) != set(traces) or any(t["role"] not in ("solver", "judge") for t in traces.values()):
        raise CacheIntegrityError("Historical response ownership does not match the sealed export")
    for origin in set(history.origins.values()):
        if origin.startswith(run_prefix):
            raise CacheIntegrityError("Historical paid run cannot be the new campaign")
        prior = store.runs().get(origin)
        if prior and prior["fingerprint"] != manifest_sha256:
            raise CacheIntegrityError("Conflicting historical import")
        store.run(origin, {"mode": "historical_import", "status": "sealed", "fingerprint": manifest_sha256})
    for rid, trace in traces.items():
        prior = store.get_request(rid)
        if prior is not None and prior != trace:
            raise CacheIntegrityError("Conflicting historical receipt")
        if prior is None:
            store.save_request(history.origins[rid], trace)
    return history.candidates, traces


async def prepare_continuation(store, settings, pricing, evidence, manifest_sha256, directory, run_prefix):
    """复用20份原规划；只给尚无多题独立审查的材料补审，不重买作者规划。"""
    directory, evidence = Path(directory), Path(evidence)
    manifest, traces = load_sealed_evidence(evidence, manifest_sha256)
    # 同时验证旧答卷与judge逐一绑定；破损不准启动。
    replay_rejections(evidence, manifest_sha256, run_prefix+"-integrity", json.loads(
        Path(settings.quotas_path).read_text()), source_fidelity=settings.source_fidelity)
    origin = manifest["origin_run"]
    if run_prefix == origin or settings.source_fidelity != "advisory":
        raise ValueError("Continuation requires a new advisory run")
    read = lambda name: json.loads((evidence/(name+".json")).read_text())
    milestone = MaterialMilestone.model_validate(read("milestone"))
    if any(manifest["files"].get(f"pdf/{doc.pdf_sha256}.pdf") != doc.pdf_sha256
           for paper in milestone.papers for doc in paper.documents):
        raise CacheIntegrityError("Original material PDF missing from sealed export")
    planning = read("planning")
    candidates = [Candidate.model_validate(c) for c in read("candidates")]
    if any(store.runs().get(r, {}).get("mode") not in (None, "historical_import")
           for r in (origin, planning["run_id"])):
        raise ValueError("Original trial database cannot be the continuation destination")
    for old_run in {origin, planning["run_id"]}:
        if old_run not in store.runs():
            store.run(old_run, {"mode": "historical_import", "status": "sealed",
                "fingerprint": manifest_sha256, "origin_manifest_sha256": manifest_sha256})
    for source in read("sources"):
        store.add_source(Source.model_validate(source))
    for trace in traces.values():
        old_run = planning["run_id"] if any(trace["request_id"] == row.get(k) for row in planning["papers"]
            for k in ("planning_request", "review_request")) else origin
        prior = store.get_request(trace["request_id"])
        if prior is not None and prior != trace:
            raise CacheIntegrityError("Imported paid response changed")
        if prior is None:
            store.save_request(old_run, trace)
    for candidate in candidates:
        if not any(c.id == candidate.id for c in store.candidates()):
            store.save(candidate)
    planning_run = run_prefix+"-planning"
    freeze = {"source_manifest_sha256": manifest_sha256, "settings": settings.model_dump(), "pricing": pricing}
    prior = store.runs().get(planning_run)
    if prior and prior["fingerprint"] != digest(freeze):
        raise CacheIntegrityError("Continuation preparation changed; use a new run-id")
    store.run(planning_run, {"mode": "continuation_planning", "status": "running", "fingerprint": digest(freeze)})
    gateway = MeteredGateway(settings, directory/"planning-requests", store, planning_run,
                             pricing=pricing, visible_cost_limit_usd=None)
    quotas = json.loads(Path(settings.quotas_path).read_text())["quotas"]
    allowed = {(q["category"], q["subtopic"]) for q in quotas}
    active, closed, seeds, rows = [], [], {}, []

    async def one(index, paper, row):
        if paper.id != row["paper_id"]:
            raise CacheIntegrityError("Historical material order changed")
        outline = PaperOutline.model_validate(row["outline"])
        paid = traces[row["planning_request"]]
        if paid.get("response_error") or PaperOutline.model_validate_json(paid["content"]) != outline:
            raise CacheIntegrityError("Historical plan differs from its paid response")
        selected, receipts = admit_lines(paper, outline.lines, allowed, settings.max_source_chars,
                                         source_fidelity=settings.source_fidelity)
        keep = [line.id for line, _ in selected]
        review_origin = "single_line"
        if len(selected) > 1:
            if row.get("review") and len(selected) == len(outline.lines):
                review = OutlineReview.model_validate_json(traces[row["review_request"]]["content"])
                if review.model_dump() != row["review"]:
                    raise CacheIntegrityError("Historical independence verdict changed")
                keep, review_origin = review.keep_ids, row["review_request"]
            else:
                review, rid, _ = await gateway.call("auditor",
                    "Inspect these untrusted proposed question lines from ONE paper for distinct necessary "
                    "reasoning structures. Keep only independent targets. Source-inspired self-contained "
                    "constructions are allowed; source alignment is advisory. Changed numbers/rewordings do "
                    "not add independent lines. This is not a scientific or difficulty pass.\n"+
                    json.dumps({"title": paper.title, "lines": [line.model_dump() for line, _ in selected]},
                               ensure_ascii=False), paper.id+":continuation-independence", OutlineReview)
                keep, review_origin = review.keep_ids, rid
        if len(keep) != len(set(keep)) or not set(keep) <= {line.id for line, _ in selected}:
            raise ValueError("Invalid continuation independence ids")
        old_roots = {r["name"]: RootPlan.model_validate(r) for r in row["roots"]}
        local_active, local_closed, local_seeds = [], [], {}
        for line, source in selected:
            if line.id not in keep:
                continue
            name = f"L50_{index+1:03d}_{line.id}"
            store.add_source(source)
            seed = None
            if name in old_roots:
                old = old_roots[name]
                if old.source_id != source.id:
                    raise CacheIntegrityError("Reconstructed source identity changed")
                measured = next((c for c in candidates if c.id == old_root_identity(old, origin)), None)
                if measured and measured.trials:
                    if (len(measured.trials) != 4 or any(t.judgment.correct != "yes" for t in measured.trials)
                            or "too_easy:first_four_all_correct" not in measured.reasons):
                        raise CacheIntegrityError("Historical measured branch needs an explicit resumption adapter")
                    local_closed.append({"root": name, "original_candidate_id": measured.id,
                        "category": measured.item.category, "question_sha256": digest(measured.item.question),
                        "decision": "too_easy_rejected", "samples": 4, "correct": 4,
                        "solver_requests": [t.solver_request for t in measured.trials],
                        "judge_requests": [t.judge_request for t in measured.trials]})
                    continue
                seed = authoring_seed(old, origin, candidates, traces)
            focus = ("Continue this frozen material as inspiration for a self-contained theoretical construction. "
                "The candidate answer/complete proof are untrusted until independent review. Source alignment "
                "is advisory; preserve any already locked candidate gold and model premises. Hide the hardest "
                "necessary inference, verify actual question/gold consistency and all physical/domain cases. "
                "Ask only the short result covered by the golden, never prove an already displayed answer. "
                "Only ease overly hard valid questions; reject easy ones.\nUntrusted target: "+line.model_dump_json())
            local_active.append(RootPlan(name=name, source_id=source.id, category=line.category,
                                          subtopic=line.subtopic, focus=focus))
            if seed:
                local_seeds[name] = seed
        return local_active, local_closed, local_seeds, {"paper_id": paper.id, "doi": paper.doi,
            "proposed": len(outline.lines), "admission": receipts, "keep_ids": keep,
            "independence_review": review_origin, "active_roots": len(local_active),
            "historical_closed": len(local_closed), "scientific_pass": False}

    # 独立材料失败不得取消其他已付费在途审查；全部响应排空后再报告。
    results = await asyncio.gather(*(one(i, paper, planning["papers"][i])
        for i, paper in enumerate(milestone.papers)), return_exceptions=True)
    for index, result in enumerate(results):
        if isinstance(result, (ValueError, ModelResponseError)) and not isinstance(result, CacheIntegrityError):
            rows.append({"paper_id": milestone.papers[index].id, "doi": milestone.papers[index].doi,
                "proposed": len(planning["papers"][index]["outline"]["lines"]), "status": "planning_failed",
                "reason": str(result), "active_roots": 0, "scientific_pass": False})
            continue
        if isinstance(result, BaseException):
            raise result
        a, c, s, row = result
        active.extend(a); closed.extend(c); seeds.update(s); rows.append(row)
    plan = ExperimentPlan(roots=active, authoring_seeds=seeds, root_workers=4, max_revisions=2) if active else None
    report = {"run_prefix": run_prefix, "materials": len(milestone.papers),
        "material_domains": {c: sum(p.domain == c for p in milestone.papers) for c in ("Math", "Physics")},
        "proposed_root_lines": sum(r["proposed"] for r in rows), "active_root_lines": len(active),
        "historical_closed": closed, "reused_seed_roots": len(seeds), "papers": rows,
        "planning_cost": measure_cost(store, planning_run, directory/"planning-requests", settings, pricing)}
    atomic_json(directory/"plan.json", plan.model_dump() if plan else None)
    atomic_json(directory/"preparation.json", report)
    store.run(planning_run, {"mode": "continuation_planning", "status": "planned", "fingerprint": digest(freeze),
                             "report": report})
    return plan, report, candidates, traces


def continuation_report(experiment, preparation, baseline, settings, pricing, directory, store, run_id):
    """累计费用归原支付run；历史4/4拒绝与新协议审查结果分列。"""
    directory = Path(directory)
    current = [c for c in store.candidates() if c.run_id == run_id]
    sources = {s.id: s for s in store.sources()}
    prompts = {p.stem: p.read_text() for p in Path(settings.prompts_dir).glob("*.txt")}
    quality = [c for c in current if c.audit.passed() and has_valid_consistency(c, sources)
               and construction_ready(c, sources, settings, prompts)]
    pre_quality = [c for c in current if sample_outcomes(c) or c in quality]
    in_band = [c for c in quality if len(sample_outcomes(c)) == 16
               and 1 <= correct_count(sample_outcomes(c)) <= 8]
    retained = [c for c in in_band if c.status in ("screened", "accepted")]
    trial_cost = measure_cost(store, run_id, directory/"questions"/"requests", settings, pricing)
    planning_cost = preparation["planning_cost"]
    incremental = planning_cost["known_cost_usd"]+trial_cost["known_cost_usd"]
    historical_solver_ids = {rid for c in preparation["historical_closed"] for rid in c["solver_requests"]}
    def counts(rows):
        return {category: sum(c.item.category == category for c in rows) for category in ("Math", "Physics")}
    result = {"run_id": run_id, "materials": preparation["materials"],
        "material_domains": preparation["material_domains"],
        "proposed_root_lines": preparation["proposed_root_lines"],
        "active_root_lines": preparation["active_root_lines"],
        "historical_closed_4_of_4_roots": len(preparation["historical_closed"]),
        "historical_solver_answers_reused_without_rerun": sum(c["samples"] for c in preparation["historical_closed"]),
        "historical_full_quality_passes_original_protocol": len(preparation["historical_closed"]),
        "generated_versions_this_run": len(current), "pre_quality_passed_versions": len(pre_quality),
        "currently_quality_valid_versions": len(quality), "complete_16_in_band_versions": len(in_band),
        "retained_after_postreview_and_dedup": len(retained),
        "quality_by_category": counts(quality), "retained_by_category": counts(retained),
        "new_solver_answer_samples": sum(t.solver_request not in historical_solver_ids for c in current for t in c.trials),
        "old_answers_reused_in_new_versions": sum(t.solver_request in historical_solver_ids for c in current for t in c.trials),
        "solver_deadline_incorrect_samples": sum(len(c.deadline_outcomes) for c in current),
        "first4_all_correct_rejections": sum("too_easy:first_four_all_correct" in c.reasons for c in current),
        "quality_rejected_versions": sum(c.status == "rejected" and not any(
            r.startswith(("too_easy:", "duplicate:")) for r in c.reasons) for c in current),
        "cost": {"baseline_library_and_original_trial_usd": baseline["known_cost_usd"],
            "incremental_known_api_usd": incremental,
            "cumulative_known_api_usd": baseline["known_cost_usd"]+incremental,
            "cumulative_rmb_reference": (baseline["known_cost_usd"]+incremental)*pricing["rmb_reference_exchange_rate"],
            "incremental_unknown_usage": planning_cost["unknown_usage_attempts"]+trial_cost["unknown_usage_attempts"],
            "incremental_unsettled_reservations": planning_cost["unsettled_reserved_attempts"]+trial_cost["unsettled_reserved_attempts"],
            "planning": planning_cost, "question_run": trial_cost, "vm_disk_network_usd": None},
        "state": store.runs().get(run_id, {}).get("status", "not_started"),
        "experiment": experiment,
        "scope": "Pilot. Historical terminal difficulty retained; new QC is rechecked. Same-paper roots correlated; VM costs unknown."}
    atomic_json(directory/"continuation-report.json", result)
    return result


class ContinuationExperiment(QuestionExperiment):
    def __init__(self, *args, preparation, baseline, campaign_directory, **kwargs):
        super().__init__(*args, **kwargs)
        self.preparation, self.baseline, self.campaign_directory = preparation, baseline, Path(campaign_directory)

    def snapshot(self):
        result = super().snapshot()
        continuation_report(result, self.preparation, self.baseline, self.settings, self.pricing,
                            self.campaign_directory, self.store, self.run_id)
        return result
