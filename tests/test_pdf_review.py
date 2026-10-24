import json
import shutil

import pytest
from pypdf import PdfWriter

from diamond_factory.models import digest
from diamond_factory.pdf_review import (
    FormulaAudit,
    MaterialSelection,
    PageRef,
    PageTranscript,
    SupplementCheck,
    checked_source,
    render_page,
    selection_errors,
    verify_request,
)


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="Real Poppler renderer not installed")
def test_real_renderer_preserves_dotted_page_prefix_and_reuses_output(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = tmp_path / "00-main.pdf"
    writer.write(pdf)
    prefix = tmp_path / "renders" / "00-main.pdf-1"
    png = render_page(pdf, 1, prefix)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert (prefix.parent / "00-main.pdf-1.png").read_bytes() == png
    # 已渲染的固定页面复用，不再执行外部渲染器。
    assert render_page(pdf, 1, prefix, executable="must-not-be-run") == png


def material():
    receipt = {"paper": {"doi": "10.1234/test", "title": "A synthetic PDF theory control"},
               "integrity_sha256": "c" * 64, "documents": [
                   {"pdf_path": "00-main.pdf", "pdf_sha256": "a" * 64, "pages": 2}]}
    refs = [PageRef(document="00-main.pdf", page=p) for p in (1, 2)]
    selection = MaterialSelection(identity_matches=True, theoretical_result=True, result_label="Lemma 2",
        authoring_focus="Reconstruct the complete quadratic coefficient identity from Lemma 2.",
        pages=refs, coverage_reason="Both the definition and the complete derivation occupy these two pages.",
        missing_dependencies=[])
    check = SupplementCheck(doi="10.1234/test", main_pdf_sha256="a"*64, status="no_separate_listed",
        evidence_urls=["https://example.org/test"], evidence="Checked the complete source and its repository file listing.",
        embedded_pages=[], unresolved=[])
    transcripts = [PageTranscript(page=r, text="Synthetic control: the exact defining expression is x^2 + 1.",
                                  formula_labels=["2"], unreadable=[]) for r in refs]
    audit = FormulaAudit(faithful_to_images=True, all_formulas_checked=True,
        definitions_and_conditions_preserved=True, issues=[], reason="Compared every expression and its surrounding conditions.")
    return receipt, check, selection, transcripts, audit


def test_only_selected_reviewed_pages_enter_versioned_source():
    r, c, s, ts, a = material()
    source = checked_source(r, c, s, ts, [(ts, a)])
    assert source.id.startswith("pdf:") and "physical page 2" in source.text
    changed = [ts[0].model_copy(update={"text": ts[0].text + " Changed."}), ts[1]]
    assert checked_source(r, c, s, changed, [(changed, a)]).id != source.id
    with pytest.raises(ValueError, match="different transcription"):
        checked_source(r, c, s, changed, [(ts, a)])


def test_missing_page_review_and_unreadable_formula_fail_closed():
    r, c, s, ts, a = material()
    for audits in ([], [(ts[:1], a)], [(ts, a), (ts[:1], a)]):
        with pytest.raises(ValueError, match="formula transcription"):
            checked_source(r, c, s, ts, audits)
    ts[0].unreadable = ["The exponent is illegible"]
    with pytest.raises(ValueError, match="formula transcription"):
        checked_source(r, c, s, ts, [(ts, a)])


def test_wrong_pdf_supplement_and_page_mapping_are_rejected():
    r, c, s, ts, a = material()
    c.main_pdf_sha256 = "b"*64
    with pytest.raises(ValueError, match="another PDF version"):
        checked_source(r, c, s, ts, [(ts, a)])
    s.pages[0].page = 3
    assert "page_outside_frozen_pdf" in selection_errors(s, r)
    s.pages = [s.pages[1], s.pages[1]]
    assert "duplicate_page_reference" in selection_errors(s, r)


def test_failed_audit_and_missing_dependencies_cannot_be_admitted():
    r, c, s, ts, a = material()
    a.issues = ["Coefficient is wrong"]
    with pytest.raises(ValueError, match="Unresolved formula audit"):
        checked_source(r, c, s, ts, [(ts, a)])
    s.missing_dependencies = ["Definition is only in another paper"]
    assert "result_dependencies_missing" in selection_errors(s, r)


def test_corrupt_or_failed_real_receipt_cannot_certify_review(tmp_path):
    trace = {"request_id": "request", "actual_model": "gpt-6-luna", "content": "review", "response_error": None}
    trace["integrity_sha256"] = digest(trace)
    p = tmp_path / "request.json"
    p.write_text(json.dumps(trace))
    assert verify_request(tmp_path, "request")["content"] == "review"
    trace["content"] = "changed"
    p.write_text(json.dumps(trace))
    with pytest.raises(ValueError, match="Corrupt"):
        verify_request(tmp_path, "request")
    trace["response_error"] = "deadline_exceeded"
    trace["integrity_sha256"] = digest({k: v for k, v in trace.items() if k != "integrity_sha256"})
    p.write_text(json.dumps(trace))
    with pytest.raises(ValueError, match="Failed model"):
        verify_request(tmp_path, "request")
