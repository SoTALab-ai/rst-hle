"""MinerU精准解析：每份固定PDF只上传一次；按结构化页码读取，丢弃论文插图。"""
import asyncio
import hashlib
import io
import json
import os
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Literal

import httpx
from pydantic import Field
from pypdf import PdfReader

from .models import StrictModel
from .pdf_library import public_url
from .pdf_ocr import OCRFailure, _read, _save


class ContentBlock(StrictModel):
    type: Literal["text", "equation", "table", "image", "list", "header", "footer", "page_number",
                  "page_footnote", "code", "reference"]
    page_idx: int = Field(ge=0)
    text: str = ""
    table_body: str = ""
    table_caption: list[str] = Field(default_factory=list)
    table_footnote: list[str] = Field(default_factory=list)
    list_items: list[str] = Field(default_factory=list)


def decode_blocks(raw, pages):
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) > 100_000:
        raise ValueError("Unsupported MinerU content-list contract")
    if any(not isinstance(b, dict) for b in values):
        raise ValueError("Invalid MinerU block")
    blocks = [ContentBlock.model_validate({k: b[k] for k in ContentBlock.model_fields if k in b}) for b in values]
    if any(b.page_idx >= pages for b in blocks):
        raise ValueError("MinerU page index exceeds the original PDF")
    return blocks


def page_text(blocks, page):
    pieces = []
    for b in blocks:
        if b.page_idx != page - 1 or b.type in ("image", "header", "footer", "page_number"):
            continue
        if b.type == "equation":
            if not b.text.strip():
                raise ValueError("Image-only equation is not a usable formula transcription")
            pieces.append("$$\n" + b.text + "\n$$")
        elif b.type == "table":
            if not b.table_body.strip():
                raise ValueError("Image-only table is not a usable mathematical table")
            pieces.extend(b.table_caption + [b.table_body] + b.table_footnote)
        elif b.type == "list":
            pieces.extend(b.list_items)
        else:
            pieces.append(b.text)
    text = "\n\n".join(p for p in pieces if p.strip())
    if len(text) < 30:
        raise ValueError("No usable body text/formulas on this physical PDF page")
    return text


def content_from_zip(raw, pages):
    """不解压附件路径，不读插图；只消费已验证大小与页码的结构化文本。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > 20_000 or sum(p.file_size for p in infos) > 500_000_000:
            raise ValueError("Oversized MinerU result archive")
        if any(PurePosixPath(p.filename).is_absolute() or ".." in PurePosixPath(p.filename).parts for p in infos):
            raise ValueError("Unsafe result archive member")
        targets = [p for p in infos if p.filename.endswith("_content_list.json")]
        if len(targets) != 1 or targets[0].file_size > 20_000_000:
            raise ValueError("Expected one bounded MinerU legacy paged content list")
        body = archive.read(targets[0])
        blocks = decode_blocks(body, pages)
        metadata = {"cloud_output_contract": "legacy_content_list_v1", "model_alias": "vlm"}
        layouts = [p for p in infos if PurePosixPath(p.filename).name == "layout.json"]
        if len(layouts) == 1 and layouts[0].file_size < 30_000_000:
            layout = json.loads(archive.read(layouts[0]))
            metadata["producer_version"] = layout.get("_version_name", "not_exposed")
            metadata["backend"] = layout.get("_backend", "not_exposed")
            metadata["effort"] = layout.get("_effort", "not_exposed")
        origins = [p for p in infos if p.filename.endswith("_origin.pdf")]
        if len(origins) == 1 and origins[0].file_size < 100_000_000:
            origin = archive.read(origins[0])
            returned_pages = len(PdfReader(io.BytesIO(origin)).pages)
            if returned_pages != pages:
                raise ValueError("Returned PDF page count differs from the uploaded full document")
            # 服务可能重存PDF；回传文件只能记为服务派生物，绝不替换原文件。
            metadata["returned_derivative_sha256"] = hashlib.sha256(origin).hexdigest()
            metadata["returned_derivative_pages"] = returned_pages
        return body, blocks, metadata


class MinerUPrecision:
    model_label = "MinerU-Precision:vlm"

    def __init__(self, directory, *, token=None, client=None, poll_interval=10, wait_seconds=1800):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.token = token or os.environ.get("MINERU_TOKEN")
        if not self.token:
            raise ValueError("MINERU_TOKEN is required for precision parsing")
        self.client = client
        self.poll_interval, self.wait_seconds = poll_interval, wait_seconds
        self.locks = {}
        self.cache = {}

    async def parse(self, pdf, page):
        raw = Path(pdf).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        async with self.locks.setdefault(sha, asyncio.Lock()):
            if sha not in self.cache:
                pages = len(PdfReader(io.BytesIO(raw)).pages)
                if len(raw) > 200_000_000 or not 1 <= pages <= 200:
                    raise ValueError("PDF exceeds the current Chinese API documented limit")
                if self.client is not None:
                    self.cache[sha] = await self._document(self.client, raw, sha, pages)
                else:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(90, write=300)) as client:
                        self.cache[sha] = await self._document(client, raw, sha, pages)
            blocks = self.cache[sha]
        return page_text(blocks, page), sha

    async def _document(self, client, raw, sha, pages):
        folder = self.directory / sha
        folder.mkdir(exist_ok=True)
        path, output = folder / "receipt.json", folder / "content.json"
        params = {"files": [{"name": "paper.pdf", "data_id": sha}], "model_version": "vlm",
                  "enable_formula": True, "enable_table": True, "language": "en"}
        identity = {"pdf_sha256": sha, "pages": pages, "params": params,
                    "endpoint": "https://mineru.net/api/v4/file-urls/batch", "exclude_figures": True}
        receipt = _read(path) if path.exists() else {"identity": identity, "provider": self.model_label,
            "submitted": False, "attempts": []}
        if receipt["identity"] != identity:
            raise OCRFailure("Precision OCR input version changed")
        if receipt.get("content_sha256"):
            if not output.exists() or hashlib.sha256(output.read_bytes()).hexdigest() != receipt["content_sha256"]:
                raise OCRFailure("Corrupt structured OCR cache")
            return decode_blocks(output.read_bytes(), pages)
        if receipt.get("submission_uncertain"):
            raise OCRFailure("Precision task submission is uncertain; recover before resubmitting")
        headers = {"Authorization": "Bearer " + self.token}
        if not receipt["submitted"]:
            receipt["submission_uncertain"] = True
            _save(path, receipt)
            response = await client.post(identity["endpoint"], json=params, headers=headers)
            receipt["attempts"].append({"stage": "submit", "http_status": response.status_code})
            _save(path, receipt)
            response.raise_for_status()
            result = response.json()
            receipt["submission_response"] = result
            if result.get("code") != 0:
                receipt["submission_uncertain"] = False
                _save(path, receipt)
                raise OCRFailure("Precision API rejected task creation; inspect private receipt")
            data = result["data"]
            if len(data["file_urls"]) != 1 or not isinstance(data["batch_id"], str):
                raise OCRFailure("Precision API batch mapping mismatch")
            public_url(data["file_urls"][0])
            receipt.update(batch_id=data["batch_id"], upload_url=data["file_urls"][0],
                           submitted=True, submission_uncertain=False)
            _save(path, receipt)
        if not receipt.get("uploaded"):
            # PUT同一对象可重试；不重新购买或另建解析任务。
            for retry in range(3):
                try:
                    response = await client.put(public_url(receipt["upload_url"]), content=raw)
                    receipt["attempts"].append({"stage": "upload", "http_status": response.status_code})
                    _save(path, receipt)
                    response.raise_for_status()
                    break
                except (httpx.TransportError, httpx.HTTPStatusError):
                    receipt["attempts"].append({"stage": "upload", "error": "transfer_failed"})
                    _save(path, receipt)
                    if retry == 2:
                        raise OCRFailure("Precision PDF upload failed; task retained") from None
                    await asyncio.sleep(5)
            receipt["uploaded"] = True
            _save(path, receipt)
        start = time.monotonic()
        while time.monotonic() - start < self.wait_seconds:
            response = await client.get("https://mineru.net/api/v4/extract-results/batch/" + receipt["batch_id"],
                                        headers=headers)
            response.raise_for_status()
            body = response.json()
            receipt["status_response"] = body
            _save(path, receipt)
            if body.get("code") != 0:
                raise OCRFailure("Precision API status query failed")
            results = body["data"]["extract_result"]
            if len(results) != 1 or results[0].get("data_id") != sha:
                raise OCRFailure("Precision API returned a different document")
            result = results[0]
            if result["state"] == "failed":
                raise OCRFailure("Precision API parsing failed")
            if result["state"] == "done":
                url = public_url(result["full_zip_url"])
                async with client.stream("GET", url) as download:
                    download.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in download.aiter_bytes():
                        size += len(chunk)
                        if size > 100_000_000:
                            raise OCRFailure("Precision result ZIP exceeds download limit")
                        chunks.append(chunk)
                zipped = b"".join(chunks)
                content, blocks, metadata = content_from_zip(zipped, pages)
                (folder / "result.zip").write_bytes(zipped)
                output.write_bytes(content)
                receipt.update(content_sha256=hashlib.sha256(content).hexdigest(),
                    zip_sha256=hashlib.sha256(zipped).hexdigest(), metadata=metadata,
                    completed_at_unix=time.time())
                _save(path, receipt)
                return blocks
            if result["state"] not in ("waiting-file", "pending", "running", "converting"):
                raise OCRFailure("Unknown precision task state")
            await asyncio.sleep(self.poll_interval)
        raise OCRFailure("Precision OCR polling window elapsed; resume original task")
