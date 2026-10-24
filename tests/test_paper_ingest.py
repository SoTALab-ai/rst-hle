import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.errors import PdfReadError
from test_gateway import install_mock

from diamond_factory.artifacts import LocalObjects
from diamond_factory.llm import CacheIntegrityError
from diamond_factory.paper_ingest import (
    LibraryPlan,
    LibraryRepository,
    ReadBatch,
    ReadPage,
    ingest_library,
    inventory_directory,
    native_hints,
    validate_pages,
)
from diamond_factory.postgres import PostgresStore


@pytest.fixture
def pg(tmp_path):
    url = os.environ.get("DIAMOND_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Dedicated real PostgreSQL test database not configured")
    assert conninfo_to_dict(url)["dbname"].endswith("_test")
    store = PostgresStore(url, tmp_path/"objects", check=False)
    store.migrate()
    store.db.execute("TRUNCATE diamond.runs, diamond.sources, diamond.jobs, diamond.quotas, "
                     "diamond.library_papers, diamond.library_documents, diamond.library_metadata CASCADE")
    yield store
    store.db.close()


def make_pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.add_blank_page(100, 100)
    writer.write(path)


def readable(n=1):
    return ReadPage(page=n, status="readable", text=r"Equation (1): $u_\ell=\nu+1$.",
                    formula_labels=["1"], unreadable=[])


def test_binary_cas_integrity_and_path_boundary(tmp_path):
    objects = LocalObjects(tmp_path)
    content = b"%PDF-1.7\noriginal bytes"
    ref = objects.put_bytes(content)
    assert objects.put_bytes(content) == ref
    assert objects.get_bytes(*ref) == content
    with pytest.raises(ValueError, match="escapes"):
        objects.get_bytes("../outside", ref[1], ref[2])
    (tmp_path/ref[0]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        objects.put_bytes(content)
    with pytest.raises(ValueError, match="corrupt"):
        objects.get_bytes(*ref)


def test_compressed_json_cas_roundtrip(tmp_path):
    obj = LocalObjects(tmp_path, compress_json=True)
    value = {"request": "long repetitive image data" * 1000}
    ref = obj.put(value)
    assert ref[0].endswith(".json.gz") and ref[2] < 500
    assert LocalObjects(tmp_path).get(*ref) == value


def test_inventory_deduplicates_files_and_preserves_missing_count(tmp_path):
    make_pdf(tmp_path/"a.pdf")
    (tmp_path/"copy.pdf").write_bytes((tmp_path/"a.pdf").read_bytes())
    plan = inventory_directory(tmp_path, 200)
    assert len(plan.papers) == 1 and plan.expected_papers == 200
    assert plan.papers[0].journal == "unknown" and plan.papers[0].supplement_status == "not_checked"
    with pytest.raises(ValidationError, match="Duplicate"):
        LibraryPlan(expected_papers=200, papers=plan.papers*2)


def test_native_hint_is_same_physical_page_and_blank_layer_is_not_transcript(tmp_path):
    make_pdf(tmp_path/"paper.pdf")
    assert native_hints(tmp_path/"paper.pdf", [2, 1]) == [
        {"physical_page": 2, "native_text_hint": "", "hint_truncated": False},
        {"physical_page": 1, "native_text_hint": "", "hint_truncated": False}]


def test_page_contract_rejects_missing_duplicate_reordered_and_hidden_uncertainty():
    for numbers in ([1], [1, 1], [2, 1], [2, 3]):
        with pytest.raises(ValueError, match="physical page"):
            validate_pages(ReadBatch(pages=[readable(n) for n in numbers]), [1, 2])
    with pytest.raises(ValidationError):
        ReadPage(page=1, status="readable", text="text", formula_labels=[], unreadable=["Eq.1 unclear"])
    with pytest.raises(ValidationError):
        ReadPage(page=1, status="empty", text="has content", formula_labels=[], unreadable=[])
    assert ReadPage(page=1, status="unreadable", text="partial", formula_labels=[],
                    unreadable=["bottom line cut off"]).unreadable


def rates():
    return {"currency": "USD", "observed_at": "2026-10-25", "rmb_reference_exchange_rate": 6.7,
            "rates_per_million": {m: {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0}
                                  for m in ("gpt-6-luna", "gpt-5.6-luna", "glm-5.2")}}


def prepare(tmp_path, monkeypatch, settings, handler):
    make_pdf(tmp_path/"paper.pdf")
    plan = inventory_directory(tmp_path, 1)
    settings = settings.model_copy(update={"generator": settings.generator.model_copy(update={
        "model": "gpt-6-luna", "accepted_response_models": ["gpt-6-luna"]}), "request_cap_enabled": False})
    install_mock(monkeypatch, handler)
    monkeypatch.setattr("diamond_factory.paper_ingest.reader_profile", lambda *a: {"test": "reader-v1", "pages_per_call": a[3]})
    def render(*args, **kwargs):
        Path(args[2]).parent.mkdir(parents=True, exist_ok=True)
        return b"\x89PNG\r\n\x1a\n"+str(args[1]).encode()

    monkeypatch.setattr("diamond_factory.paper_ingest.render_page", render)
    return plan, settings


def response(n, *, unreadable=False):
    page = readable(n)
    if unreadable:
        page = ReadPage(page=n, status="unreadable", text="partial", formula_labels=[], unreadable=["Eq.1"])
    return httpx.Response(200, json={"model": "gpt-6-luna", "choices": [{"message": {
        "content": ReadBatch(pages=[page]).model_dump_json()}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20}})


def test_pg_library_resume_export_input_change_and_corruption(pg, tmp_path, settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        text = json.loads(request.content)["messages"][1]["content"][0]["text"]
        page = json.JSONDecoder().raw_decode(text.split("Input map (images in this order): ")[1])[0]["physical_pages"][0]
        return response(page)

    plan, settings = prepare(tmp_path, monkeypatch, settings, handler)
    directory = tmp_path/"run"
    args = (pg, plan, settings, rates(), directory, "library")
    report = asyncio.run(ingest_library(*args))
    assert report["status"] == "transcribed" and report["stored_pages"] == 2 and len(calls) == 2
    repo = LibraryRepository(pg)
    pid = plan.papers[0].identity()
    assert "physical page 2" in repo.paper_text("library", pid)
    assert pg.sources() == []  # 转录不是科学审批，不自动创建出题来源。
    assert asyncio.run(ingest_library(*args))["cost"]["http_attempts"] == 2 and len(calls) == 2
    # 同一转录在新run复用，费用仍关联首次购买的run，不增加实际调用。
    other = asyncio.run(ingest_library(pg, plan, settings, rates(), tmp_path/"other", "other"))
    assert other["stored_pages"] == 2 and len(calls) == 2
    with pytest.raises(ValueError, match="Frozen"):
        asyncio.run(ingest_library(pg, plan, settings, rates(), directory, "library", pages_per_call=2))
    obj = pg.db.execute("SELECT object_key FROM diamond.model_requests ORDER BY id LIMIT 1").fetchone()[0]
    (pg.objects.root/obj).write_text("corrupt")
    with pytest.raises(CacheIntegrityError, match="cache"):
        asyncio.run(ingest_library(*args))
    assert len(calls) == 2
    Path(plan.papers[0].documents[0].path).write_bytes(b"%PDF-corrupted")
    with pytest.raises((ValueError, PdfReadError)):
        asyncio.run(ingest_library(pg, plan, settings, rates(), tmp_path/"changed", "changed"))
    assert len(calls) == 2


def test_pg_library_unreadable_and_wrong_page_never_ready(pg, tmp_path, settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        text = json.loads(request.content)["messages"][1]["content"][0]["text"]
        page = json.JSONDecoder().raw_decode(text.split("Input map (images in this order): ")[1])[0]["physical_pages"][0]
        return response(1, unreadable=True) if page == 1 else response(999)

    plan, settings = prepare(tmp_path, monkeypatch, settings, handler)
    result = asyncio.run(ingest_library(pg, plan, settings, rates(), tmp_path/"run", "library"))
    assert result["status"] == "incomplete" and result["stored_pages"] == 1
    assert result["documents"][0]["unreadable_pages"] == [1]
    assert result["documents"][0]["missing_pages"] == [2] and len(result["failures"]) == 1
    with pytest.raises(ValueError, match="Incomplete"):
        LibraryRepository(pg).paper_text("library", plan.papers[0].identity())
    # 不把失败的缺页改成空白，也不在原run无条件重新购买。
    again = asyncio.run(ingest_library(pg, plan, settings, rates(), tmp_path/"run", "library"))
    assert len(calls) == 2 and again["stored_pages"] == 1


def test_pg_library_cas_damage_blocks_export(pg, tmp_path):
    repo = LibraryRepository(pg)
    pg.run("x", {"mode": "pdf_library", "status": "running", "fingerprint": "x"})
    make_pdf(tmp_path/"a.pdf")
    paper = inventory_directory(tmp_path, 1).papers[0]
    repo.register("x", paper, {"profile": "x"})
    doc = repo.documents("x")[0]
    assert hashlib.sha256(repo.objects.get_bytes(*doc[3:])).hexdigest() == doc[1]
    with pytest.raises(ValueError, match="Incomplete"):
        repo.paper_text("x", paper.identity())
    # 窄事务回滚：不能把超出物理页的输出写入库。
    batch = ReadBatch(pages=[readable(1), readable(3)])
    pg.save_request("x", {"request_id": "request-x", "role": "generator", "actual_model": "gpt-6-luna",
                          "usage": {}, "content": batch.model_dump_json()})
    with pytest.raises(ValueError, match="exceeds"):
        repo.save_batch(doc[0], batch, "request-x", "gpt-6-luna")
    assert repo.pages(doc[0]) == []
