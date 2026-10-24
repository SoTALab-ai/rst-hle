"""在冻结目录内定位理论结果，按PDF原图转录并独立复核；失败不放行。"""
import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import httpx

from diamond_factory.config import load
from diamond_factory.costs import MeteredGateway, measure_cost
from diamond_factory.models import digest
from diamond_factory.pdf_library import PdfReceipt
from diamond_factory.pdf_ocr import MinerUAgent
from diamond_factory.pdf_review import (
    MaterialSelection,
    SupplementCheck,
    checked_source,
    selection_errors,
    transcribe_batch,
    verify_request,
)
from diamond_factory.runtime import atomic_json


async def run(args):
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    settings = load(args.config)
    pricing = json.loads(Path(args.rates).read_text())
    checks = [SupplementCheck.model_validate(x) for x in json.loads(Path(args.supplements).read_text())]
    by_doi = {c.doi: c for c in checks}
    input_dir = directory / "input"
    receipts = [PdfReceipt.model_validate_json(p.read_text()).model_dump()
                for p in sorted(input_dir.glob("*/receipt.json"))]
    if not receipts or len(by_doi) != len(checks) or set(by_doi) != {r["paper"]["doi"] for r in receipts}:
        raise ValueError("Supplement review must cover every planned paper exactly once")
    manifest = {"run_id": args.run_id, "config": settings.model_dump(), "pricing": pricing,
                "parser": args.parser,
                "code_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                    [Path(__file__), Path("src/diamond_factory/pdf_review.py"), Path("src/diamond_factory/pdf_ocr.py"),
                     Path("src/diamond_factory/llm.py")]},
                "receipts": [r["integrity_sha256"] for r in receipts],
                "supplements": [c.model_dump() for c in checks]}
    freeze = directory / "review-manifest.json"
    if freeze.exists() and json.loads(freeze.read_text()) != manifest:
        raise ValueError("Frozen review changed; use a new run-id and directory")
    atomic_json(freeze, manifest)
    gateway = MeteredGateway(settings, directory / "requests", run_id=args.run_id,
                             pricing=pricing, visible_cost_limit_usd=None)
    sem = asyncio.Semaphore(args.workers)
    ocr = MinerUAgent(directory / "ocr") if args.parser == "mineru" else None

    async def one(receipt):
        async with sem:
            key = receipt["plan_sha256"]
            row = {"doi": receipt["paper"]["doi"], "plan_sha256": key, "ready": False}
            try:
                supplement = by_doi[row["doi"]]
                if supplement.status == "unresolved":
                    raise ValueError("Supplementary materials remain unresolved")
                materials = []
                for doc in receipt["documents"]:
                    folder = input_dir / key
                    for field, sha in (("pdf_path", "pdf_sha256"), ("text_path", "text_sha256")):
                        if hashlib.sha256((folder / doc[field]).read_bytes()).hexdigest() != doc[sha]:
                            raise ValueError("Frozen PDF/text content hash mismatch")
                    materials.append({"document": doc["pdf_path"], "physical_pages": doc["pages"],
                                      "locator_only_text": (folder / doc["text_path"]).read_text()})
                selection, sid, model = await gateway.call("generator",
                    "Inspect ONLY this paper and its supplied supplements. Match the paper identity, then "
                    "select one compact EXISTING theoretical result with a complete reconstructible derivation. "
                    "Prefer a difficult non-obvious coefficient, identity, case classification or intermediate "
                    "mechanism, requiring a genuine bottleneck, not the famous headline theorem or mere "
                    "substitution. We will lock its existing answer, reconstruct its proof, and hide its hardest "
                    "necessary inference to make a self-contained short-answer examination problem. Do not "
                    "invent a new result. The raw PDF text below is ONLY a locator and may have broken formulas. "
                    "Choose at most TEN physical PDF pages covering the result, COMPLETE proof, every necessary "
                    "definition/normalization and domain assumption, using the [document; page] markers. "
                    "Consecutive pages are allowed; no reference paper will be appended. If a chosen result "
                    "needs an unavailable external theorem beyond standard background, choose a smaller "
                    "self-contained lemma. List missing_dependencies if no covered result fits. The authoring "
                    "focus must identify exactly the already-established target and proof sections.\n" +
                    json.dumps({"paper": receipt["paper"], "materials": materials}, ensure_ascii=False),
                    f"{key}:select-result", MaterialSelection)
                row.update(selection=selection.model_dump(), selection_request=sid, selection_model=model)
                errors = selection_errors(selection, receipt)
                if errors:
                    raise ValueError(";".join(errors))
                transcripts, audits, batches = [], [], []
                for i in range(0, len(selection.pages), 2):
                    refs = selection.pages[i:i+2]
                    batch, audit, attempts = await transcribe_batch(gateway, directory, receipt, refs,
                        salt=f"{key}:pages:{i//2}", executable=args.renderer, ocr=ocr)
                    for attempt in attempts:
                        if attempt["transcription_request"]:
                            verify_request(directory / "requests", attempt["transcription_request"])
                        verify_request(directory / "requests", attempt["audit_request"])
                    transcripts.extend(batch)
                    audits.append((batch, audit))
                    batches.append(attempts)
                source = checked_source(receipt, supplement, selection, transcripts, audits)
                row.update(ready=True, source=source.model_dump(), reviewed_pages=len(transcripts),
                           formula_count=sum(len(p.formula_labels) for p in transcripts), batches=batches,
                           selection_sha256=digest(selection.model_dump()))
            except (RuntimeError, ValueError, OSError, httpx.HTTPError, subprocess.SubprocessError) as e:
                # 外部异常可能带临时签名URL；公开报告只保留类型，原始收据留私有库。
                row.update(error_type=type(e).__name__, error=str(e)[:500] if
                           isinstance(e, (RuntimeError, ValueError)) else "External I/O failure; inspect private receipt")
            atomic_json(directory / "papers" / (key + ".json"), row)
            print(json.dumps({k: v for k, v in row.items() if k not in
                              ("source", "selection", "batches")}, ensure_ascii=False), flush=True)
            return row

    rows = await asyncio.gather(*(one(r) for r in receipts))
    report = {"run_id": args.run_id, "planned": len(rows), "ready": sum(r["ready"] for r in rows),
              "reviewed_pages": sum(r.get("reviewed_pages", 0) for r in rows),
              "formula_labels": sum(r.get("formula_count", 0) for r in rows),
              "cost": measure_cost(None, args.run_id, directory / "requests", settings, pricing),
              "scope": "Only selected result/proof pages were image-reviewed, not every formula in every PDF.",
              "papers": [{k: v for k, v in r.items() if k != "source"} for r in rows]}
    tasks = [json.loads(p.read_text()) for p in (directory / "ocr").glob("*/receipt.json")]
    report["external_ocr"] = {"provider": args.parser, "tasks_submitted": sum(t["submitted"] for t in tasks),
        "tasks_completed": sum(bool(t.get("markdown_sha256")) for t in tasks),
        "known_cost_usd": None, "billing_identity_supplied": False,
        "note": "No billing credentials used; no returned fee receipt. Do not record unknown external fees as zero."}
    atomic_json(directory / "review-report.json", report)
    (directory / "sources.jsonl").write_text("\n".join(json.dumps(r["source"], ensure_ascii=False)
                                                      for r in rows if r["ready"]) + "\n")
    print(json.dumps({k: report[k] for k in ("planned", "ready", "reviewed_pages", "cost")}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--directory", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--rates", required=True)
    p.add_argument("--supplements", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    p.add_argument("--renderer", default="pdftoppm")
    p.add_argument("--parser", choices=("mineru", "model_vision"), default="mineru")
    asyncio.run(run(p.parse_args()))
