import asyncio
import json

import httpx
import pytest
from test_gateway import install_mock

from diamond_factory.llm import Gateway


def test_page_images_are_cached_and_image_change_changes_identity(tmp_path, settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"model": settings.consistency.model, "choices": [
            {"message": {"content": "transcription"}, "finish_reason": "stop"}]})

    install_mock(monkeypatch, handler)
    png = b"\x89PNG\r\n\x1a\npage-a"

    async def exercise():
        g = Gateway(settings, tmp_path)
        first = await g.call("consistency", "Read page", "page:1", images=[png])
        assert first == await g.call("consistency", "Read page", "page:1", images=[png])
        changed = await g.call("consistency", "Read page", "page:1", images=[png + b"changed"])
        assert changed[1] != first[1]

    asyncio.run(exercise())
    assert len(calls) == 2
    blocks = calls[0]["messages"][1]["content"]
    assert blocks[0]["text"] == "Read page"
    assert blocks[1]["image_url"]["detail"] == "high"
    assert "TEST_SECRET_MUST_NOT_BE_LOGGED" not in "".join(p.read_text() for p in tmp_path.glob("*.json"))


@pytest.mark.parametrize("role,images", [
    ("solver", [b"\x89PNG\r\n\x1a\n"]), ("judge", [b"\x89PNG\r\n\x1a\n"]),
    ("consistency", [b"<html>not an image"]), ("consistency", [b"\x89PNG\r\n\x1a\n"] * 13),
])
def test_images_cannot_enter_solver_or_bypass_limits(tmp_path, settings, monkeypatch, role, images):
    install_mock(monkeypatch, lambda r: pytest.fail("Invalid image request reached HTTP"))
    with pytest.raises(ValueError):
        asyncio.run(Gateway(settings, tmp_path).call(role, "Read", "bad", images=images))
