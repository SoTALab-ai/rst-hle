import base64
import hashlib
import json

import httpx
import pytest
from test_pdf_library import pdf, plan

from diamond_factory.models import digest
from diamond_factory.pdf_library import collect_paper
from diamond_factory.pdf_ocr import _save
from diamond_factory.pdf_review import (
    FormulaAudit,
    MaterialSelection,
    PageRef,
    PageTranscript,
    SupplementCheck,
    Transcription,
    admit_material,
    checked_source,
)
from diamond_factory.runtime import atomic_json


def journal(directory, content, request=None):
    row = {"content": content, "actual_model": "synthetic-control-model", "response_error": None,
           "request": request or {}}
    key = digest(row)
    row["request_id"] = key
    _save(directory / "requests" / (key + ".json"), row)
    return key


def admitted_fixture(tmp_path):
    material = plan(pdf(tmp_path / "original.pdf", "Synthetic transport control"))
    with httpx.Client() as c:
        receipt, _ = collect_paper(material, tmp_path / "input", client=c)
    key = receipt["plan_sha256"]
    ref = PageRef(document="00-main.pdf", page=1)
    selection = MaterialSelection(identity_matches=True, theoretical_result=True, result_label="Control lemma",
        authoring_focus="Reconstruct the exact coefficient in the synthetic control lemma.", pages=[ref],
        coverage_reason="All definitions and the complete synthetic derivation occupy one page.", missing_dependencies=[])
    supplement = SupplementCheck(doi=material.doi, main_pdf_sha256=receipt["documents"][0]["pdf_sha256"],
        status="no_separate_listed", evidence_urls=["https://example.org/control"],
        evidence="Synthetic complete repository listing has no supplementary document.", embedded_pages=[], unresolved=[])
    text = "Synthetic control only. For positive real x the defining expression is x squared plus one."
    transcript = Transcription(pages=[PageTranscript(page=ref, text=text, formula_labels=[], unreadable=[])])
    audit = FormulaAudit(faithful_to_images=True, all_formulas_checked=True,
        definitions_and_conditions_preserved=True, issues=[], reason="Synthetic structural transport control, not a scientific audit.")
    image = b"\x89PNG\r\n\x1a\nsynthetic-image-control"
    image_sha = hashlib.sha256(image).hexdigest()
    render = tmp_path / "renders" / key / "00-main.pdf-1.png"
    render.parent.mkdir(parents=True)
    render.write_bytes(image)
    request = {"messages": [{"role": "system", "content": "fixture"}, {"role": "user", "content": [
        {"type": "text", "text": "Compare original:\n" + transcript.model_dump_json() +
            "\nReturn ONLY valid JSON satisfying this schema:\n{}"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode()}}]}]}
    aid = journal(tmp_path, audit.model_dump_json(), request)
    sid = journal(tmp_path, selection.model_dump_json())
    native_key = "n" * 64
    folder = tmp_path / "ocr" / native_key
    folder.mkdir(parents=True)
    (folder / "page.md").write_text(text)
    _save(folder / "receipt.json", {"identity": {"pdf_sha256": supplement.main_pdf_sha256,
        "params": {"page_range": "1-1"}}, "markdown_sha256": hashlib.sha256(text.encode()).hexdigest()})
    salt = key + ":pages:0"
    atomic_json(tmp_path / "batches" / (digest(salt) + ".json"), {"salt": salt, "pages": [ref.model_dump()],
        "transcript": transcript.model_dump(), "image_sha256": [image_sha], "attempts": [{
            "audit_request": aid, "audit": audit.model_dump(), "transcription_request": None,
            "ocr_receipts": [native_key]}]})
    source = checked_source(receipt, supplement, selection, transcript.pages, [(transcript.pages, audit)])
    row = {"plan_sha256": key, "selection": selection.model_dump(), "selection_request": sid,
           "source": source.model_dump(), "ready": True}
    return row, supplement, source, folder, render


def test_admission_recomputes_source_and_rejects_stale_native_content(tmp_path):
    row, supplement, source, folder, _ = admitted_fixture(tmp_path)
    assert admit_material(tmp_path, row, supplement) == source
    text = "A changed formula, even though a stale ready flag remains true."
    (folder / "page.md").write_text(text)
    native = json.loads((folder / "receipt.json").read_text())
    native["markdown_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    _save(folder / "receipt.json", native)
    with pytest.raises(ValueError, match="differs from the reviewed"):
        admit_material(tmp_path, row, supplement)


def test_admission_rejects_image_and_source_version_changes(tmp_path):
    row, supplement, _, _, render = admitted_fixture(tmp_path)
    row["source"]["id"] = "pdf:unrelated-source"
    with pytest.raises(ValueError, match="recomputed admitted material"):
        admit_material(tmp_path, row, supplement)
    render.write_bytes(b"changed image")
    with pytest.raises(ValueError, match="Rendered page cache changed"):
        admit_material(tmp_path, row, supplement)
