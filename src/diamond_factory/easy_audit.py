"""首四次全对的只读证据核对；原因由人工标注，不靠关键词推断或购买新答卷。"""
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .llm import CacheIntegrityError
from .models import Candidate, StrictModel, digest


class ManualFinding(StrictModel):
    index: int = Field(ge=1)
    root: str
    cause: Literal['shallow_target', 'scaffolded_question', 'valid_short_route']
    mechanism: str = Field(min_length=3)
    observation: str = Field(min_length=20)


class ManualFindings(StrictModel):
    method: str
    labels: dict[str, str]
    cases: list[ManualFinding] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_cases(self):
        if ({c.index for c in self.cases} != set(range(1, len(self.cases) + 1))
                or len({c.root for c in self.cases}) != len(self.cases)):
            raise ValueError('Manual audit cases must have consecutive unique indices and roots')
        return self


def summarize_easy_cohort(candidates, traces, root_names, findings):
    """返回脱敏核对结果；输入对象、评分、原报告和原数据库都不修改。"""
    cohort = [c for c in candidates if 'too_easy:first_four_all_correct' in c.reasons]
    if {c.id for c in cohort} != set(root_names):
        raise ValueError('Historical report and candidate cohort disagree')
    if len({root_names[c.id] for c in cohort}) != len(cohort):
        raise ValueError('Multiple easy versions share a root')
    by_root = {root_names[c.id]: c for c in cohort}
    if set(by_root) != {f.root for f in findings.cases}:
        raise ValueError('Manual findings do not cover the complete easy cohort')

    def checked(request_id, role, expected, actual_model, *, structured=False):
        trace = traces.get(request_id)
        if (not trace or trace.get('request_id') != request_id or trace.get('role') != role
                or trace.get('response_error') or trace.get('actual_model') != actual_model
                or trace.get('integrity_sha256') != digest(
                    {k: v for k, v in trace.items() if k != 'integrity_sha256'})):
            raise CacheIntegrityError('Audit response is missing, changed or not successful')
        content = json.loads(trace['content']) if structured else trace['content']
        if content != expected:
            raise CacheIntegrityError('Audit response does not match the stored candidate')
        return trace['integrity_sha256']

    rows, seen = [], set()
    for finding in sorted(findings.cases, key=lambda f: f.index):
        candidate = by_root[finding.root]
        if (candidate.status != 'rejected' or candidate.deadline_outcomes or len(candidate.trials) != 4
                or {t.sample_id for t in candidate.trials} != {0, 1, 2, 3}
                or any(t.judgment.correct != 'yes' for t in candidate.trials)):
            raise ValueError('Easy cohort must contain exactly four actual correct answers per case')
        receipts = []
        for trial in sorted(candidate.trials, key=lambda t: t.sample_id):
            if trial.solver_request in seen:
                raise CacheIntegrityError('A solver response was reused across audit samples')
            seen.add(trial.solver_request)
            receipts.append({
                'sample_id': trial.sample_id,
                'solver_request': trial.solver_request,
                'solver_seal': checked(trial.solver_request, 'solver', trial.response, trial.solver_actual_model),
                'judge_request': trial.judge_request,
                'judge_seal': checked(trial.judge_request, 'judge', trial.judgment.model_dump(),
                                      trial.judge_actual_model, structured=True)})
        consistency = candidate.consistency
        if not consistency:
            raise ValueError('Missing historical independent solution')
        checked(consistency.independent_request, 'consistency', consistency.independent_solution.model_dump(),
                consistency.independent_actual_model, structured=True)
        mask = candidate.solution.design.difficulty.mask
        row = {**finding.model_dump(), 'candidate_id': candidate.id, 'category': candidate.item.category,
            'question_sha256': digest(candidate.item.question), 'golden_sha256': digest(candidate.item.answer),
            'solution_sha256': digest(candidate.solution.model_dump()), 'candidate_sha256': digest(candidate.model_dump()),
            'masked_proof_entries': mask.proof_step_indices, 'full_proof_entries': len(candidate.solution.derivation),
            'historical_blueprint_pass': bool(candidate.blueprint_audit and candidate.blueprint_audit.passed(
                source_fidelity=candidate.source_fidelity)),
            'historical_no_shortcut': candidate.audit.no_shortcut,
            'historical_mask_checks': {name: getattr(candidate.bottleneck_assessment.review, name).verdict
                for name in candidate.bottleneck_assessment.review.check_names()},
            'construction_type': candidate.solution.design.construction_type,
            'blind_solution_sha256': digest(consistency.independent_solution.model_dump()),
            'receipts_sha256': digest(receipts)}
        rows.append(row)
    return {'method': findings.method, 'labels': findings.labels, 'case_count': len(rows),
        'existing_glm_answers_read': len(seen), 'new_http_requests': 0, 'new_glm_requests': 0,
        'new_api_cost_usd': 0, 'official_regrading': False, 'counterfactual_new_prompt_replay': False,
        'by_category': dict(Counter(r['category'] for r in rows)),
        'primary_causes': dict(Counter(r['cause'] for r in rows)),
        'primary_causes_by_category': {category: dict(Counter(
            r['cause'] for r in rows if r['category'] == category)) for category in ('Math', 'Physics')},
        'historical_approvals': {
            'blueprint': sum(r['historical_blueprint_pass'] for r in rows),
            'composite_no_shortcut': sum(r['historical_no_shortcut'] for r in rows),
            'all_mask_checks_pass': sum(all(v == 'pass' for v in r['historical_mask_checks'].values()) for r in rows)},
        'cases': rows}


def audit_exports(directory, findings_path):
    """核对既有冻结导出，不连接网关和数据库；只返回哈希与人工脱敏记录。"""
    directory = Path(directory).resolve()
    manifest_file = directory / 'hashes.json'
    manifest = json.loads(manifest_file.read_text())
    required = {'candidates-private.json', 'requests-private.json', 'experiment-report-private.json'}
    if not required <= manifest.keys():
        raise CacheIntegrityError('Incomplete frozen export manifest')
    for name, sha in manifest.items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise CacheIntegrityError('Frozen audit export changed or escaped its directory')
    read = lambda name: json.loads((directory / name).read_text())
    candidates = [Candidate.model_validate(c) for c in read('candidates-private.json')]
    raw = read('requests-private.json')
    traces = {t['request_id']: t for t in raw}
    if len(traces) != len(raw) or len({c.id for c in candidates}) != len(candidates):
        raise CacheIntegrityError('Duplicate exported candidate or model response')
    root_names = {v['id']: root['root'] for report in read('experiment-report-private.json')
        for root in report['roots'] for v in root['versions']
        if 'too_easy:first_four_all_correct' in v['reasons']}
    findings = ManualFindings.model_validate_json(Path(findings_path).read_text())
    result = summarize_easy_cohort(candidates, traces, root_names, findings)
    result['evidence_manifest_sha256'] = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    result['manual_findings_sha256'] = hashlib.sha256(Path(findings_path).read_bytes()).hexdigest()
    return result
