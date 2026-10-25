"""用事件锁住慢阶段，验证逐题补测不依赖其他题完成；不靠睡眠推断并发。"""
import asyncio

import pytest
from conftest import trials
from test_experiment import PatternGateway, pricing
from test_paper_ingest import pg as pg_fixture

from diamond_factory.experiment import ExperimentPlan, QuestionExperiment, RootPlan
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import digest
from diamond_factory.store import Store


@pytest.fixture
def pg(tmp_path):
    yield from pg_fixture.__wrapped__(tmp_path)


def experiment(tmp_path, settings, source, candidate, count=2, store=None, **plan_options):
    settings.early_screen_samples = 4
    settings.difficulty_policy = 'ease_only'
    store = store or Store(tmp_path/'streaming.sqlite')
    store.add_source(source)
    roots = [RootPlan(name=f'claim{i}', source_id=source.id, category='Math', subtopic='组合与图论',
        focus=f'A synthetic independent claim used only to check scheduling order {i}') for i in range(count)]
    plan = ExperimentPlan(roots=roots, max_revisions=0, **plan_options)
    gateway = PatternGateway(candidate, {0, 1, 4, 5})
    exp = QuestionExperiment(settings, store, 'streaming', 'pilot', tmp_path/'run', gateway,
        plan=plan, pricing=pricing())
    exp.external = []

    async def author(src, parent, target, generation, attempt, brief=None):
        identity = exp.generation_identity(src, parent, target, generation, attempt, brief)
        saved = next((c for c in store.candidates() if c.id == identity), None)
        if saved:
            return saved
        c = candidate.model_copy(deep=True)
        c.id = c.root_id = identity
        c.run_id = exp.run_id
        store.save(c)
        return c

    exp.generate = author
    return exp, store, gateway, author


def test_supplement_starts_while_another_author_is_blocked_even_with_one_author_slot(
        tmp_path, settings, source, candidate):
    async def run():
        exp, _store, gateway, author = experiment(tmp_path, settings, source, candidate, root_workers=1)
        supplement_started = asyncio.Event()
        slow_author_started = asyncio.Event()
        original = gateway.call
        first_id = exp.root_identity(exp.plan.roots[0], {source.id: source})

        async def delayed_author(*args, **kwargs):
            if kwargs['brief']['root'] == 'claim1':
                slow_author_started.set()
                await supplement_started.wait()
            return await author(*args, **kwargs)

        async def call(role, prompt, salt, schema=None, **kwargs):
            if role == 'solver' and salt == first_id+':solver:4':
                await slow_author_started.wait()
                supplement_started.set()
            return await original(role, prompt, salt, schema, **kwargs)

        exp.generate, gateway.call = delayed_author, call
        result = await asyncio.wait_for(exp.run_experiment(), 3)
        assert supplement_started.is_set() and result['generated_versions'] == 2
        assert all(v['samples'] == 16 for r in result['roots'] for v in r['versions'])
        assert sum(role == 'solver' for role, _, _ in gateway.calls) == 32

    asyncio.run(run())


def test_full_evaluation_pool_does_not_block_authoring_and_respects_question_limit(
        tmp_path, settings, source, candidate):
    async def run():
        exp, _store, gateway, author = experiment(tmp_path, settings, source, candidate, count=4,
            root_workers=1, evaluation_workers=2)
        all_authored, two_evaluating, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        authors, entering = [], set()
        original = gateway.call

        async def tracked_author(*args, **kwargs):
            c = await author(*args, **kwargs)
            authors.append(c.id)
            if len(authors) == 4:
                all_authored.set()
            return c

        async def call(role, prompt, salt, schema=None, **kwargs):
            if role == 'solver' and salt.endswith(':solver:0'):
                entering.add(salt.split(':')[0])
                if len(entering) == 2:
                    two_evaluating.set()
                await release.wait()
            return await original(role, prompt, salt, schema, **kwargs)

        exp.generate, gateway.call = tracked_author, call
        task = asyncio.create_task(exp.run_experiment())
        try:
            await asyncio.wait_for(asyncio.gather(all_authored.wait(), two_evaluating.wait()), 3)
            assert len(authors) == 4 and len(entering) == 2
            release.set()
            result = await asyncio.wait_for(task, 3)
            # 同一道合成题并行完成时，串行去重收尾仍只保留一份。
            assert result['roots_with_retained_question'] == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_streaming_resume_buys_only_missing_samples_and_rejects_four_correct_without_tail(
        tmp_path, settings, source, candidate):
    exp, store, gateway, author = experiment(tmp_path, settings, source, candidate)

    async def prepare():
        for root in exp.plan.roots:
            c = await author(source, None, {'category': root.category, 'subtopic': root.subtopic},
                0, 0, brief=exp.brief(root, None))
            c.trials = trials(2 if root.name == 'claim0' else 16)[:4]
            store.save(c)

    asyncio.run(prepare())
    initial = {c.id: c.model_dump() for c in store.candidates()}
    result = asyncio.run(exp.run_experiment())
    calls = [salt for role, _, salt in gateway.calls if role == 'solver']
    first_id = exp.root_identity(exp.plan.roots[0], {source.id: source})
    assert calls and set(calls) == {f'{first_id}:solver:{i}' for i in range(4, 16)}
    for c in store.candidates():
        assert [t.model_dump() for t in c.trials[:4]] == initial[c.id]['trials']
    assert sorted(v['samples'] for r in result['roots'] for v in r['versions']) == [4, 16]
    easy = next(r for r in result['roots'] if r['root'] == 'claim1')
    assert easy['versions'][0]['decision'] == 'too_easy_rejected'
    assert store.runs()[exp.run_id]['status'] == 'completed'
    before = list(gateway.calls)
    asyncio.run(exp.run_experiment())
    assert gateway.calls == before


def test_one_question_api_failure_does_not_trigger_tail_or_cancel_other_question(
        tmp_path, settings, source, candidate):
    exp, _store, gateway, _author = experiment(tmp_path, settings, source, candidate)
    first_id = exp.root_identity(exp.plan.roots[0], {source.id: source})
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kwargs):
        if role == 'solver' and salt == first_id+':solver:1':
            gateway.calls.append((role, prompt, salt))
            raise ModelResponseError(role, digest(salt), 'http_500')
        return await original(role, prompt, salt, schema, **kwargs)

    gateway.call = call
    result = asyncio.run(exp.run_experiment())
    failed = next(r for r in result['roots'] if r['root'] == 'claim0')
    assert failed['state']['phase'] == 'prefix_failed'
    assert failed['versions'][0]['samples'] == 3
    assert not any(int(s.rsplit(':', 1)[1]) >= 4 for role, _, s in gateway.calls
        if role == 'solver' and s.startswith(first_id+':'))
    assert next(r for r in result['roots'] if r['root'] == 'claim1')['versions'][0]['samples'] == 16
    assert exp.store.runs()[exp.run_id]['status'] == 'completed_with_errors'


def test_postreview_api_failure_blocks_easing_and_remains_incomplete(tmp_path, settings, source, candidate):
    exp, _store, gateway, _author = experiment(tmp_path, settings, source, candidate, count=1)
    exp.plan.max_revisions = 2
    gateway.correct_samples = set()
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kwargs):
        if ':post-evaluation-audit-v3' in salt:
            raise ModelResponseError(role, digest(salt), 'http_500')
        return await original(role, prompt, salt, schema, **kwargs)

    gateway.call = call
    result = asyncio.run(exp.run_experiment())
    assert result['generated_versions'] == 1
    assert result['roots'][0]['state']['phase'] == 'evaluation_failed'
    assert result['roots'][0]['versions'][0]['samples'] == 16
    assert exp.store.runs()[exp.run_id]['status'] == 'completed_with_errors'


def test_pg_parallel_sampling_preserves_original_prefix_and_serializes_dedup(
        tmp_path, settings, source, candidate, pg):
    exp, store, gateway, author = experiment(tmp_path, settings, source, candidate, store=pg)
    # 预先登记与正式入口相同的清单，模拟已有四份答卷的原身份恢复。
    exp.start({'command': 'paper-question-experiment', 'plan_sha256': digest(exp.plan.model_dump()),
               'pricing_sha256': digest(exp.pricing)})

    async def prepare():
        for root in exp.plan.roots:
            c = await author(source, None, {'category': root.category, 'subtopic': root.subtopic},
                0, 0, brief=exp.brief(root, None))
            for t in trials(2)[:4]:
                t.solver_request = c.id+':paid-solver:'+str(t.sample_id)
                t.judge_request = c.id+':paid-judge:'+str(t.sample_id)
                for role, rid in [('solver', t.solver_request), ('judge', t.judge_request)]:
                    store.save_request(exp.run_id, {'request_id': rid, 'role': role, 'actual_model': role,
                        'request': {}, 'content': 'synthetic fixture only', 'salt': rid,
                        'usage': {}, 'attempts': []})
                c.trials.append(t)
            store.save(c)

    asyncio.run(prepare())
    # 后续控制网关也保存合成收据，使PG外键与真实持久化路径被实际执行。
    original = gateway.call

    async def recorded(role, prompt, salt, schema=None, **kwargs):
        result = await original(role, prompt, salt, schema, **kwargs)
        store.save_request(exp.run_id, {'request_id': result[1], 'role': role, 'actual_model': result[2],
            'request': {}, 'content': 'synthetic fixture only', 'salt': salt, 'usage': {}, 'attempts': []})
        return result

    gateway.call = recorded
    result = asyncio.run(exp.run_experiment())
    assert result['roots_with_retained_question'] == 1
    assert len([1 for role, _, _ in gateway.calls if role == 'solver']) == 24
    assert store.db.execute('SELECT count(*) FROM diamond.qc_samples').fetchone()[0] == 32
    assert store.db.execute("SELECT count(*) FROM diamond.qc_samples WHERE solver_request LIKE %s",
        ('%:paid-solver:%',)).fetchone()[0] == 8
