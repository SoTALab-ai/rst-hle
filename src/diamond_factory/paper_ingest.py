"""预先读取整篇PDF并存入独立论文库；程序检查不等于科学正确性认证。"""
import asyncio
import hashlib
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
from typing import Literal

import httpx
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .costs import MeteredGateway, measure_cost
from .llm import CacheIntegrityError
from .models import StrictModel, digest
from .pdf_library import PdfReceipt
from .pdf_review import render_page
from .runtime import atomic_json, gather_cancel_on_error


class LibraryDocument(StrictModel):
    path: str
    role: Literal["main", "supplement"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pages: int = Field(ge=1, le=2000)


class LibraryPaper(StrictModel):
    title: str = Field(min_length=1)
    doi: str | None = None
    journal: str
    domain: Literal["Math", "Physics", "unknown"]
    provenance: str
    supplement_status: str
    documents: list[LibraryDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def main_first(self):
        if self.documents[0].role != "main" or sum(d.role == "main" for d in self.documents) != 1:
            raise ValueError("Exactly one main PDF must be first")
        if len({d.sha256 for d in self.documents}) != len(self.documents):
            raise ValueError("Duplicate PDF is not supplementary material")
        return self

    def identity(self):
        # 本地路径变化不创建新的论文版本；PDF内容、元数据及补充状态变化则创建。
        body = self.model_dump()
        for doc in body["documents"]:
            del doc["path"]
        return digest(body)


class LibraryPlan(StrictModel):
    expected_papers: int = Field(ge=1)
    papers: list[LibraryPaper] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_papers(self):
        if len(self.papers) > self.expected_papers:
            raise ValueError("Inventory exceeds expected paper count")
        ids = [p.identity() for p in self.papers]
        mains = [p.documents[0].sha256 for p in self.papers]
        if len(set(ids)) != len(ids) or len(set(mains)) != len(mains):
            raise ValueError("Duplicate main PDFs do not count as independent papers")
        return self


class ReadPage(StrictModel):
    page: int = Field(ge=1)
    status: Literal["readable", "empty", "unreadable"]
    text: str
    formula_labels: list[str]
    unreadable: list[str]

    @model_validator(mode="after")
    def explicit_uncertainty(self):
        if (self.status == "unreadable") != bool(self.unreadable):
            raise ValueError("Unclear text/formulas require explicit unreadable status and locations")
        if self.status == "readable" and not self.text.strip():
            raise ValueError("Readable page cannot be empty")
        if self.status == "empty" and (self.text.strip() or self.formula_labels):
            raise ValueError("Empty page must not contain body or formulas")
        return self


class ReadBatch(StrictModel):
    pages: list[ReadPage] = Field(min_length=1, max_length=2)


def validate_pages(batch, expected):
    if [p.page for p in batch.pages] != expected:
        raise ValueError("Transcription must cover each physical page exactly once in order")


def _document(path, role):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if not raw[:1024].lstrip().startswith(b"%PDF-"):
        raise ValueError("Input is not a PDF")
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF requires separate handling")
    return LibraryDocument(path=str(path), role=role, sha256=hashlib.sha256(raw).hexdigest(),
                           pages=len(reader.pages))


def inventory_receipts(root, expected):
    """已有采集收据保留DOI、刊物和补充信息；不重复下载论文。"""
    papers = []
    for path in sorted(Path(root).glob("*/receipt.json")):
        receipt = PdfReceipt.model_validate_json(path.read_text())
        docs = []
        for item in receipt.documents:
            doc = _document(path.parent / item.pdf_path, item.role)
            if doc.sha256 != item.pdf_sha256 or doc.pages != item.pages:
                raise ValueError("PDF differs from its collection receipt")
            docs.append(doc)
        p = receipt.paper
        papers.append(LibraryPaper(title=p.title, doi=p.doi, journal=p.journal, domain=p.domain,
            provenance=p.metadata_url, supplement_status=p.supplement_status, documents=docs))
    return LibraryPlan(expected_papers=expected, papers=papers)


def inventory_directory(root, expected):
    """无元数据的PDF仅登记文件身份；不猜测刊物、领域或补充完整性。"""
    papers, seen = [], set()
    for path in sorted(Path(root).rglob("*.pdf")):
        doc = _document(path, "main")
        if doc.sha256 in seen:
            continue
        seen.add(doc.sha256)
        papers.append(LibraryPaper(title=path.stem, journal="unknown", domain="unknown",
            provenance="local-file", supplement_status="not_checked", documents=[doc]))
    return LibraryPlan(expected_papers=expected, papers=papers)


class LibraryRepository:
    """状态/索引写PG，原PDF和转录写内容寻址库；不向出题来源表自动放行。"""

    def __init__(self, store):
        self.store, self.db, self.objects = store, store.db, store.objects
        if not self.db.execute("SELECT to_regclass('diamond.library_pages')").fetchone()[0]:
            raise ValueError("PDF library migration required; run diamond db-init explicitly")

    def register(self, run_id, paper, profile):
        pid, profile_sha = paper.identity(), digest(profile)
        docs = []
        # 先验证所有输入，再写短事务；损坏输入不会生成一半的论文索引。
        for doc in paper.documents:
            current = _document(doc.path, doc.role)
            if current != doc:
                raise ValueError("PDF changed after inventory; create a new frozen plan")
            key, sha, size = self.objects.put_bytes(Path(doc.path).read_bytes())
            docs.append((doc, key, sha, size))
        metadata = paper.model_dump()
        for doc in metadata["documents"]:
            del doc["path"]
        with self.db.transaction():
            self.db.execute("""INSERT INTO diamond.library_papers(id,title,doi,journal,domain,metadata)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (pid, paper.title, paper.doi, paper.journal, paper.domain, Jsonb(metadata)))
            for i, (doc, key, sha, size) in enumerate(docs):
                parse_id = digest({"pdf": sha, "profile": profile_sha})
                self.db.execute("""INSERT INTO diamond.library_documents
                    (id,pages,object_key,object_sha256,object_size) VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING""", (sha, doc.pages, key, sha, size))
                self.db.execute("INSERT INTO diamond.library_paper_documents VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING", (pid, i, doc.role, sha))
                self.db.execute("INSERT INTO diamond.library_parses(id,document_id,profile_sha256,profile) "
                    "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (parse_id, sha, profile_sha, Jsonb(profile)))
                self.db.execute("INSERT INTO diamond.library_run_documents VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING", (run_id, pid, i, parse_id))
        return pid

    def documents(self, run_id):
        return self.db.execute("""SELECT DISTINCT p.id,d.id,d.pages,d.object_key,d.object_sha256,d.object_size
            FROM diamond.library_run_documents r JOIN diamond.library_parses p ON p.id=r.parse_id
            JOIN diamond.library_documents d ON d.id=p.document_id WHERE r.run_id=%s ORDER BY p.id""",
            (run_id,)).fetchall()

    def save_batch(self, parse_id, batch, request_id, actual_model):
        pages = self.db.execute("SELECT d.pages FROM diamond.library_parses p JOIN diamond.library_documents d "
            "ON d.id=p.document_id WHERE p.id=%s", (parse_id,)).fetchone()[0]
        trace = self.store.get_request(request_id)
        if (not trace or trace.get("response_error") or trace.get("actual_model") != actual_model
                or ReadBatch.model_validate_json(trace["content"]) != batch):
            raise ValueError("Library transcript differs from the recorded model response")
        with self.db.transaction():
            for page in batch.pages:
                if page.page > pages:
                    raise ValueError("Page exceeds frozen PDF")
                key, sha, size = self.objects.put(page.model_dump())
                prior = self.db.execute("SELECT object_sha256,request_id FROM diamond.library_pages "
                    "WHERE parse_id=%s AND page=%s", (parse_id, page.page)).fetchone()
                if prior and prior != (sha, request_id):
                    raise ValueError("Immutable transcription changed; use a new reader profile")
                self.db.execute("""INSERT INTO diamond.library_pages
                    (parse_id,page,status,request_id,actual_model,object_key,object_sha256,object_size)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (parse_id, page.page, page.status, request_id, actual_model, key, sha, size))

    def pages(self, parse_id):
        rows = self.db.execute("SELECT page,object_key,object_sha256,object_size FROM diamond.library_pages "
                               "WHERE parse_id=%s ORDER BY page", (parse_id,)).fetchall()
        result = [ReadPage.model_validate(self.objects.get(*r[1:])) for r in rows]
        if [p.page for p in result] != [r[0] for r in rows]:
            raise ValueError("Library page index/content mismatch")
        return result

    def paper_text(self, run_id, paper_id):
        """后续材料定位可直接调用；未读清或缺页的材料拒绝无提示导出。"""
        rows = self.db.execute("""SELECT r.ordinal,r.parse_id,d.pages FROM diamond.library_run_documents r
            JOIN diamond.library_parses p ON p.id=r.parse_id JOIN diamond.library_documents d ON d.id=p.document_id
            WHERE r.run_id=%s AND r.paper_id=%s ORDER BY r.ordinal""", (run_id, paper_id)).fetchall()
        if not rows:
            raise ValueError("Unknown frozen paper version")
        blocks = []
        for ordinal, parse_id, count in rows:
            pages = self.pages(parse_id)
            if [p.page for p in pages] != list(range(1, count+1)) or any(p.unreadable for p in pages):
                raise ValueError("Incomplete or unreadable PDF; inspect locations before authoring")
            blocks.extend(f"[document {ordinal}; physical page {p.page}; parse {parse_id}]\n{p.text}" for p in pages)
        return "\n\n".join(blocks)


def native_hints(pdf, numbers):
    """同一PDF的文本层只作符号消歧提示，原页图像仍是版面依据。"""
    reader = PdfReader(pdf)
    hints = []
    for number in numbers:
        try:
            text = reader.pages[number-1].extract_text() or ""
        except (PyPdfError, UnicodeError, ValueError, KeyError):
            # 扫描页/损坏文本层不阻止图像读取，也不伪装成可靠空页。
            text = ""
        hints.append({"physical_page": number, "native_text_hint": text[:20000],
                      "hint_truncated": len(text) > 20000})
    return hints


def reader_profile(settings, prompt, renderer, pages_per_call):
    # 渲染器和本模块变动都使旧转录失效；不热改正在执行的快照。
    version = subprocess.run([renderer, "-v"], capture_output=True, check=True, timeout=10)
    return {"reader": settings.generator.model_dump(),
        "endpoint_sha256": digest(os.environ.get(settings.generator.base_url_env, "")), "prompt_sha256": digest(prompt),
        "renderer_version": (version.stdout + version.stderr).decode(errors="replace"),
        "pages_per_call": pages_per_call, "scale_to": 2200,
        "native_hint_engine": "pypdf/" + importlib.metadata.version("pypdf"),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "render_sha256": hashlib.sha256(Path(__file__).with_name("pdf_review.py").read_bytes()).hexdigest(),
        "gateway_sha256": hashlib.sha256(Path(__file__).with_name("llm.py").read_bytes()).hexdigest(),
        "schema": ReadBatch.model_json_schema()}


async def ingest_library(store, plan, settings, pricing, directory, run_id, *, pages_per_call=1,
                         prompt_path="prompts/read_pdf.txt", renderer="pdftoppm"):
    if pages_per_call not in (1, 2):
        raise ValueError("Read one or two physical PDF pages per request")
    directory = Path(directory)
    prompt = Path(prompt_path).read_text()
    profile = reader_profile(settings, prompt, renderer, pages_per_call)
    manifest = {"run_id": run_id, "mode": "pdf_library", "plan": plan.model_dump(),
        "settings": settings.model_dump(), "pricing": pricing, "profile": profile,
        "code_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("src/diamond_factory").rglob("*"))
            if p.is_file() and p.suffix in (".py", ".sql")}}
    fingerprint = digest(manifest)
    old = store.runs().get(run_id)
    if old and old["fingerprint"] != fingerprint:
        raise ValueError("Frozen library run changed; use a new run-id")
    freeze = directory / "manifest.json"
    if freeze.exists() and json.loads(freeze.read_text()) != manifest:
        raise ValueError("Frozen library manifest changed; use a new run-id and directory")
    atomic_json(freeze, manifest)
    store.run(run_id, {"mode": "pdf_library", "status": "running", "fingerprint": fingerprint,
                       "manifest": manifest})
    repo = LibraryRepository(store)
    # 全部文件核对/入库后才开始购买读取，任何源文件变更在此显式失败。
    for paper in plan.papers:
        repo.register(run_id, paper, profile)
    gateway = MeteredGateway(settings, directory / "requests", store, run_id,
                             pricing=pricing, visible_cost_limit_usd=None)
    documents = repo.documents(run_id)
    sem = asyncio.Semaphore(settings.concurrency)
    errors = []

    async def one(document, numbers):
        parse_id, sha, total, key, obj_sha, size = document
        async with sem:
            try:
                raw = repo.objects.get_bytes(key, obj_sha, size)
                local = directory / "pdf" / (sha + ".pdf")
                if local.exists() and local.read_bytes() != raw:
                    raise CacheIntegrityError("Corrupt working PDF; refuse recovery")
                if not local.exists():
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(raw)
                images = []
                for n in numbers:
                    prefix = directory / "rendered" / sha / str(n)
                    seal_path = Path(str(prefix) + ".sha256")
                    if Path(str(prefix) + ".png").exists() and not seal_path.exists():
                        raise CacheIntegrityError("Unsealed rendered-page checkpoint")
                    image = await asyncio.to_thread(render_page, local, n, prefix, executable=renderer)
                    seal = hashlib.sha256(image).hexdigest()
                    if seal_path.exists() and seal_path.read_text() != seal:
                        raise CacheIntegrityError("Corrupt rendered-page checkpoint")
                    seal_path.write_text(seal)
                    images.append(image)
                hints = await asyncio.to_thread(native_hints, local, numbers)
                content = prompt + "\nInput map (images in this order): " + json.dumps(
                    {"pdf_sha256": sha, "physical_pages": numbers, "total_pages": total,
                     "native_hints": hints}, ensure_ascii=False)
                try:
                    batch, rid, model = await gateway.call("generator", content,
                        f"library:{parse_id}:pages:{','.join(map(str, numbers))}", ReadBatch, images=images)
                except ValueError as error:
                    raise CacheIntegrityError("Unreadable request/object cache; refusing recovery") from error
                validate_pages(batch, numbers)
                repo.save_batch(parse_id, batch, rid, model)
                # PG与对象库已保存付费响应/尝试，删除可再生镜像以控制大批次磁盘占用。
                if not store.get_request(rid):
                    raise CacheIntegrityError("Response not durable in PostgreSQL/object store")
                (gateway.directory / (rid + ".json")).unlink(missing_ok=True)
                for n in numbers:
                    Path(str(directory / "rendered" / sha / str(n)) + ".png").unlink(missing_ok=True)
            except CacheIntegrityError:
                # 损坏缓存拒绝全批续跑；不能当普通页面失败后悄悄买一份替代结果。
                raise
            except (ValueError, RuntimeError, OSError, httpx.HTTPError, subprocess.SubprocessError) as error:
                row = {"parse_id": parse_id, "pages": numbers, "error_type": type(error).__name__}
                errors.append(row)
                store.event(run_id, {"event": "library_page_failure", **row})
            print(json.dumps({"event": "page_batch_finished", "pdf": sha, "pages": numbers,
                              "failures_so_far": len(errors)}), flush=True)

    try:
        await gather_cancel_on_error(*(one(doc, list(range(start, min(start+pages_per_call, doc[2]+1))))
            for doc in documents for start in range(1, doc[2]+1, pages_per_call)))
    except BaseException:
        store.run(run_id, {"mode": "pdf_library", "status": "interrupted", "fingerprint": fingerprint,
                           "manifest": manifest})
        raise
    rows = []
    for doc in documents:
        pages = repo.pages(doc[0])
        rows.append({"parse_id": doc[0], "pdf_sha256": doc[1], "expected_pages": doc[2],
            "stored_pages": len(pages), "readable_pages": sum(p.status == "readable" for p in pages),
            "empty_pages": sum(p.status == "empty" for p in pages),
            "unreadable_pages": [p.page for p in pages if p.status == "unreadable"],
            "missing_pages": sorted(set(range(1, doc[2]+1)) - {p.page for p in pages})})
    report = {"run_id": run_id, "expected_papers": plan.expected_papers, "registered_papers": len(plan.papers),
        "missing_input_papers": plan.expected_papers-len(plan.papers), "unique_pdfs": len(rows),
        "expected_pages": sum(r["expected_pages"] for r in rows),
        "stored_pages": sum(r["stored_pages"] for r in rows), "documents": rows, "failures": errors,
        "cost": measure_cost(store, run_id, directory / "requests", settings, pricing),
        "formula_accuracy_certified": False, "scientific_review_complete": False}
    complete = not report["missing_input_papers"] and not errors and all(
        not r["missing_pages"] and not r["unreadable_pages"] for r in rows)
    report["status"] = "transcribed" if complete else "incomplete"
    atomic_json(directory / "report.json", report)
    store.run(run_id, {"mode": "pdf_library", "status": report["status"], "fingerprint": fingerprint,
                       "manifest": manifest, "report": report})
    return report
