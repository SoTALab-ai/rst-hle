"""排版误杀的最小复现；全部为合成夹具，不含论文/题库原文。"""
import asyncio
import hashlib
import json

import pytest

from diamond_factory.gates import evidence_errors
from diamond_factory.llm import CacheIntegrityError
from diamond_factory.models import VerifierSpec, VerifierVersion, digest
from diamond_factory.provenance import match_quote
from diamond_factory.rejection_replay import load_sealed_evidence
from diamond_factory.verifier import anchor_repairs_valid, repair_spec_anchors, spec_errors


@pytest.mark.parametrize(('quote', 'text'), [
    (r'The value $b/\mu$ is finite.', r'The value \(b/\mu\) is finite.'),
    (r'The degree is $\deg(x)-2.', r'The degree is $\deg(x)-2$.'),
    (r'The set $\mathcal{N}$ is finite.', r'The set \(\mathcal N\) is finite.'),
    ('The integral in Φ diverges.', r'The integral in $\Phi$ diverges.'),
    (r'\(f(x)=x^2,', '\\[\n' + r'f(x)=x^2,' + '\n\\]'),
])
def test_presentation_equivalence_returns_original_span(quote, text):
    match = match_quote(quote, text)
    assert match is not None
    assert text[match.start:match.end] == match.literal


@pytest.mark.parametrize(('quote', 'text'), [
    (r'The Christoffel symbol cancels.', r'The term $\Gamma^b_{ac}$ cancels.'),
    (r'Since the nucleus $N$ is finite.', r'Since $N$ is finite.'),
    (r'$x^2+1$', r'\(x^2-1\)'),
    (r'$x_i$', r'$x_j$'),
    (r'$A x$', r'$a x$'),
    (r'$x^{-1}$', r'$x-1$'),
    (r'$\alpha x$', r'$\alphax$'),
    (r'holds for x>0', 'holds for x>=0'),
    ('first claim second claim', 'first claim hidden premise second claim'),
])
def test_mathematical_and_semantic_changes_still_rejected(quote, text):
    assert match_quote(quote, text) is None


def spec(item, anchor):
    return VerifierSpec(requirements=[{'target_output': item.required_outputs[0],
        'question_anchor': anchor, 'acceptance_rule': 'Return only the requested exact value.',
        'equivalent_forms': ['the same value']}], conditions=[{'condition': 'retained literal data',
        'kind': 'explicit', 'question_anchor': anchor,
        'justification': 'The condition must still undergo independent semantic review.'}],
        interpretation='Only the requested output may be graded; no additional proof requirement.')


@pytest.mark.parametrize(('question', 'anchor'), [
    ('Let π:P→G be a continuous surjection from a profinite group.',
     'π:P→G is a continuous surjection from a profinite group.'),
    ('Let A be a matrix-valued object in a trace setup.', 'A matrix-valued object in a trace setup.'),
    ('Define the potential:\n\\[\nf(x)=x^2,\n\\]', r'\(f(x)=x^2,'),
])
def test_anchor_projection_preserves_rules_and_seals_raw_spec(candidate, question, anchor):
    item = candidate.item.model_copy(update={'question': question})
    raw = spec(item, anchor)
    fixed, receipts = repair_spec_anchors(raw, item)
    assert spec_errors(raw, item) and not spec_errors(fixed, item)
    assert raw.requirements[0].question_anchor == anchor
    assert fixed.requirements[0].acceptance_rule == raw.requirements[0].acceptance_rule
    version = VerifierVersion(item=item, spec=fixed, spec_request='cached-only', spec_actual_model='fixture',
        spec_raw_sha256=digest(raw.model_dump()), anchor_repairs=receipts)
    assert anchor_repairs_valid(version)
    assert not anchor_repairs_valid(version.model_copy(update={'spec_raw_sha256': ''}))
    version.anchor_repairs[0].start += 1
    assert not anchor_repairs_valid(version)


def test_anchor_repair_does_not_repair_wrong_output_or_condition(candidate):
    item = candidate.item.model_copy(update={'question': 'Let x>0 and define the exact output on this domain.'})
    raw = spec(item, 'Let x<0')
    fixed, receipts = repair_spec_anchors(raw, item)
    assert not receipts and spec_errors(fixed, item)
    fixed.requirements[0].target_output = 'an unrequested output'
    assert 'verifier_output_coverage_mismatch' in spec_errors(fixed, item)


def test_projection_cannot_choose_between_two_presentation_equivalent_locations(candidate):
    item = candidate.item.model_copy(update={'question': r'First $f(x)=x^2$; second \[f(x)=x^2\].'})
    raw = spec(item, r'\(f(x)=x^2\)')
    assert match_quote(raw.requirements[0].question_anchor, item.question)
    fixed, receipts = repair_spec_anchors(raw, item)
    assert not receipts and spec_errors(fixed, item)


def test_evidence_gate_uses_same_conservative_matcher(candidate, source):
    source = source.model_copy(update={'text': r'The synthetic quantity \(b/\mu\) is finite.'})
    ev = candidate.item.evidence[0].model_copy(update={'source_id': source.id,
        'quote': r'The synthetic quantity $b/\mu$ is finite.'})
    assert not evidence_errors([ev], {source.id: source})
    ev.quote = r'The synthetic quantity $b/\mu$ is infinite.'
    assert evidence_errors([ev], {source.id: source})


def test_draft_recovers_format_without_buying_repair_or_solver(tmp_path, settings, source, candidate):
    from test_qc import setup_pipeline

    from diamond_factory.verifier import VerifierBuilder

    candidate.item.question = 'Let A be a matrix-valued object in a trace setup.'
    raw = spec(candidate.item, 'A matrix-valued object in a trace setup.')
    pipe, _store, gateway = setup_pipeline(tmp_path, settings, source, candidate)

    async def cached_only(role, prompt, salt, schema, **kwargs):
        gateway.calls.append(role)
        assert role == 'generator' and schema is VerifierSpec
        return raw, 'paid-original-response', 'fixture-model'

    gateway.call = cached_only
    version = asyncio.run(VerifierBuilder(pipe).draft(candidate, {source.id: source}, 0))
    assert version.anchor_repairs and version.result is None
    assert gateway.calls == ['generator']  # 没有格式重购、没有GLM，尚未完成对抗。


def test_offline_export_rejects_changed_receipts_and_path_escape(tmp_path):
    (tmp_path/'traces').mkdir()
    trace = {'request_id': 'paid', 'role': 'solver', 'content': 'saved answer'}
    trace['integrity_sha256'] = digest(trace)
    raw = json.dumps(trace).encode()
    (tmp_path/'traces/paid.json').write_bytes(raw)
    manifest = {'origin_run': 'original', 'files': {'traces/paid.json': hashlib.sha256(raw).hexdigest()}}
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps(manifest))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    assert load_sealed_evidence(tmp_path, sha)[1]['paid']['content'] == 'saved answer'
    (tmp_path/'traces/paid.json').write_text('changed paid result')
    with pytest.raises(CacheIntegrityError, match='changed'):
        load_sealed_evidence(tmp_path, sha)
    manifest['files'] = {'../escaped': '0'*64}
    path.write_text(json.dumps(manifest))
    with pytest.raises(CacheIntegrityError, match='escaped'):
        load_sealed_evidence(tmp_path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_false_stronger_adjoint_bound_has_exact_counterexample():
    from fractions import Fraction

    # w=E12时均值恰为t^-2（d=6log(t)）；K*t^-3更小不能因“更强”而接受。
    # 任意K取t>K即反例；下列整数是该推理的精确算术控制。
    for constant in (1, 2, 7, 100):
        t = constant+1
        assert Fraction(1, t*t) > Fraction(constant, t*t*t)
