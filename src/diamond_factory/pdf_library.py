"""独立PDF材料暂存库；只采集正文与补充材料，不拼TeX、不自动批准出题。"""
import hashlib
import importlib.metadata
import ipaddress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import Field, model_validator
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .models import StrictModel, digest
from .runtime import atomic_json


class PdfInput(StrictModel):
    role: Literal["main", "supplement"]
    location: str = Field(min_length=5)
    version: str = Field(min_length=3)
    provenance_url: str = Field(min_length=8)


class PaperPdfPlan(StrictModel):
    doi: str = Field(pattern=r"^10\.\d{4,9}/\S+$")
    title: str = Field(min_length=10)
    journal: str = Field(min_length=3)
    domain: Literal["Math", "Physics"]
    metadata_url: str = Field(min_length=8)
    theory_screen: Literal["unreviewed", "theoretical", "experimental"] = "unreviewed"
    theory_screen_reason: str = ""
    supplement_status: Literal["not_checked", "embedded", "separate", "none_confirmed"] = "not_checked"
    supplement_evidence: str = ""
    documents: list[PdfInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def bundle_contract(self):
        if self.documents[0].role != "main" or sum(d.role == "main" for d in self.documents) != 1:
            raise ValueError("Exactly one main PDF must be first")
        if len({d.location for d in self.documents}) != len(self.documents):
            raise ValueError("Duplicate file locations do not add supplementary coverage")
        separate = any(d.role == "supplement" for d in self.documents)
        if separate != (self.supplement_status == "separate"):
            raise ValueError("Separate supplementary files require separate status and vice versa")
        if self.supplement_status != "not_checked" and len(self.supplement_evidence) < 10:
            raise ValueError("A supplement completeness claim requires locatable evidence")
        return self


class PdfArtifact(PdfInput):
    resolved_url: str | None
    pdf_path: str = Field(pattern=r"^\d{2}-(main|supplement)\.pdf$")
    pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pages: int = Field(ge=1)
    text_path: str = Field(pattern=r"^\d{2}-(main|supplement)\.txt$")
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_characters: list[int]
    empty_text_pages: list[int]


class PdfReceipt(StrictModel):
    plan_sha256: str
    paper: PaperPdfPlan
    documents: list[PdfArtifact]
    status: Literal["downloaded_pending_review"]
    identity_review: Literal["pending"]
    formula_review: Literal["pending"]
    ready_for_authoring: Literal[False]
    database_writes: Literal[0]
    transcription_engine: str
    retrieved_at: str
    note: str
    integrity_sha256: str = ""

    @model_validator(mode="after")
    def integrity(self):
        if self.plan_sha256 != digest(self.paper.model_dump()):
            raise ValueError("Paper plan hash mismatch")
        actual = digest(self.model_dump(exclude={"integrity_sha256"}))
        if self.integrity_sha256 and self.integrity_sha256 != actual:
            raise ValueError("Paper receipt integrity mismatch")
        self.integrity_sha256 = actual
        return self


class CollectedPaper(StrictModel):
    doi: str
    plan_sha256: str
    reused: bool
    documents: int = Field(ge=1)
    pages: int = Field(ge=1)
    status: Literal["downloaded_pending_review"]
    ready_for_authoring: Literal[False]


class CollectionFailure(StrictModel):
    doi: str
    error_type: str


class CollectionReport(StrictModel):
    planned: int = Field(ge=1)
    downloaded: int = Field(ge=0)
    failed: int = Field(ge=0)
    ready_for_authoring: Literal[0]
    model_calls: Literal[0]
    database_writes: Literal[0]
    papers: list[CollectedPaper]
    failures: list[CollectionFailure]

    @model_validator(mode="after")
    def counts(self):
        if self.planned != self.downloaded + self.failed or self.downloaded != len(self.papers) or self.failed != len(self.failures):
            raise ValueError("Collection totals do not match all planned attempts")
        return self


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("A public HTTPS URL without credentials is required")
    host = parts.hostname.lower()
    if host in ("localhost", "metadata.google.internal") or host.endswith((".local", ".internal")):
        raise ValueError("Private hosts are not paper repositories")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Private IP addresses are not paper repositories")
    return url


def _download(client, url, path, max_bytes=40_000_000, pause=None):
    """逐跳核对公开URL；不转发API凭据、Cookie或执行论文附件。"""
    public_url(url)
    for _ in range(6):
        if pause:
            pause(3.1)
        with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                url = public_url(urljoin(url, response.headers["location"]))
                continue
            response.raise_for_status()
            size = 0
            with path.open("wb") as stream:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("PDF exceeds the collection size limit")
                    stream.write(chunk)
            return url
    raise ValueError("Too many PDF redirects")


def _inspect_pdf(path):
    with path.open("rb") as stream:
        if not stream.read(1024).lstrip().startswith(b"%PDF-"):
            raise ValueError("Expected a PDF; HTML and source archives are not accepted")
    reader = PdfReader(path)
    if reader.is_encrypted or not reader.pages or len(reader.pages) > 2000:
        raise ValueError("Encrypted, empty or oversized PDFs require separate handling")
    pages = [(page.extract_text() or "") for page in reader.pages]
    return pages


def collect_paper(plan, directory, *, client, local_base=None, pause=None):
    """不可变计划/缓存收据；内容损坏时报错，不悄悄重下或覆盖旧材料。"""
    key = digest(plan.model_dump())
    directory = Path(directory) / key
    receipt = directory / "receipt.json"
    if receipt.exists():
        saved = PdfReceipt.model_validate_json(receipt.read_text()).model_dump()
        if saved["plan_sha256"] != key or saved["paper"] != plan.model_dump():
            raise ValueError("Paper plan receipt mismatch")
        for document in saved["documents"]:
            for path_key, hash_key in (("pdf_path", "pdf_sha256"), ("text_path", "text_sha256")):
                path = directory / document[path_key]
                if hashlib.sha256(path.read_bytes()).hexdigest() != document[hash_key]:
                    raise ValueError("Corrupt paper cache; use a new collection version")
        return saved, True
    directory.mkdir(parents=True, exist_ok=True)
    documents = []
    for i, spec in enumerate(plan.documents):
        name = f"{i:02d}-{spec.role}"
        path = directory / (name + ".pdf")
        checkpoint = directory / (name + ".json")
        if checkpoint.exists():
            artifact = PdfArtifact.model_validate_json(checkpoint.read_text()).model_dump()
            if any(artifact[k] != v for k, v in spec.model_dump().items()):
                raise ValueError("Document checkpoint does not match the frozen plan")
            for path_key, hash_key in (("pdf_path", "pdf_sha256"), ("text_path", "text_sha256")):
                if hashlib.sha256((directory/artifact[path_key]).read_bytes()).hexdigest() != artifact[hash_key]:
                    raise ValueError("Corrupt document checkpoint; use a new collection version")
            if any(d["pdf_sha256"] == artifact["pdf_sha256"] for d in documents):
                raise ValueError("The same PDF cannot count as main and supplementary material")
            documents.append(artifact)
            continue
        temporary = directory / (name + ".part")
        url = None
        try:
            if spec.location.startswith("https://"):
                url = _download(client, spec.location, temporary, pause=pause)
            else:
                location = Path(spec.location)
                if not location.is_absolute():
                    if local_base is None:
                        raise ValueError("A base directory is required for a relative local PDF")
                    location = Path(local_base) / location
                if location.suffix.lower() != ".pdf":
                    raise ValueError("Only explicit PDF files enter a paper bundle")
                if location.stat().st_size > 40_000_000:
                    raise ValueError("PDF exceeds the collection size limit")
                temporary.write_bytes(location.read_bytes())
            pages = _inspect_pdf(temporary)
            raw_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
            if any(d["pdf_sha256"] == raw_hash for d in documents):
                raise ValueError("The same PDF cannot count as main and supplementary material")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        text = "\n\n".join(f"[document {i}; {spec.role}; page {n}]\n{p}" for n, p in enumerate(pages, 1))
        transcription = directory / (name + ".txt")
        transcription.write_text(text)
        documents.append({**spec.model_dump(), "resolved_url": url, "pdf_path": path.name,
            "pdf_sha256": raw_hash, "pages": len(pages), "text_path": transcription.name,
            "text_sha256": hashlib.sha256(transcription.read_bytes()).hexdigest(),
            "page_characters": [len(p.strip()) for p in pages],
            "empty_text_pages": [n for n, p in enumerate(pages, 1) if not p.strip()]})
        atomic_json(checkpoint, PdfArtifact.model_validate(documents[-1]).model_dump())
    result = PdfReceipt.model_validate({"plan_sha256": key, "paper": plan.model_dump(), "documents": documents,
        "status": "downloaded_pending_review", "identity_review": "pending", "formula_review": "pending",
        "ready_for_authoring": False, "database_writes": 0,
        "transcription_engine": "pypdf/" + importlib.metadata.version("pypdf"),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "note": "PDF原文件分别保存；普通抽字仅供定位，不代表公式完整或补充材料齐全；未自动导入出题源。"}).model_dump()
    atomic_json(receipt, result)
    return result, False


def collect_library(plans, directory, *, client=None, local_base=None, pause=None):
    """逐篇隔离失败；全部计划保留在分母，只有PDF采集统计，不调用模型。"""
    if not plans or len({p.doi.lower() for p in plans}) != len(plans):
        raise ValueError("Require nonempty, DOI-deduplicated paper plans")
    own = client is None
    client = client or httpx.Client(timeout=120, headers={"User-Agent": "rst-hle/0.1 paper-library"})
    rows, failures = [], []
    try:
        for plan in plans:
            try:
                record, reused = collect_paper(plan, directory, client=client, local_base=local_base, pause=pause)
                rows.append({"doi": plan.doi, "plan_sha256": record["plan_sha256"], "reused": reused,
                    "documents": len(record["documents"]), "pages": sum(d["pages"] for d in record["documents"]),
                    "status": record["status"], "ready_for_authoring": False})
            except (httpx.HTTPError, PyPdfError, ValueError, OSError) as error:
                # 不把可能含token的重定向URL或私有本地路径打印到共享日志。
                failures.append({"doi": plan.doi, "error_type": type(error).__name__})
    finally:
        if own:
            client.close()
    result = CollectionReport.model_validate({"planned": len(plans), "downloaded": len(rows), "failed": len(failures),
        "ready_for_authoring": 0, "model_calls": 0, "database_writes": 0,
        "papers": rows, "failures": failures}).model_dump()
    atomic_json(Path(directory) / "collection-report.json", result)
    return result
