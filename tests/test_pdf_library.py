import json

import httpx
import pytest
from pypdf import PdfWriter

from diamond_factory.pdf_library import PaperPdfPlan, PdfInput, collect_library, collect_paper


def pdf(path, title):
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_metadata({"/Title": title})
    w.write(path)
    return path


def plan(main, supplement=None):
    documents = [PdfInput(role="main", location=str(main), version="publishedVersion",
                          provenance_url="https://example.org/article")]
    if supplement:
        documents.append(PdfInput(role="supplement", location=str(supplement), version="publishedVersion",
                                  provenance_url="https://example.org/supplement"))
    return PaperPdfPlan(doi="10.1000/fixture", title="Controlled synthetic paper fixture", journal="Fixture journal",
        domain="Math", metadata_url="https://example.org/metadata", documents=documents,
        supplement_status="separate" if supplement else "not_checked",
        supplement_evidence="Publisher lists the supplied supplementary PDF" if supplement else "")


def test_main_and_supplement_are_separate_and_no_text_extraction_can_auto_approve(tmp_path):
    p = plan(pdf(tmp_path/"main.pdf", "main"), pdf(tmp_path/"supp.pdf", "supplement"))
    with httpx.Client() as c:
        row, reused = collect_paper(p, tmp_path/"library", client=c)
    assert not reused and not row["ready_for_authoring"] and row["formula_review"] == "pending"
    assert [d["role"] for d in row["documents"]] == ["main", "supplement"]
    assert [d["pdf_path"] for d in row["documents"]] == ["00-main.pdf", "01-supplement.pdf"]
    assert all(d["empty_text_pages"] == [1] for d in row["documents"])
    assert row["paper"]["supplement_status"] == "separate"


def test_integrity_checked_cache_is_reused_without_network_and_corruption_is_rejected(tmp_path):
    p = plan(pdf(tmp_path/"main.pdf", "main"))
    with httpx.Client() as c:
        row, _ = collect_paper(p, tmp_path/"library", client=c)
    # 缓存合法时，即使原始文件已不在，也不重新采集。
    (tmp_path/"main.pdf").unlink()
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))) as c:
        restored, reused = collect_paper(p, tmp_path/"library", client=c)
        assert reused and restored == row
        file = tmp_path/"library"/row["plan_sha256"]/"00-main.pdf"
        file.write_bytes(b"corrupt")
        with pytest.raises(ValueError, match="Corrupt paper cache"):
            collect_paper(p, tmp_path/"library", client=c)


def test_receipt_tampering_and_false_supplement_completeness_are_rejected(tmp_path):
    p = plan(pdf(tmp_path/"main.pdf", "main"))
    with pytest.raises(ValueError, match="requires locatable evidence"):
        PaperPdfPlan.model_validate({**p.model_dump(), "supplement_status": "none_confirmed"})
    with httpx.Client() as c:
        row, _ = collect_paper(p, tmp_path/"library", client=c)
        receipt = tmp_path/"library"/row["plan_sha256"]/"receipt.json"
        row["note"] = "Changed after collection"
        receipt.write_text(json.dumps(row))
        with pytest.raises(ValueError, match="integrity mismatch"):
            collect_paper(p, tmp_path/"library", client=c)


def test_html_and_private_redirect_fail_without_discarding_other_papers(tmp_path):
    good = plan(pdf(tmp_path/"main.pdf", "main"))
    bad = good.model_copy(update={"doi": "10.1000/bad", "documents": [good.documents[0].model_copy(
        update={"location": "https://example.org/not-a-pdf"})]})
    redirect = bad.model_copy(update={"doi": "10.1000/private", "documents": [bad.documents[0].model_copy(
        update={"location": "https://example.org/redirect"})]})
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
        return httpx.Response(200, content=b"<html>Not a paper</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        result = collect_library([bad, good, redirect], tmp_path/"library", client=c)
    assert result["planned"] == 3 and result["downloaded"] == 1 and result["failed"] == 2
    assert result["ready_for_authoring"] == result["model_calls"] == result["database_writes"] == 0
    assert len(calls) == 2 and not list((tmp_path/"library").rglob("*.part"))


def test_duplicate_pdf_cannot_count_as_both_main_and_supplement(tmp_path):
    main = pdf(tmp_path/"main.pdf", "same file")
    main.with_name("copy.pdf").write_bytes(main.read_bytes())
    with httpx.Client() as c, pytest.raises(ValueError, match="same PDF cannot count"):
        collect_paper(plan(main, main.with_name("copy.pdf")), tmp_path/"library", client=c)


def test_partial_bundle_recovers_checked_main_without_refetching_it(tmp_path):
    main = pdf(tmp_path/"main.pdf", "main")
    supp = tmp_path/"supp.pdf"
    p = plan(main, supp)
    with httpx.Client() as c:
        first = collect_library([p], tmp_path/"library", client=c)
        assert first["failed"] == 1 and first["downloaded"] == 0
        main.unlink()
        pdf(supp, "supplement")
        second = collect_library([p], tmp_path/"library", client=c)
        assert second["downloaded"] == 1 and second["failed"] == 0
        assert second["ready_for_authoring"] == 0
