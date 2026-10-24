"""MinerU免登录PDF解析适配器；原文件+页码寻址，任务续查，不把OCR成功当作公式正确。"""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field

from .models import StrictModel, digest
from .pdf_library import public_url
from .runtime import atomic_json

API = "https://mineru.net/api/v1/agent/parse"


class OCRFailure(RuntimeError):
    pass


class TaskStatus(StrictModel):
    task_id: str = Field(min_length=1, max_length=100)
    state: Literal["waiting-file", "uploading", "pending", "running", "done", "failed"]
    markdown_url: str | None = None
    err_msg: str | None = None


def _save(path, data):
    data["integrity_sha256"] = digest({k: v for k, v in data.items() if k != "integrity_sha256"})
    atomic_json(path, data)


def _read(path):
    data = json.loads(path.read_text())
    if data.get("integrity_sha256") != digest({k: v for k, v in data.items() if k != "integrity_sha256"}):
        raise OCRFailure("Corrupt OCR task receipt; do not resubmit")
    return data


class MinerUAgent:
    def __init__(self, directory, *, client=None, submission_interval=3.1, poll_interval=10, wait_seconds=600):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.submit_lock = asyncio.Lock()
        self.last_submit = 0.0
        self.submission_interval = submission_interval
        self.poll_interval = poll_interval
        self.wait_seconds = wait_seconds
        self.locks = {}

    async def parse(self, pdf, page):
        """直接提交固定PDF，逐物理页解析，避免Markdown无页码时猜测跨页映射。"""
        raw = Path(pdf).read_bytes()
        if not raw.startswith(b"%PDF-") or len(raw) > 10_000_000 or type(page) is not int or page < 1:
            raise ValueError("Agent API requires a PDF below 10MB and a positive physical page")
        params = {"file_name": "paper.pdf", "language": "en", "page_range": f"{page}-{page}",
                  "enable_table": True, "is_ocr": False, "enable_formula": True}
        identity = {"pdf_sha256": hashlib.sha256(raw).hexdigest(), "params": params, "endpoint": API}
        key = digest(identity)
        async with self.locks.setdefault(key, asyncio.Lock()):
            if self.client is not None:
                return await self._parse(self.client, raw, identity, key)
            async with httpx.AsyncClient(timeout=90) as client:
                return await self._parse(client, raw, identity, key)

    async def _parse(self, client, raw, identity, key):
        folder = self.directory / key
        folder.mkdir(exist_ok=True)
        path, output = folder / "receipt.json", folder / "page.md"
        receipt = _read(path) if path.exists() else {"identity": identity, "provider": "MinerU Agent API",
            "provider_model_version": "not_exposed", "attempts": [], "submitted": False}
        if receipt["identity"] != identity:
            raise OCRFailure("OCR identity mismatch")
        if receipt.get("markdown_sha256"):
            if not output.exists() or hashlib.sha256(output.read_bytes()).hexdigest() != receipt["markdown_sha256"]:
                raise OCRFailure("Corrupt OCR Markdown; do not resubmit")
            return output.read_text(), key
        if receipt.get("submission_uncertain"):
            raise OCRFailure("Submission outcome unknown; recover the original task before resubmitting")
        if not receipt["submitted"]:
            async with self.submit_lock:
                await asyncio.sleep(max(0, self.submission_interval - (time.monotonic() - self.last_submit)))
                for retry in range(4):
                    receipt["submission_uncertain"] = True
                    _save(path, receipt)
                    response = await client.post(API + "/file", json=identity["params"])
                    self.last_submit = time.monotonic()
                    receipt["attempts"].append({"stage": "submit", "http_status": response.status_code})
                    if response.status_code != 429:
                        break
                    # 已明确拒绝受理；保留收据并限次退避，绝不换IP绕过限制。
                    receipt["submission_uncertain"] = False
                    _save(path, receipt)
                    if retry == 3:
                        raise OCRFailure("MinerU IP rate limited submission (429); retry later")
                    delay = response.headers.get("retry-after", "20")
                    await asyncio.sleep(min(60, max(1, float(delay) if delay.isdigit() else 20)))
                response.raise_for_status()
                result = response.json()
                receipt["submission_response"] = result
                if result.get("code") != 0:
                    receipt["submission_uncertain"] = False
                    _save(path, receipt)
                    raise OCRFailure("MinerU rejected task creation")
                data = result["data"]
                TaskStatus(task_id=data["task_id"], state="waiting-file")
                public_url(data["file_url"])
                receipt.update(task_id=data["task_id"], upload_url=data["file_url"],
                               submitted=True, submission_uncertain=False)
                _save(path, receipt)
        if not receipt.get("uploaded"):
            response = await client.put(public_url(receipt["upload_url"]), content=raw)
            receipt["attempts"].append({"stage": "upload", "http_status": response.status_code})
            _save(path, receipt)
            response.raise_for_status()
            receipt["uploaded"] = True
            _save(path, receipt)
        started = time.monotonic()
        while time.monotonic() - started < self.wait_seconds:
            response = await client.get(API + "/" + receipt["task_id"])
            receipt["attempts"].append({"stage": "poll", "http_status": response.status_code})
            _save(path, receipt)
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise OCRFailure("MinerU status query failed; retain original task")
            data = body["data"]
            status = TaskStatus.model_validate({k: data[k] for k in
                ("task_id", "state", "markdown_url", "err_msg") if k in data})
            if status.task_id != receipt["task_id"]:
                raise OCRFailure("MinerU returned a different task identity")
            receipt["status_response"] = body
            _save(path, receipt)
            if status.state == "failed":
                raise OCRFailure("MinerU parsing failed; see private provider receipt")
            if status.state == "done":
                if not status.markdown_url:
                    raise OCRFailure("Completed OCR task has no Markdown artifact")
                url = public_url(status.markdown_url)
                # 不携带模型API凭据；下载重定向逐跳检查，限制产物体积。
                for _ in range(6):
                    response = await client.get(url, follow_redirects=False)
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        url = public_url(urljoin(url, response.headers["location"]))
                        continue
                    break
                response.raise_for_status()
                result = response.content
                if not result or len(result) > 5_000_000:
                    raise OCRFailure("Empty or oversized OCR Markdown")
                result.decode("utf-8")
                output.write_bytes(result)
                receipt["markdown_sha256"] = hashlib.sha256(result).hexdigest()
                receipt["completed_at_unix"] = time.time()
                _save(path, receipt)
                return result.decode("utf-8"), key
            await asyncio.sleep(self.poll_interval)
        raise OCRFailure("OCR polling window elapsed; resume the same task, not a new submission")
