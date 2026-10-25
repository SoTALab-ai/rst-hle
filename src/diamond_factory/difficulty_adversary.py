"""首题采样前的两阶段难度对抗；不改已采样题，不把模型意见当难度实测。"""
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .artifacts import LocalObjects
from .difficulty import bottleneck_mask_errors, difficulty_design_errors, known_result_errors
from .llm import CacheIntegrityError, ModelResponseError
from .models import (
    Audit,
    Candidate,
    DifficultyCampaign,
    DifficultyChallenge,
    DifficultyMaskRevision,
    DifficultyOrigin,
    DifficultyProposal,
    DifficultyProposalVersion,
    DifficultyQuestionRound,
    Question,
    StrictModel,
    digest,
)


class DifficultyRejected(RuntimeError):
    pass


class ReferenceCard(StrictModel):
    id: str
    category: Literal["Math", "Physics"]
    observed_correct: int = Field(ge=0)
    valid_samples: int = Field(ge=0)
    requested_samples: int = Field(ge=1)
    measurement_complete: bool
    convention: str
    modules: list[str] = Field(min_length=1)
    bottlenecks: list[str] = Field(min_length=1)
    legitimate_short_routes: list[str] = Field(min_length=1)
    ambiguity_warnings: list[str] = Field(min_length=1)
    observed_failures: list[str] = Field(min_length=1)


class ReferenceCards(StrictModel):
    version: str
    cards: list[ReferenceCard] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_references(self):
        if len({c.id for c in self.cards}) != len(self.cards):
            raise ValueError("Duplicate difficulty references")
        for card in self.cards:
            if (not card.observed_correct <= card.valid_samples <= card.requested_samples
                    or card.measurement_complete != (card.valid_samples == card.requested_samples)):
                raise ValueError("Reference counts or completion claims disagree")
        return self


class CheckpointPointer(StrictModel):
    key: str
    sha256: str
    size: int = Field(ge=1)


def references(settings, category=None):
    cards = ReferenceCards.model_validate_json(Path(settings.difficulty_references_path).read_text())
    selected = [c.model_dump() for c in cards.cards if category is None or c.category == category]
    if not selected:
        raise ValueError("No difficulty reference for this category")
    return {"version": cards.version, "cards": selected}


def protocol_hash(settings, prompts):
    # 不记录凭据。端点、模型、参考卡、修订上限或协议代码改变，旧难度收据均失效。
    endpoints = {role: os.environ.get(getattr(settings, role).base_url_env, "").rstrip("/")
                 for role in ("generator", "auditor", "consistency")}
    return digest({"protocol": "two-stage-difficulty-v1", "prompts": prompts,
        "references": references(settings), "profile": Path(settings.difficulty_profile_path).read_text(),
        "models": {role: getattr(settings, role).model_dump() for role in endpoints},
        "endpoints_sha256": digest(endpoints), "deadline": settings.request_deadline_seconds,
        "limits": [settings.difficulty_design_max_revisions, settings.difficulty_mask_max_revisions],
        "source_fidelity": settings.source_fidelity,
        "code": {name: Path(__file__).with_name(name).read_text()
                 for name in ("difficulty_adversary.py", "models.py", "difficulty.py")}})


def valid_origin(candidate, sources, expected_protocol):
    origin = candidate.difficulty_origin
    if (not origin or origin.protocol_sha256 != expected_protocol or origin.root_id != candidate.root_id
            or len(candidate.source_ids) != 1 or candidate.source_ids[0] not in sources
            or origin.source_sha256 != sources[candidate.source_ids[0]].sha256):
        return False
    version = origin.selected
    if (version.proposal.outcome != "proposed" or not version.blueprint
            or not version.blueprint.passed(source_fidelity=candidate.source_fidelity)
            or not version.challenge or version.challenge.verdict != "sufficient"
            or not version.blueprint_request or not version.challenge_request or not version.author_request
            or version.errors or version.proposal.result != candidate.known_result):
        return False
    original = version.proposal.proof.bind(version.proposal.result)
    if (version.blueprint.reviewed_step_count != len(original.derivation)
            or max(version.challenge.indispensable_proof_entries) > len(original.derivation)):
        return False
    # 后续有效0/16降难保留原结果，只继承根题来源；实际新解仍须科学复核与题面难度审查。
    return original == candidate.solution if candidate.generation == 0 else bool(candidate.parent_id)


def question_input(candidate, sources):
    return digest({"item": candidate.item.model_dump(), "solution": candidate.solution.model_dump(),
        "known_result": candidate.known_result.model_dump() if candidate.known_result else None,
        "origin": candidate.difficulty_origin.model_dump() if candidate.difficulty_origin else None,
        "sources": {sid: sources[sid].sha256 for sid in candidate.source_ids},
        "audit": candidate.audit.model_dump(),
        "blueprint": candidate.blueprint_fingerprint,
        "consistency": candidate.consistency.model_dump() if candidate.consistency else None,
        "bottleneck": candidate.bottleneck_assessment.model_dump() if candidate.bottleneck_assessment else None})


def valid_history(candidate, sources, expected_protocol):
    if not candidate.difficulty_rounds:
        return False
    parent, previous_record = "", None
    for record in candidate.difficulty_rounds:
        if (record.parent_input_sha256 != parent or record.protocol_sha256 != expected_protocol
                or not record.request_id or max(record.challenge.indispensable_proof_entries, default=0)
                > len(record.solution.derivation) or not record.audit.passed()
                or not record.bottleneck.review.passed() or not record.consistency.passed()):
            return False
        if previous_record and (previous_record.revision is None
                or previous_record.challenge.verdict != "too_easy"
                or record.item != previous_record.item.model_copy(update={"question": previous_record.revision.question})
                or record.solution != previous_record.solution):
            return False
        previous = candidate.model_copy(update={"item": record.item, "solution": record.solution,
            "audit": record.audit, "bottleneck_assessment": record.bottleneck, "consistency": record.consistency})
        if record.input_sha256 != question_input(previous, sources):
            return False
        parent = record.input_sha256
        previous_record = record
    return True


def has_valid_difficulty(candidate, sources, expected_protocol):
    if not valid_origin(candidate, sources, expected_protocol) or not valid_history(candidate, sources, expected_protocol):
        return False
    last = candidate.difficulty_rounds[-1]
    return last.challenge.verdict == "sufficient" and last.input_sha256 == question_input(candidate, sources)


class DifficultyAdversary:
    def __init__(self, pipeline):
        self.p = pipeline
        self.protocol = protocol_hash(pipeline.settings, pipeline.prompts)
        self.objects = getattr(pipeline.store, "objects", None) or LocalObjects(pipeline.run_dir / "difficulty-objects")

    def save_campaign(self, campaign):
        key, sha, size = self.objects.put(campaign.model_dump())
        self.p.difficulty_checkpoints[campaign.identity] = CheckpointPointer(key=key, sha256=sha, size=size).model_dump()
        # PG保存轻量指针，完整提案/推导/审查在独立CAS；SQLite诊断使用本run的CAS。
        self.p.metadata("running")

    def load_campaign(self, identity, input_hash):
        ref = self.p.difficulty_checkpoints.get(identity)
        if ref is None:
            return DifficultyCampaign(identity=identity, input_sha256=input_hash, protocol_sha256=self.protocol)
        pointer = CheckpointPointer.model_validate(ref)
        campaign = DifficultyCampaign.model_validate(self.objects.get(pointer.key, pointer.sha256, pointer.size))
        if (campaign.identity != identity or campaign.input_sha256 != input_hash
                or campaign.protocol_sha256 != self.protocol
                or len(campaign.versions) > self.p.settings.difficulty_design_max_revisions + 1):
            raise CacheIntegrityError("Difficulty checkpoint changed; use a new run-id")
        parent = None
        for index, version in enumerate(campaign.versions):
            if (version.parent_proposal_id != parent
                    or version.proposal_id != digest({"identity": identity, "proposal": index})[:24]):
                raise CacheIntegrityError("Difficulty proposal lineage changed")
            parent = version.proposal_id
        return campaign

    async def design(self, source, target, identity, brief):
        p = self.p
        source_data = {**source.model_dump(), "text": source.text[:p.settings.max_source_chars]}
        reference = references(p.settings, target["category"])
        packet = {"source": source_data, "source_fidelity": p.settings.source_fidelity, "target": target,
            "research_brief": brief, "reference_cards": reference, "difficulty_profile": p.difficulty_profile}
        campaign = self.load_campaign(identity, digest(packet))
        self.save_campaign(campaign)
        if campaign.status == "rejected":
            raise DifficultyRejected("difficulty_design_exhausted")
        for index in range(p.settings.difficulty_design_max_revisions + 1):
            prior = campaign.versions[index - 1] if index else None
            if index >= len(campaign.versions):
                proposal, rid, model = await p.gateway.call("generator", p.prompts["difficulty_proposal"] + "\n" +
                    json.dumps({**packet, "previous_proposal": prior.model_dump() if prior else None,
                                "revision": index}, ensure_ascii=False), f"{identity}:depth-design:{index}:author",
                    DifficultyProposal)
                campaign.versions.append(DifficultyProposalVersion(
                    proposal_id=digest({"identity": identity, "proposal": index})[:24],
                    parent_proposal_id=prior.proposal_id if prior else None, proposal=proposal,
                    author_request=rid, author_actual_model=model))
                self.save_campaign(campaign)
            version = campaign.versions[index]
            if version.proposal.outcome == "unavailable":
                version.errors = ["proposal_unavailable"]
                campaign.status = "rejected"
                self.save_campaign(campaign)
                raise DifficultyRejected("difficulty_design_unavailable:" + version.proposal.reason)
            result = version.proposal.result
            solution = version.proposal.proof.bind(result)
            errors = known_result_errors(result, {source.id: source}, source_fidelity=p.settings.source_fidelity)
            errors += bottleneck_mask_errors(solution)
            errors += difficulty_design_errors(solution, target["category"], p.difficulty_profile, True)
            if not errors and version.blueprint is None:
                review, rid, model = await p.review_blueprint(solution, source_data, target,
                    f"{identity}:depth-design:{index}:science", result, construction_reference=reference)
                version.blueprint, version.blueprint_request, version.blueprint_actual_model = review, rid, model
                self.save_campaign(campaign)
            if version.blueprint and not version.blueprint.passed(source_fidelity=p.settings.source_fidelity):
                errors.append("blueprint_rejected")
            if not errors and version.challenge is None:
                challenge, rid, model = await p.consistency_call(p.prompts["difficulty_challenge"] + "\n" +
                    json.dumps({"stage": "before_lock", "reference_cards": reference,
                        "solution": solution.model_dump(), "provisional_result": result.model_dump(),
                        "solver_inputs": solution.design.difficulty.mask.retained_information}, ensure_ascii=False),
                    f"{identity}:depth-design:{index}:challenge", DifficultyChallenge)
                self.check_mapping(challenge, solution, rid)
                version.challenge, version.challenge_request, version.challenge_actual_model = challenge, rid, model
                self.save_campaign(campaign)
            if version.challenge and version.challenge.verdict != "sufficient":
                errors.append("depth_" + version.challenge.verdict)
            version.errors = errors
            if not errors and version.challenge:
                campaign.status = "passed"
                self.save_campaign(campaign)
                return version, DifficultyOrigin(root_id=identity, source_sha256=source.sha256,
                    protocol_sha256=self.protocol, campaign_input_sha256=campaign.input_sha256, selected=version)
            self.save_campaign(campaign)
            if version.challenge and version.challenge.verdict == "uncertain":
                # 不确定不是过易证据，不据此加难；接口失败也由原网关另行保留。
                raise DifficultyRejected("difficulty_design_unverified")
        campaign.status = "rejected"
        self.save_campaign(campaign)
        raise DifficultyRejected("difficulty_design_exhausted")

    @staticmethod
    def check_mapping(challenge, solution, request):
        if max(challenge.indispensable_proof_entries, default=0) > len(solution.derivation):
            raise ModelResponseError("consistency", request, "invalid_difficulty_proof_mapping")

    async def audit_item(self, candidate, source_data, salt):
        audit, rid, model = await self.p.gateway.call("auditor", self.p.prompts["audit"] + "\n" +
            json.dumps({"source": source_data, "source_fidelity": candidate.source_fidelity,
                "solution": candidate.solution.model_dump(), "item": candidate.item.model_dump(),
                "parent": None}, ensure_ascii=False), salt, Audit)
        return audit, rid, model

    async def generate(self, source, target, identity, brief, existing=None):
        p = self.p
        if existing and existing.authoring_complete:
            return existing
        if existing and existing.status == "rejected" and (existing.difficulty_rounds or existing.verifier_assessment
                or (existing.preflight_repair and existing.preflight_repair.owner == "certificate")):
            existing.authoring_complete = True
            p.store.save(existing)
            return existing
        version, origin = await self.design(source, target, identity, brief)
        solution = version.proposal.proof.bind(version.proposal.result)
        source_data = {**source.model_dump(), "text": source.text[:p.settings.max_source_chars]}
        candidate = existing
        start = existing.authoring_repairs_completed if existing else 0
        for quality_revision in range(start, p.settings.max_repairs + 1):
            if candidate is None or candidate.status == "rejected":
                item, rid, _ = await p.gateway.call("generator", p.prompts["align"] + "\n" + json.dumps({
                    "solution": solution.model_dump(), "source_fidelity": p.settings.source_fidelity,
                    "target": target, "previous_question": candidate.item.model_dump() if candidate else None,
                    "repair_feedback": candidate.reasons if candidate else None}, ensure_ascii=False),
                    f"{identity}:depth-align:{quality_revision}", Question)
                if item.answer != solution.answer:
                    raise ModelResponseError("generator", rid, "difficulty_locked_gold_changed")
                if (item.category, item.subtopic) != (target["category"], target["subtopic"]):
                    raise ModelResponseError("generator", rid, "difficulty_requested_category_mismatch")
                candidate = Candidate(id=identity, parent_id=None, root_id=identity, generation=0,
                    operator="difficulty_adversarial_seed", source_ids=[source.id], source_fidelity=p.settings.source_fidelity,
                    solution=solution, item=item, audit=Audit(valid=False, self_contained=False, uniquely_answerable=False,
                        answer_supported=False, substantive_delta=False, category_correct=False, no_shortcut=False,
                        reason="Pending independent actual-question review."), known_result=version.proposal.result,
                    blueprint_audit=version.blueprint, blueprint_request=version.blueprint_request,
                    blueprint_actual_model=version.blueprint_actual_model,
                    blueprint_fingerprint=p.blueprint_stamp(solution, source_data, target, version.proposal.result),
                    difficulty_origin=origin, status="quality_seed", run_id=p.run_id, authoring_complete=False,
                    authoring_repairs_completed=quality_revision)
                candidate.audit, _, _ = await self.audit_item(candidate, source_data,
                    f"{identity}:depth-align:{quality_revision}:audit")
                p.store.save(candidate)
            if await p.preflight(candidate):
                candidate.authoring_complete = True
                p.store.save(candidate)
                return candidate
            # 进入难度对抗后、证书修复耗尽后、进入verifier后，均不得回到外层整包作者循环。
            if (candidate.difficulty_rounds or candidate.verifier_assessment
                    or (candidate.preflight_repair and candidate.preflight_repair.owner == "certificate")):
                break
            candidate.authoring_repairs_completed = quality_revision + 1
            p.store.save(candidate)
        candidate.authoring_complete = True
        p.store.save(candidate)
        return candidate

    async def question(self, candidate, sources, correctness):
        p = self.p
        if not valid_origin(candidate, sources, self.protocol):
            candidate.status, candidate.reasons = "rejected", ["missing_or_stale_difficulty_origin"]
            p.store.save(candidate)
            return False
        if has_valid_difficulty(candidate, sources, self.protocol):
            return await correctness(candidate)
        if candidate.trials or candidate.deadline_outcomes or candidate.verifier_assessment or candidate.verifier_history:
            candidate.status, candidate.reasons = "rejected", ["difficulty_frozen_version_requires_valid_receipt"]
            p.store.save(candidate)
            return False
        if candidate.difficulty_rounds and not valid_history(candidate, sources, self.protocol):
            raise CacheIntegrityError("Difficulty challenge history is invalid")
        if candidate.difficulty_rounds and candidate.difficulty_rounds[-1].input_sha256 != question_input(candidate, sources):
            # 唯一允许的变化是本方法保存的题面修订；当前版本须已通过新的科学检查后再追加审查。
            prior = candidate.difficulty_rounds[-1]
            if (prior.challenge.verdict != "too_easy" or not prior.revision
                    or candidate.item != prior.item.model_copy(update={"question": prior.revision.question})
                    or candidate.solution != prior.solution):
                raise CacheIntegrityError("Unrecorded difficulty question change")
        if candidate.difficulty_rounds and candidate.difficulty_rounds[-1].challenge.verdict == "sufficient":
            raise CacheIntegrityError("Difficulty approval history is invalid")
        limit = p.settings.difficulty_mask_max_revisions if candidate.generation == 0 else 0
        for index in range(limit + 1):
            if not await correctness(candidate):
                return False
            input_hash = question_input(candidate, sources)
            current = next((r for r in candidate.difficulty_rounds if r.input_sha256 == input_hash), None)
            if current is None:
                challenge, rid, model = await p.consistency_call(p.prompts["difficulty_challenge"] + "\n" + json.dumps({
                    "stage": "actual_question", "reference_cards": references(p.settings, candidate.item.category),
                    "question": candidate.item.question, "required_outputs": candidate.item.required_outputs,
                    "solution": candidate.solution.model_dump(), "locked_result": candidate.known_result.model_dump(),
                    "independent_solution": candidate.consistency.independent_solution.model_dump()}, ensure_ascii=False),
                    f"{candidate.id}:depth-question:{input_hash}", DifficultyChallenge)
                self.check_mapping(challenge, candidate.solution, rid)
                current = DifficultyQuestionRound(input_sha256=input_hash,
                    parent_input_sha256=candidate.difficulty_rounds[-1].input_sha256 if candidate.difficulty_rounds else "",
                    protocol_sha256=self.protocol, item=candidate.item.model_copy(deep=True),
                    solution=candidate.solution.model_copy(deep=True), audit=candidate.audit.model_copy(deep=True),
                    bottleneck=candidate.bottleneck_assessment.model_copy(deep=True),
                    consistency=candidate.consistency.model_copy(deep=True), challenge=challenge, request_id=rid,
                    actual_model=model)
                candidate.difficulty_rounds.append(current)
                p.store.save(candidate)
                p.log("difficulty_question_challenge", candidate=candidate.id,
                    revision=len(candidate.difficulty_rounds) - 1, verdict=challenge.verdict, request_id=rid,
                    actual_model=model)
            if current.challenge.verdict == "sufficient":
                return True
            if current.challenge.verdict != "too_easy" or len(candidate.difficulty_rounds) > limit:
                candidate.status, candidate.reasons = "rejected", ["difficulty_question_" + current.challenge.verdict]
                p.store.save(candidate)
                return False
            revision, rid, model = await p.gateway.call("generator", p.prompts["difficulty_mask_repair"] + "\n" + json.dumps({
                "item": candidate.item.model_dump(), "solution": candidate.solution.model_dump(),
                "challenge": current.challenge.model_dump(),
                "reference_cards": references(p.settings, candidate.item.category)}, ensure_ascii=False),
                f"{candidate.id}:depth-question:{input_hash}:mask-revision", DifficultyMaskRevision)
            if max(revision.concealed_proof_entries) > len(candidate.solution.derivation):
                raise ModelResponseError("generator", rid, "invalid_difficulty_mask_mapping")
            item = candidate.item.model_copy(update={"question": revision.question})
            if item.question == candidate.item.question:
                raise ModelResponseError("generator", rid, "unchanged_difficulty_mask_revision")
            pending = candidate.model_copy(update={"item": item})
            source_data = [{**sources[sid].model_dump(), "text": sources[sid].text[:p.settings.max_source_chars]}
                           for sid in candidate.source_ids]
            audit, _, _ = await self.audit_item(pending, source_data,
                f"{candidate.id}:depth-question:{input_hash}:revised-audit")
            # 先获得新题独立审查再原子保存；崩溃时靠同payload收据复用，不让旧audit放行新题。
            candidate.item, candidate.audit = item, audit
            current.revision, current.revision_request, current.revision_actual_model = revision, rid, model
            candidate.consistency, candidate.bottleneck_assessment, candidate.preflight_repair = None, None, None
            candidate.status = "quality_seed"
            p.store.save(candidate)
        raise AssertionError("Bounded difficulty loop did not terminate")


def summarize_difficulty(pipeline, candidates):
    builder = DifficultyAdversary(pipeline)
    campaigns = []
    for ref in pipeline.difficulty_checkpoints.values():
        pointer = CheckpointPointer.model_validate(ref)
        campaigns.append(DifficultyCampaign.model_validate(builder.objects.get(pointer.key, pointer.sha256, pointer.size)))
    proposals = [version for campaign in campaigns for version in campaign.versions]
    rounds = [record for candidate in candidates for record in candidate.difficulty_rounds]
    return {"pilot_only": True, "empirical_difficulty_claim": False,
        "design_campaigns": len(campaigns), "provisional_versions": len(proposals),
        "design_passed": sum(c.status == "passed" for c in campaigns),
        "design_exhausted": sum(c.status == "rejected" for c in campaigns),
        "design_pending_or_unverified": sum(c.status == "pending" for c in campaigns),
        "question_reviewed_versions": len(rounds),
        "question_mask_revisions": sum(r.revision is not None for r in rounds),
        "question_sufficient": sum(has_valid_difficulty(c, {s.id: s for s in pipeline.store.sources()},
            builder.protocol) for c in candidates),
        "question_uncertain": sum(r.challenge.verdict == "uncertain" for r in rounds),
        "question_scientific_defects": sum(r.challenge.verdict == "invalid" for r in rounds),
        "question_too_easy_reviews": sum(r.challenge.verdict == "too_easy" for r in rounds)}
