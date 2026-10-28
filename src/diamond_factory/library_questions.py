"""从已完整解析的论文冻结材料里程碑，规划独立出题线；不跳过现有科学质检。"""
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .costs import MeteredGateway, measure_cost
from .experiment import ExperimentPlan, QuestionExperiment, RootPlan
from .llm import CacheIntegrityError, GatewayConfigurationError, ModelResponseError
from .models import Source, StrictModel, digest
from .paper_ingest import LibraryRepository, ReadPage
from .paper_metadata import canonical_doi
from .provenance import match_quote
from .runtime import atomic_json, gather_cancel_on_error


class SnapshotDocument(StrictModel):
    ordinal: int = Field(ge=0)
    role: Literal["main", "supplement"]
    pdf_sha256: str
    parse_id: str
    pages: list[ReadPage] = Field(min_length=1)
    reader_requests: list[str] = Field(min_length=1)


class SnapshotPaper(StrictModel):
    id: str
    title: str
    doi: str | None
    journal: str
    domain: Literal["Math", "Physics"]
    supplement_status: str
    documents: list[SnapshotDocument] = Field(min_length=1)


class MaterialMilestone(StrictModel):
    library_run_id: str
    required_materials: int = Field(ge=1, le=200)
    papers: list[SnapshotPaper] = Field(min_length=1, max_length=200)
    excluded_paper_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete_cohort(self):
        if (len(set(self.excluded_paper_ids)) != len(self.excluded_paper_ids)
                or set(self.excluded_paper_ids) & {p.id for p in self.papers}):
            raise ValueError("Excluded materials must be unique and disjoint from the selected cohort")
        if len(self.papers) != self.required_materials or len({p.id for p in self.papers}) != len(self.papers):
            raise ValueError("Milestone must contain exactly the required distinct complete materials")
        main_hashes = []
        for paper in self.papers:
            mains = [doc.pdf_sha256 for doc in paper.documents if doc.role == "main"]
            if len(mains) != 1:
                raise ValueError("Each material needs exactly one original main PDF")
            main_hashes.extend(mains)
        dois = [canonical_doi(paper.doi) for paper in self.papers if paper.doi]
        if len(set(main_hashes)) != len(main_hashes) or len(set(dois)) != len(dois):
            raise ValueError("PDF/DOI revisions do not add distinct materials")
        return self


class SelectedPage(StrictModel):
    document: int = Field(ge=0)
    page: int = Field(ge=1)


class QuestionLine(StrictModel):
    id: str = Field(pattern=r"^q[1-5]$")
    category: Literal["Math", "Physics"]
    subtopic: str
    result_label: str = Field(min_length=3)
    target: str = Field(min_length=20)
    provisional_answer: str = Field(min_length=1)
    bottleneck: str = Field(min_length=30)
    structural_delta: str = Field(min_length=30)
    source_quote: str = Field(min_length=20, max_length=1000)
    pages: list[SelectedPage] = Field(min_length=1, max_length=50)


class PaperOutline(StrictModel):
    outcome: Literal["ready", "unavailable"]
    lines: list[QuestionLine] = Field(max_length=5)
    reason: str = Field(min_length=20)

    @model_validator(mode="after")
    def bounded_independent_lines(self):
        if (self.outcome == "ready") != bool(self.lines):
            raise ValueError("Ready requires 1–5 lines; unavailable must not invent a line")
        if (len({line.id for line in self.lines}) != len(self.lines)
                or len({line.bottleneck for line in self.lines}) != len(self.lines)):
            raise ValueError("Duplicate ids/bottlenecks do not add independent lines")
        return self


class OutlineReview(StrictModel):
    keep_ids: list[str]
    reason: str = Field(min_length=30)


# 仅同一分类的已核定语言/字形别名；不把凝聚态等缺失分类猜成粒子物理。
SUBTOPIC_ALIASES = {
    ("Physics", "量子場論と粒子物理"): "量子场论与粒子物理",
    ("Physics", "量子场论與粒子物理"): "量子场论与粒子物理",
    ("Physics", "量子場論與粒子物理"): "量子场论与粒子物理",
    ("Physics", "quantum field theory and particle physics"): "量子场论与粒子物理",
    ("Physics", "電磁学"): "电磁学",
}


def admit_lines(paper, lines, allowed, max_chars, *, source_fidelity="strict"):
    """逐条登记入口结果；一条无效不能抹去同材料的其他有效规划。"""
    selected, receipts = [], []
    for original in lines:
        line = original.model_copy(update={"subtopic": SUBTOPIC_ALIASES.get(
            (original.category, original.subtopic), original.subtopic)})
        receipt = {"line_id": line.id, "original_subtopic": original.subtopic,
                   "canonical_subtopic": line.subtopic, "scientific_pass": False}
        try:
            if (line.category, line.subtopic) not in allowed:
                raise ValueError("Invalid v3 category/subtopic")
            source = source_for_line(paper, line, max_chars, source_fidelity=source_fidelity)
            match = match_quote(line.source_quote, source.text)
            receipt.update(status="admitted", source_sha256=source.sha256,
                           anchor=vars(match) if match else None,
                           source_notes=[] if match else ["nonliteral_source_anchor_advisory"],
                           source_fidelity=source_fidelity)
            selected.append((line, source))
        except ValueError as error:
            receipt.update(status="rejected", error=str(error))
        receipts.append(receipt)
    return selected, receipts


def complete_paper_ids(store, library_run_id, limit=50):
    # 登记200篇不触发；所有正文/补充的物理页均已存且无未读清，才计一份完整材料。
    return [r[0] for r in store.db.execute("""WITH docs AS (
        SELECT r.paper_id,r.ordinal,d.pages,count(lp.page) AS stored,
            count(lp.page) FILTER(WHERE lp.status='unreadable') AS unclear,
            count(lp.page) FILTER(WHERE lp.status='readable') AS readable,max(lp.created_at) AS completed
        FROM diamond.library_run_documents r JOIN diamond.library_parses p ON p.id=r.parse_id
        JOIN diamond.library_documents d ON d.id=p.document_id
        LEFT JOIN diamond.library_pages lp ON lp.parse_id=p.id
        WHERE r.run_id=%s GROUP BY r.paper_id,r.ordinal,d.pages)
        SELECT docs.paper_id FROM docs JOIN diamond.library_papers paper ON paper.id=docs.paper_id
        WHERE paper.domain IN ('Math','Physics') GROUP BY docs.paper_id
        HAVING bool_and(stored=pages AND unclear=0) AND sum(readable)>0
        ORDER BY max(completed),docs.paper_id LIMIT %s""", (library_run_id, limit))]


def snapshot_paper(store, library_run_id, paper_id):
    row = store.db.execute("SELECT title,doi,journal,domain,metadata FROM diamond.library_papers WHERE id=%s",
                           (paper_id,)).fetchone()
    if not row:
        raise ValueError("Unknown library paper")
    repo = LibraryRepository(store)
    # 重放完整性/物理页覆盖；不以SQL计数替代对象校验。
    repo.paper_text(library_run_id, paper_id)
    docs = []
    for ordinal, role, pdf_sha, parse_id, key, sha, size in store.db.execute("""SELECT r.ordinal,pd.role,
        d.id,p.id,d.object_key,d.object_sha256,d.object_size FROM diamond.library_run_documents r
        JOIN diamond.library_paper_documents pd ON pd.paper_id=r.paper_id AND pd.ordinal=r.ordinal
        JOIN diamond.library_parses p ON p.id=r.parse_id JOIN diamond.library_documents d ON d.id=p.document_id
        WHERE r.run_id=%s AND r.paper_id=%s ORDER BY r.ordinal""", (library_run_id, paper_id)):
        store.objects.get_bytes(key, sha, size)
        requests = [r[0] for r in store.db.execute("SELECT DISTINCT request_id FROM diamond.library_pages "
                                                  "WHERE parse_id=%s ORDER BY request_id", (parse_id,))]
        docs.append(SnapshotDocument(ordinal=ordinal, role=role, pdf_sha256=pdf_sha, parse_id=parse_id,
                                     pages=repo.pages(parse_id), reader_requests=requests))
    return SnapshotPaper(id=paper_id, title=row[0], doi=row[1], journal=row[2], domain=row[3],
                         supplement_status=row[4]["supplement_status"], documents=docs)


def freeze_milestone(store, library_run_id, required, path, *, exclude=()):
    path = Path(path)
    excluded = sorted(set(exclude))
    if path.exists():
        milestone = MaterialMilestone.model_validate_json(path.read_text())
        if (milestone.library_run_id != library_run_id or milestone.required_materials != required
                or sorted(milestone.excluded_paper_ids) != excluded):
            raise ValueError("Frozen milestone changed; use a new run-id and directory")
        # 续跑验证同一批材料，不因更多论文完成而重选另一批。
        for paper in milestone.papers:
            if snapshot_paper(store, library_run_id, paper.id) != paper:
                raise CacheIntegrityError("Frozen paper parse or objects changed")
        return milestone
    ids = [pid for pid in complete_paper_ids(store, library_run_id, 200) if pid not in excluded][:required]
    if len(ids) < required:
        return None
    result = MaterialMilestone(library_run_id=library_run_id, required_materials=required,
                              papers=[snapshot_paper(store, library_run_id, pid) for pid in ids],
                              excluded_paper_ids=excluded)
    atomic_json(path, result.model_dump())
    return result


def planning_outline(paper, max_chars):
    """提供逐页定位摘录与截断声明，选定后从数据库读取完整原页，不把摘录当完整证明。"""
    pages = [(doc.ordinal, page) for doc in paper.documents for page in doc.pages]
    per_page = max(100, min(2000, (max_chars-20000)//len(pages)))
    return {"paper_id": paper.id, "title": paper.title, "doi": paper.doi, "journal": paper.journal,
        "supplement_status": paper.supplement_status,
        "pages": [{"document": doc, "physical_page": p.page, "formula_labels": p.formula_labels,
                   "locator_excerpt": p.text[:per_page], "excerpt_truncated": len(p.text)>per_page}
                  for doc, p in pages]}


def source_for_line(paper, line, max_chars, *, source_fidelity="strict"):
    by_page = {(doc.ordinal, page.page): (doc, page) for doc in paper.documents for page in doc.pages}
    keys = [(r.document, r.page) for r in line.pages]
    if len(keys) != len(set(keys)) or any(k not in by_page for k in keys):
        raise ValueError("Selected page missing or repeated")
    blocks = []
    for key in sorted(keys):
        doc, page = by_page[key]
        if page.status == "unreadable":
            raise ValueError("Unclear source page cannot enter planning")
        blocks.append(f"[PDF document {doc.ordinal}; SHA256 {doc.pdf_sha256}; "
                      f"physical page {page.page}; parse {doc.parse_id}]\n{page.text}")
    text = "\n\n".join(blocks)
    if source_fidelity == "strict" and match_quote(line.source_quote, text) is None:
        raise ValueError("Source anchor must match selected original pages without semantic rewriting")
    if len(text) > max_chars:
        raise ValueError("Complete selected proof exceeds model context; no silent truncation")
    identity = digest({"paper": paper.id, "line": line.model_dump(), "text": digest(text)})
    return Source(id="library:"+identity, title=paper.title, uri="https://doi.org/"+paper.doi if paper.doi
                  else "library:"+paper.id, text=text)


async def plan_questions(store, milestone, settings, pricing, directory, run_id):
    from .scientific_goals import load_references

    directory = Path(directory)
    references = load_references(settings)
    manifest = {"run_id": run_id, "milestone_sha256": digest(milestone.model_dump()),
        "settings": settings.model_dump(), "pricing": pricing,
        "scientific_goal_references": references.model_dump() if references else None,
        "code_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
            sorted(Path("src/diamond_factory").rglob("*")) if p.is_file() and p.suffix in (".py", ".sql")}}
    fingerprint = digest(manifest)
    prior = store.runs().get(run_id)
    if prior and prior["fingerprint"] != fingerprint:
        raise ValueError("Frozen question planning changed; use a new run-id")
    freeze = directory/"manifest.json"
    if freeze.exists() and json.loads(freeze.read_text()) != manifest:
        raise ValueError("Question planning snapshot changed")
    atomic_json(freeze, manifest)
    store.run(run_id, {"mode": "library_question_planning", "status": "running", "fingerprint": fingerprint})
    gateway = MeteredGateway(settings, directory/"requests", store, run_id, pricing=pricing,
                             visible_cost_limit_usd=None)
    quotas = json.loads(Path(settings.quotas_path).read_text())["quotas"]
    allowed = {(q["category"], q["subtopic"]) for q in quotas if q["category"] in ("Math", "Physics")}
    sem = asyncio.Semaphore(4)
    advisory = settings.source_fidelity == "advisory"

    async def one(index, paper):
        row = {"paper_id": paper.id, "doi": paper.doi, "planned_roots": 0, "roots": []}
        async with sem:
            try:
                outline, rid, model = await gateway.call("generator",
                    "Plan 1–5 STRUCTURALLY INDEPENDENT scientific goals from this ONE "
                    "paper and its supplied supplements. You see page locator excerpts, NOT complete proofs. " +
                    ("Select physical pages containing an inspiring mechanism and definitions; you may propose "
                     "a self-contained constructed variant, with its answer proved independently later. "
                     "Label this as a construction rather than claiming it is a theorem of the paper. " if advisory else
                     "Select physical pages covering each EXISTING exact result, complete proof and all needed ") +
                    "definitions. Do not rely on unavailable figure values, external papers or missing supplements. " +
                    ("Choose scientific objectives only; defer answer form and candidate answers. " if references else
                     "Choose scientific objectives first, then their natural answer forms and candidate answers; ") +
                    "there is no answer-length or quantitative-only requirement. Reconstruct proofs later, "
                    "and hide the hardest necessary "
                    "inference. Prefer non-obvious intermediate mechanisms, branch/domain classification or "
                    "coupled derivations; avoid a headline theorem, coefficient lookup or direct substitution. "
                    "Challenge each proposal with the shortest CORRECT route from its intended primitive inputs. "
                    "Use bottleneck and structural_delta to describe that route and what non-routine inference remains. "
                    "Reject proposals resolved by routine coefficient matching, a small matrix inverse, "
                    "Taylor/power counting, two quadratic inequalities or direct use of a standard closure lemma "
                    "unless you identify a further indispensable discovery. Branches, inverse wording, all-parameter "
                    "scope, specialist notation and a long reference proof do not establish depth. "
                    "Do not use an intentionally wrong shortcut as evidence against the best correct solution. "
                    "For constructed specializations, say which substantive mechanism survives: retaining only "
                    "the paper's vocabulary while reducing its coupled theory to isolated scalar algebra is insufficient. "
                    "Each line needs a DIFFERENT inference structure; rewordings or changed numbers do not add "
                    "lines. Use ids q1..q5. Use 1 line if only one is well supported, unavailable if none. " +
                    ("For this goal-first configuration, set provisional_answer to "
                     "deferred_until_scientific_goal_selection. The later dedicated scientific-goal "
                     "call precedes answer selection; do not choose a gold or answer format here. "
                     if references else "") +
                    ("Provide a 20+ character source_quote as a locator; paraphrase is advisory, not a rejection. "
                     if advisory else "Copy a 20+ character source_quote VERBATIM from a selected page, preferably prose; ") +
                    "it is only an anchor, not proof validation. Provisional answers remain untrusted. "
                    "Choose the exact category/subtopic pair from the supplied list.\n" + json.dumps(
                        {"source_fidelity": settings.source_fidelity, "correctness_basis": settings.correctness_basis,
                         "allowed_targets": sorted(allowed),
                         **({"high_difficulty_cases": references.model_dump()} if references else {}),
                         "material": planning_outline(paper, settings.max_source_chars)},
                        ensure_ascii=False), f"{paper.id}:plan-independent-lines", PaperOutline)
                row.update(outline=outline.model_dump(), planning_request=rid, planning_model=model)
                if not outline.lines:
                    row["status"] = "source_unavailable"
                    return row
                selected, receipts = admit_lines(paper, outline.lines, allowed, settings.max_source_chars,
                                                 source_fidelity=settings.source_fidelity)
                row["line_admission"] = receipts
                if not selected:
                    row.update(status="planning_failed", error_type="LineAdmissionError")
                    return row
                keep = [line.id for line, _ in selected]
                if len(selected) > 1:
                    review, rid, model = await gateway.call("auditor",
                        "Independently inspect whether these proposed question lines from ONE paper have "
                        "distinct necessary reasoning structures. Source-inspired constructed targets are allowed "
                        "when source_fidelity=advisory; source alignment alone does not reject them. "
                        "Different numbers, labels, equivalent formulations or splitting one identical "
                        "bottleneck do not add independent lines. This is planning review, NOT a scientific "
                        "pass or difficulty score; existing proof/consistency/verifier stages will still run.\n"+
                        json.dumps({"title": paper.title, "source_fidelity": settings.source_fidelity,
                                    "lines": [line.model_dump() for line, _ in selected]},
                                   ensure_ascii=False), f"{paper.id}:independence-review", OutlineReview)
                    keep = review.keep_ids
                    if len(keep) != len(set(keep)) or not set(keep) <= {line.id for line, _ in selected}:
                        raise ValueError("Reviewer returned unknown or duplicate line ids")
                    row.update(review=review.model_dump(), review_request=rid, review_model=model)
                for line, source in selected:
                    if line.id not in keep:
                        continue
                    focus = (("First select a scientific goal using the supplied high-difficulty cases, then its "
                        "natural answer and complete reference derivation. Use the source mechanism or an explicitly "
                        "justified construction; do not choose the answer form in this planning brief. " if references else
                        "Use this material as INSPIRATION. First select a self-contained theoretical model "
                        "and candidate answer, mark it as a construction, and independently prove it before masking. "
                        "Source alignment and literal citations are advisory. Do not invent unavailable empirical "
                        "data or treat the paper as proof of a changed claim. Keep the locked constructed target "
                        "and answer across repairs and easing. " if advisory else
                        "Use ONLY this selected PDF body/formula/table material. First independently lock "
                        "the EXISTING source result and golden answer. ") +
                        ("Record the complete reference derivation; faithful paper citations with applicable "
                         "hypotheses may justify steps, while adapted steps need explicit justification. "
                         if settings.correctness_basis == "paper_grounded" else
                         "Reconstruct the complete proof and verify each step before reverse construction. ") +
                        "The plan is untrusted guidance, not an "
                        "additional premise or a passed gold. Hide the hardest necessary inference and its "
                        "equivalent hints; preserve answer uniqueness and physical validity. Do not assume unavailable "
                        "figure values or external cited-paper content. Let the scientific goal determine the answer "
                        "form and length, with every requested output covered by the full reference solution. "
                        "Preserve the locked goal's obligations; the golden text need not itself contain the full proof. "
                        "Only ease overly hard valid questions; reject easy ones.\nUntrusted selected target: "+
                        json.dumps(line.model_dump(), ensure_ascii=False))
                    root = RootPlan(name=f"L50_{index+1:03d}_{line.id}", source_id=source.id,
                                    category=line.category, subtopic=line.subtopic, focus=focus)
                    store.add_source(source)
                    row["roots"].append(root.model_dump())
                row.update(status="planned" if row["roots"] else "independence_rejected",
                           planned_roots=len(row["roots"]))
            except (CacheIntegrityError, GatewayConfigurationError):
                raise
            except (ValueError, ModelResponseError) as error:
                row.update(status="planning_failed", error_type=type(error).__name__, error=str(error),
                           roots=[], planned_roots=0)
            finally:
                atomic_json(directory/"papers"/(paper.id+".json"), row)
            return row

    rows = await gather_cancel_on_error(*(one(i, p) for i, p in enumerate(milestone.papers)))
    roots = [RootPlan.model_validate(r) for row in rows for r in row["roots"]]
    # 保留已有单批100条合同；50份材料最多250条，按100分片，不改旧实验协议。
    plans = [ExperimentPlan(roots=roots[i:i+100], max_revisions=2, root_workers=4,
                            visible_cost_limit_usd=None) for i in range(0, len(roots), 100)]
    report = {"run_id": run_id, "materials": len(rows), "planned_independent_roots": len(roots),
        "planning_failed_materials": sum(row["status"]=="planning_failed" for row in rows),
        "unavailable_or_rejected_materials": sum(row["status"] in ("source_unavailable", "independence_rejected")
                                                  for row in rows),
        "question_shards": len(plans), "papers": rows,
        "cost": measure_cost(store, run_id, directory/"requests", settings, pricing),
        "scope": "Planning is not scientific validation, measured difficulty or delivery; same-paper roots are correlated."}
    atomic_json(directory/"report.json", report)
    for i, plan in enumerate(plans):
        atomic_json(directory/"plans"/f"part-{i+1:02d}.json", plan.model_dump())
    store.run(run_id, {"mode": "library_question_planning", "status": "planned" if roots else "no_roots",
                       "fingerprint": fingerprint, "manifest": manifest, "report": report})
    return plans, report


async def run_question_shards(store, settings, pricing, plans, directory, run_prefix, *, gateway_factory=None):
    reports = []
    for i, plan in enumerate(plans):
        run_id = f"{run_prefix}-part{i+1:02d}"
        path = Path(directory)/run_id
        gateway = (gateway_factory or MeteredGateway)(settings, path/"requests", store, run_id,
                                 pricing=pricing, visible_cost_limit_usd=None)
        report = await QuestionExperiment(settings, store, run_id, "pilot", path, gateway,
            plan=plan, pricing=pricing).run_experiment()
        reports.append(report)
        atomic_json(Path(directory)/"campaign-report.json", {"question_runs": reports,
            "planned_roots": sum(len(p.roots) for p in plans), "completed_shards": len(reports),
            "delivery_items": 0, "note": "Pilot only; quality failures and incomplete roots remain in the denominator."})
    return reports
