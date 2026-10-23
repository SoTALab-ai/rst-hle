import asyncio
import json

import httpx
import pytest

from diamond_factory.llm import BudgetExceeded, Gateway


def install_mock(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("NEW_API_BASE_URL", "https://testing.invalid/v1")
    monkeypatch.setenv("NEW_API_KEY", "TEST_SECRET_MUST_NOT_BE_LOGGED")


def test_cache_samples_are_distinct_and_no_secret_logged(tmp_path, settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "glm-5.2", "choices": [
            {"message": {"content": "answer"}, "finish_reason": "stop"}], "usage": {"total_tokens": 2}})

    install_mock(monkeypatch, handler)
    gateway = Gateway(settings, tmp_path)

    async def exercise():
        a = await gateway.call("solver", "same question", "sample:0")
        assert a == await gateway.call("solver", "same question", "sample:0")
        b = await gateway.call("solver", "same question", "sample:1")
        assert a[1] != b[1]

    asyncio.run(exercise())
    assert len(calls) == 2
    assert "TEST_SECRET_MUST_NOT_BE_LOGGED" not in "".join(p.read_text() for p in tmp_path.glob("*.json"))


def test_budget_persists_across_process_instances(tmp_path, settings, monkeypatch):
    settings.max_requests = 1

    def handler(request):
        return httpx.Response(200, json={"model": "glm-5.2", "choices": [
            {"message": {"content": "answer"}, "finish_reason": "stop"}]})

    install_mock(monkeypatch, handler)
    asyncio.run(Gateway(settings, tmp_path).call("solver", "Q", "sample:0"))
    with pytest.raises(BudgetExceeded):
        asyncio.run(Gateway(settings, tmp_path).call("solver", "Q", "sample:1"))
    assert json.loads((tmp_path / "budget.json").read_text())["attempts"] == 1


@pytest.mark.parametrize("model,finish,content", [("unrequested-model", "stop", "answer"),
                                                 ("glm-5.2", "length", "partial"),
                                                 ("glm-5.2", "stop", "")])
def test_wrong_model_and_truncation_fail_closed(tmp_path, settings, monkeypatch, model, finish, content):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": model, "choices": [
            {"message": {"content": content}, "finish_reason": finish}],
            "usage": {"completion_tokens": 123}})

    install_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        asyncio.run(Gateway(settings, tmp_path).call("solver", "Q", "sample:0"))
    # Resuming preserves paid usage and cannot silently resample a rejected logical trial.
    with pytest.raises(RuntimeError):
        asyncio.run(Gateway(settings, tmp_path).call("solver", "Q", "sample:0"))
    assert len(calls) == 1
    traces = [json.loads(p.read_text()) for p in tmp_path.glob("*.json") if p.name != "budget.json"]
    assert traces[0]["usage"]["completion_tokens"] == 123
    assert traces[0]["response_error"]
    sent = json.loads(calls[0].content)
    assert "tools" not in sent and "functions" not in sent
    assert "max_tokens" not in sent and "max_completion_tokens" not in sent


def test_wall_clock_deadline_cancels_and_does_not_restart_on_resume(tmp_path, settings, monkeypatch):
    settings.request_deadline_seconds = 0.03
    settings.max_retries = 1
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(1)
        raise AssertionError("Deadline did not cancel the in-flight call")

    install_mock(monkeypatch, handler)
    gateway = Gateway(settings, tmp_path)
    with pytest.raises(RuntimeError, match="deadline_exceeded"):
        asyncio.run(gateway.call("solver", "Q", "timed:0"))
    with pytest.raises(RuntimeError, match="deadline_exceeded"):
        asyncio.run(gateway.call("solver", "Q", "timed:0"))
    assert len(calls) == 1
    receipt = next(json.loads(p.read_text()) for p in tmp_path.glob("*.json") if p.name != "budget.json")
    assert receipt["usage_unavailable"] is True
    assert receipt["elapsed_seconds"] < 0.3


def test_deadline_includes_retry_backoff(tmp_path, settings, monkeypatch):
    settings.request_deadline_seconds = 0.03
    settings.max_retries = 1
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    install_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="deadline_exceeded"):
        asyncio.run(Gateway(settings, tmp_path).call("solver", "Q", "retry:0"))
    assert len(calls) == 1


def test_long_solver_queue_leaves_capacity_for_judging_and_persistence(tmp_path,settings,monkeypatch):
    settings.concurrency=3;settings.solver_concurrency=2
    active=0;peak=0;started=0

    async def exercise():
        nonlocal active,peak,started
        two_running=asyncio.Event();release=asyncio.Event()

        async def handler(request):
            nonlocal active,peak,started
            model=json.loads(request.content)['model']
            if model==settings.solver.model:
                active+=1;started+=1;peak=max(peak,active)
                if started==2:two_running.set()
                try:await release.wait()
                finally:active-=1
            return httpx.Response(200,json={'model':model,'choices':[
                {'message':{'content':'complete'},'finish_reason':'stop'}]})

        install_mock(monkeypatch,handler)
        gateway=Gateway(settings,tmp_path)
        tasks=[asyncio.create_task(gateway.call('solver','Q','sample:'+str(i))) for i in range(3)]
        try:
            await asyncio.wait_for(two_running.wait(),1)
            judged=await asyncio.wait_for(gateway.call('judge','fixed response','judge:0'),1)
            assert judged[0]=='complete' and started==2 and active==2
        finally:
            release.set()
            await asyncio.gather(*tasks)

    asyncio.run(exercise())
    assert peak==2 and started==3
    with pytest.raises(ValueError,match='global concurrency'):
        type(settings).model_validate(dict(settings.model_dump(),solver_concurrency=4))
