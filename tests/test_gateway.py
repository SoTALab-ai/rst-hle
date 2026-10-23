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
    def handler(request):
        return httpx.Response(200, json={"model": model, "choices": [
            {"message": {"content": content}, "finish_reason": finish}]})

    install_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError):
        asyncio.run(Gateway(settings, tmp_path).call("solver", "Q", "sample:0"))
