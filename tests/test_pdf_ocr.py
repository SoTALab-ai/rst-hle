import asyncio
import json

import httpx
import pytest

from diamond_factory.pdf_ocr import MinerUAgent, OCRFailure


def test_real_task_resume_does_not_submit_again_and_corrupt_output_is_rejected(tmp_path):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF- synthetic transport test")
    calls = []

    def handler(request):
        calls.append(request.method + " " + request.url.path)
        assert not request.headers.get("Authorization")
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {
                "task_id": "task-1", "file_url": "https://uploads.example.org/pdf"}})
        if request.method == "PUT":
            assert request.content == pdf.read_bytes()
            return httpx.Response(200)
        if request.url.path.endswith("task-1"):
            return httpx.Response(200, json={"code": 0, "data": {
                "task_id": "task-1", "state": "done", "markdown_url": "https://cdn.example.org/md"}})
        return httpx.Response(200, text="An exact expression: $x^2+1$.")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            g = MinerUAgent(tmp_path / "ocr", client=c, submission_interval=0, poll_interval=0)
            first = await g.parse(pdf, 35)
            assert await g.parse(pdf, 35) == first
            assert len(calls) == 4
            output = tmp_path / "ocr" / first[1] / "page.md"
            output.write_text("Changed exponent")
            with pytest.raises(OCRFailure, match="Corrupt OCR Markdown"):
                await g.parse(pdf, 35)
            assert len(calls) == 4

    asyncio.run(exercise())


def test_uncertain_submission_is_not_automatically_rebought(tmp_path):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF- transport test")
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("Lost submission response", request=request)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            g = MinerUAgent(tmp_path / "ocr", client=c, submission_interval=0)
            with pytest.raises(httpx.ReadTimeout):
                await g.parse(pdf, 1)
            with pytest.raises(OCRFailure, match="outcome unknown"):
                await g.parse(pdf, 1)
        assert len(calls) == 1

    asyncio.run(exercise())


def test_poll_timeout_resumes_original_task(tmp_path):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF- transport test")
    posts = []
    done = False

    def handler(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "data": {
                "task_id": "task", "file_url": "https://uploads.example.org/pdf"}})
        if request.method == "PUT":
            return httpx.Response(200)
        if request.url.path.endswith("task"):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "task", "state":
                "done" if done else "running", "markdown_url": "https://cdn.example.org/md" if done else None}})
        return httpx.Response(200, text="Exact: $x^{-1}$.")

    async def exercise():
        nonlocal done
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            g = MinerUAgent(tmp_path / "ocr", client=c, submission_interval=0, poll_interval=0, wait_seconds=.003)
            with pytest.raises(OCRFailure, match="resume the same task"):
                await g.parse(pdf, 2)
            done = True
            assert (await g.parse(pdf, 2))[0] == "Exact: $x^{-1}$."
        assert len(posts) == 1 and posts[0]["page_range"] == "2-2"

    asyncio.run(exercise())


def test_failed_provider_task_is_not_a_formula_pass(tmp_path):
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF- transport test")

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {
                "task_id": "task", "file_url": "https://uploads.example.org/pdf"}})
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(200, json={"code": 0, "data": {
            "task_id": "task", "state": "failed", "err_msg": "Formula extraction failed"}})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            g = MinerUAgent(tmp_path / "ocr", client=c, submission_interval=0)
            with pytest.raises(OCRFailure, match="parsing failed"):
                await g.parse(pdf, 1)
        assert not list((tmp_path / "ocr").rglob("page.md"))

    asyncio.run(exercise())
