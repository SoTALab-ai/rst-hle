"""按冻结期刊DOI候选队列补齐论文库；下载、理论筛选和后续全文转录分开计数。"""
import asyncio
import hashlib
import json
import os
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field
from pypdf.errors import PyPdfError

from .costs import MeteredGateway, measure_cost
from .llm import CacheIntegrityError, GatewayConfigurationError
from .models import StrictModel, digest
from .open_access import openalex_locations, scoap_locations
from .paper_ingest import inventory_receipts
from .pdf_library import PaperPdfPlan, PdfInput, PdfReceipt, collect_paper
from .runtime import atomic_json


class TheoryTriage(StrictModel):
    paper_identity_matches: bool
    classification: Literal["theoretical", "experimental", "mixed", "uncertain"]
    reconstructible_derivation: bool
    evidence: str = Field(min_length=30)
    topic: str = Field(min_length=3)


def balanced_candidates(records):
    groups = defaultdict(list)
    for row in records:
        if not row["updates"]:
            groups[(row["domain"], row["journal_id"])].append(row)
    result = []
    while any(groups.values()):
        for key in sorted(groups):
            if groups[key]:
                result.append(groups[key].pop(0))
    return result


def resolve_one(row, folder):
    checkpoint = folder / (digest(row["doi"])+".json")
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if saved["seal"] != digest(saved["body"]):
            raise ValueError("Corrupt resolution checkpoint")
        return saved["body"]
    failures, locations = [], []
    with httpx.Client(timeout=45, follow_redirects=True, headers={"User-Agent": "rst-hle/0.1 paper-library"}) as client:
        if row["journal_id"] in ("prl", "prd", "jhep"):
            try:
                locations = scoap_locations(client, row["doi"])
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
                failures.append(type(error).__name__)
        try:
            locations += openalex_locations(client, row["doi"], api_key=os.environ.get("OPENALEX_API_KEY"))
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            failures.append(type(error).__name__)
    body = {"doi": row["doi"], "locations": [p.model_dump() for p in locations], "failures": failures}
    atomic_json(checkpoint, {"body": body, "seal": digest(body)})
    return body


def download_one(row, location, directory):
    plan = PaperPdfPlan(doi=row["doi"], title=row["title"], journal=row["journal"], domain=row["domain"],
        metadata_url=row["metadata_source"], documents=[PdfInput(role="main", location=location["pdf_url"],
            version=location["version"], provenance_url=location["index_url"])])
    with httpx.Client(timeout=120, headers={"User-Agent": "rst-hle/0.1 paper-library"}) as client:
        receipt, _reused = collect_paper(plan, directory, client=client, pause=time.sleep)
    return receipt


async def acquire_library(store, discovery, settings, pricing, directory, run_id, *, reuse_root=None,
                          target_per_domain=100):
    directory = Path(directory)
    manifest = {"run_id": run_id, "discovery_sha256": digest(discovery), "target_per_domain": target_per_domain,
        "reuse_root": str(reuse_root), "config": settings.model_dump(), "pricing": pricing,
        "code_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
            sorted(Path("src/diamond_factory").rglob("*")) if p.is_file() and p.suffix in (".py", ".sql")}}
    fingerprint = digest(manifest)
    prior = store.runs().get(run_id)
    if prior and prior["fingerprint"] != fingerprint:
        raise ValueError("Frozen acquisition changed; use a new run-id")
    path = directory/"manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("Frozen acquisition directory changed")
    atomic_json(path, manifest)
    store.run(run_id, {"mode": "pdf_acquisition", "status": "running", "fingerprint": fingerprint})
    gateway = MeteredGateway(settings, directory/"requests", store, run_id, pricing=pricing,
                             visible_cost_limit_usd=None)
    accepted = directory/"accepted"
    accepted.mkdir(exist_ok=True)
    if reuse_root:
        for p in sorted(Path(reuse_root).glob("*/receipt.json")):
            receipt = PdfReceipt.model_validate_json(p.read_text())
            target = accepted/receipt.plan_sha256
            if not target.exists():
                shutil.copytree(p.parent, target)
    counts, dois, hashes = Counter(), set(), set()
    for p in sorted(accepted.glob("*/receipt.json")):
        receipt = PdfReceipt.model_validate_json(p.read_text())
        if receipt.paper.doi in dois or receipt.documents[0].pdf_sha256 in hashes:
            raise ValueError("Duplicate accepted PDF/DOI")
        counts[receipt.paper.domain] += 1
        dois.add(receipt.paper.doi)
        hashes.add(receipt.documents[0].pdf_sha256)
    reused = len(list(Path(reuse_root).glob("*/receipt.json"))) if reuse_root else 0
    # 下载串行，遵守原采集器逐跳3.1秒间隔；独立模型筛选不重复识别PDF。
    for row in balanced_candidates(discovery["records"]):
        if all(counts[d] >= target_per_domain for d in ("Math", "Physics")):
            break
        if row["doi"] in dois or counts[row["domain"]] >= target_per_domain:
            continue
        key = digest(row["doi"])
        decision_path = directory/"decisions"/(key+".json")
        if decision_path.exists():
            continue
        decision = {"doi": row["doi"], "domain": row["domain"], "accepted": False, "errors": []}
        resolution = await asyncio.to_thread(resolve_one, row, directory/"resolutions")
        for location in resolution["locations"]:
            try:
                receipt = await asyncio.to_thread(download_one, row, location, directory/"downloads")
                if receipt["documents"][0]["pdf_sha256"] in hashes:
                    decision["errors"].append("DuplicatePdfContent")
                    continue
                folder = directory/"downloads"/receipt["plan_sha256"]
                text = (folder/receipt["documents"][0]["text_path"]).read_text()
                # 这里只看研究类型/是否有可重建推导，不把普通抽字用于公式验真。
                triage, rid, model = await gateway.call("generator",
                    "Classify this journal paper for a theoretical mathematics/physics question library. "
                    "Use only supplied bibliographic metadata and locator text from its original PDF. "
                    "Compare the actual PDF title/authors with metadata and flag paper identity mismatches. "
                    "This is NOT an OCR audit. Do not certify formula accuracy or answer a scientific question. "
                    "Select theoretical only if analytical definitions, mechanisms or mathematical results "
                    "and a reconstructible derivation dominate. Experiments, measurement campaigns, survey-only "
                    "papers and uncertain evidence are not admitted. Preserve doubts explicitly.\n" + json.dumps(
                        {"title": row["title"], "journal": row["journal"], "abstract": row["abstract"],
                         "locator_text": text[:65000]}, ensure_ascii=False),
                    f"acquisition:{key}:theory", TheoryTriage)
                decision.update(triage=triage.model_dump(), request_id=rid, actual_model=model,
                                plan_sha256=receipt["plan_sha256"])
                if (not triage.paper_identity_matches or triage.classification != "theoretical"
                        or not triage.reconstructible_derivation):
                    break
                target = accepted/receipt["plan_sha256"]
                if not target.exists():
                    shutil.copytree(folder, target)
                counts[row["domain"]] += 1
                dois.add(row["doi"])
                hashes.add(receipt["documents"][0]["pdf_sha256"])
                decision["accepted"] = True
                break
            except (CacheIntegrityError, GatewayConfigurationError):
                raise
            except (ValueError, RuntimeError, OSError, httpx.HTTPError, PyPdfError) as error:
                decision["errors"].append(type(error).__name__)
        atomic_json(decision_path, decision)
        print(json.dumps({"event": "paper_acquired", "doi": row["doi"], "accepted": decision["accepted"],
                          "totals": dict(counts)}, ensure_ascii=False), flush=True)
        atomic_json(directory/"progress.json", {"counts": dict(counts), "reused": reused})
    plan = inventory_receipts(accepted, target_per_domain*2)
    atomic_json(directory/"library-plan.json", plan.model_dump())
    decisions = [json.loads(p.read_text()) for p in (directory/"decisions").glob("*.json")]
    report = {"run_id": run_id, "target_papers": target_per_domain*2, "downloaded_papers": sum(counts.values()),
        "reused_papers": reused, "counts": dict(counts), "attempted_new_dois": len(decisions),
        "newly_accepted": sum(d["accepted"] for d in decisions),
        "pdf_pages": sum(d.pages for p in plan.papers for d in p.documents),
        "supplement_status": dict(Counter(p.supplement_status for p in plan.papers)),
        "cost": measure_cost(store, run_id, directory/"requests", settings, pricing),
        "status": "downloaded" if len(plan.papers) == target_per_domain*2 else "incomplete"}
    atomic_json(directory/"report.json", report)
    store.run(run_id, {"mode": "pdf_acquisition", "status": report["status"], "fingerprint": fingerprint,
                       "manifest": manifest, "report": report})
    return report
