import asyncio
import hashlib
import io
import json
import zipfile

import httpx
import pytest
from pypdf import PdfWriter

from diamond_factory.pdf_ocr import OCRFailure
from diamond_factory.pdf_precision import MinerUPrecision, content_from_zip, decode_blocks, page_text


def zip_content(blocks, *, unsafe=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("result_content_list.json", json.dumps(blocks))
        # 插图即使包含有害内容，也不提取、不交给作者。
        z.writestr("../outside" if unsafe else "images/plot.jpg", b"DO_NOT_READ_FIGURE")
    return buffer.getvalue()


def blocks():
    return [{"type": "text", "page_idx": 0, "text": "A complete synthetic mathematical definition of the target."},
            {"type": "equation", "page_idx": 0, "text": "x^2+1=2\\tag{1}"},
            {"type": "image", "page_idx": 0, "image_caption": ["SECRET_FIGURE_CAPTION"]},
            {"type": "text", "page_idx": 1, "text": "Second physical page with a separately defined exact quantity."}]


def test_only_body_formulas_and_tables_enter_original_page_mapping():
    body, b, metadata = content_from_zip(zip_content(blocks()), 2)
    first = page_text(b, 1)
    assert "x^2+1" in first and "Second physical page" not in first
    assert "SECRET_FIGURE_CAPTION" not in first and "DO_NOT_READ_FIGURE" not in body.decode()
    assert "separately defined" in page_text(b, 2)
    assert metadata["cloud_output_contract"] == "legacy_content_list_v1"


def test_unsafe_archive_out_of_range_and_image_only_equation_are_rejected():
    with pytest.raises(ValueError, match="Unsafe"):
        content_from_zip(zip_content(blocks(), unsafe=True), 2)
    with pytest.raises(ValueError, match="page index"):
        decode_blocks(json.dumps(blocks()), 1)
    bad = decode_blocks(json.dumps([{"type": "equation", "page_idx": 0, "text": ""}]), 1)
    with pytest.raises(ValueError, match="Image-only equation"):
        page_text(bad, 1)


def test_precision_upload_once_for_multiple_pages_and_no_token_in_journal(tmp_path):
    pdf = tmp_path / "main.pdf"
    w = PdfWriter()
    w.add_blank_page(width=100, height=100)
    w.add_blank_page(width=100, height=100)
    w.write(pdf)
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    posts, uploads = [], []
    token = "TEST_PRECISION_SECRET"

    def handler(request):
        if request.method == "POST":
            assert request.headers["Authorization"] == "Bearer " + token
            posts.append(request)
            return httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "batch", "file_urls": ["https://uploads.example.org/pdf"]}})
        if request.method == "PUT":
            assert not request.headers.get("Authorization")
            uploads.append(request)
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{
                "state": "done", "data_id": sha, "full_zip_url": "https://cdn.example.org/result.zip"}]}})
        assert not request.headers.get("Authorization")
        return httpx.Response(200, content=zip_content(blocks()))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            g = MinerUPrecision(tmp_path / "ocr", token=token, client=c, poll_interval=0)
            assert "x^2" in (await g.parse(pdf, 1))[0]
            assert "Second physical" in (await g.parse(pdf, 2))[0]
            fresh = MinerUPrecision(tmp_path / "ocr", token=token, client=c)
            assert "Second physical" in (await fresh.parse(pdf, 2))[0]
            assert len(posts) == len(uploads) == 1
            output = tmp_path / "ocr" / sha / "content.json"
            output.write_text("[]")
            with pytest.raises(OCRFailure, match="Corrupt structured OCR"):
                await MinerUPrecision(tmp_path / "ocr", token=token, client=c).parse(pdf, 1)
        assert token not in "".join(p.read_text() for p in (tmp_path / "ocr").rglob("*.json"))

    asyncio.run(exercise())
