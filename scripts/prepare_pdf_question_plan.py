"""重放材料准入后冻结出题计划；被材料拒绝的线保留在总分母中。"""
import argparse
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter

from diamond_factory.experiment import ExperimentPlan, RootPlan
from diamond_factory.models import StrictModel, digest
from diamond_factory.pdf_library import PdfReceipt
from diamond_factory.pdf_review import SupplementCheck, admit_material
from diamond_factory.runtime import atomic_json


class PdfTarget(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    doi: str
    category: Literal["Math", "Physics"]
    subtopic: str


def prepare(directory, targets, checks):
    directory = Path(directory)
    manifest = json.loads((directory / "review-manifest.json").read_text())
    report = json.loads((directory / "review-report.json").read_text())
    if manifest["parser"] != "mineru_precision" or report["planned"] != len(targets):
        raise ValueError("A completed precision material review must cover the complete target cohort")
    receipts = [PdfReceipt.model_validate_json(p.read_text()).model_dump()
                for p in (directory / "input").glob("*/receipt.json")]
    by_doi = {r["paper"]["doi"]: r for r in receipts}
    if (len({t.name for t in targets}) != len(targets) or len({t.doi for t in targets}) != len(targets)
            or set(by_doi) != {t.doi for t in targets} or set(checks) != set(by_doi)):
        raise ValueError("Planned paper identities or supplementary reviews do not match")
    sources, roots, cohort = [], [], []
    for target in targets:
        receipt = by_doi[target.doi]
        row = json.loads((directory / "papers" / (receipt["plan_sha256"] + ".json")).read_text())
        item = {**target.model_dump(), "collection_plan_sha256": receipt["plan_sha256"],
                "title": receipt["paper"]["title"], "state": "material_rejected",
                "documents": [{k: d[k] for k in ("pdf_path", "pdf_sha256", "pages", "version", "provenance_url")}
                              for d in receipt["documents"]]}
        if row["ready"]:
            source = admit_material(directory, row, checks[target.doi])
            focus = ("Use ONLY the approved PDF body/formula/table material. The proposed focus is an "
                "untrusted locator, not an additional premise. First lock an existing exact source result "
                "and its golden answer, then reconstruct the complete derivation before making a question. "
                "Hide the hardest necessary inference and equivalent hints. Ask for a uniquely determined "
                "short exact expression, object or case classification; do not ask for a proof while giving "
                "only the theorem statement as gold. Preserve every requested output, normalization and "
                "physical domain. Scientific figures, plots and photographs are unavailable: reject any "
                "target requiring figure values or external reference-paper content. Do not invent a toy "
                "or new physical regime.\nSelected source result: " + row["selection"]["authoring_focus"])
            roots.append(RootPlan(name=target.name, source_id=source.id, category=target.category,
                                  subtopic=target.subtopic, focus=focus))
            sources.append(source)
            item.update(state="eligible_for_authoring", source_id=source.id, source_sha256=source.sha256,
                        reviewed_pages=row["reviewed_pages"], selection=row["selection"])
        else:
            item.update(error_type=row.get("error_type"), reason=row.get("error"))
        cohort.append(item)
    plan = ExperimentPlan(roots=roots, max_revisions=2, root_workers=4, visible_cost_limit_usd=None) if roots else None
    rollup = {"planned_independent_roots": len(targets), "eligible_roots": len(roots),
        "material_rejected_roots": len(targets) - len(roots), "cohort": cohort,
        "material_run_id": report["run_id"],
        "material_manifest_sha256": hashlib.sha256((directory / "review-manifest.json").read_bytes()).hexdigest(),
        "material_report_sha256": hashlib.sha256((directory / "review-report.json").read_bytes()).hexdigest(),
        "plan_sha256": digest(plan.model_dump()) if plan else None,
        "note": "Material rejects remain in the complete cohort denominator; eligible is not a successful question."}
    return plan, sources, rollup


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--directory", required=True)
    p.add_argument("--targets", default="configs/papers/pdf20-targets.json")
    p.add_argument("--supplements", default="configs/papers/pdf20-supplements.json")
    p.add_argument("--output", default="work/private/pdf20-prepared")
    args = p.parse_args()
    targets = TypeAdapter(list[PdfTarget]).validate_json(Path(args.targets).read_text())
    checks = TypeAdapter(list[SupplementCheck]).validate_json(Path(args.supplements).read_text())
    plan, sources, cohort = prepare(args.directory, targets, {c.doi: c for c in checks})
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "cohort.json", cohort)
    atomic_json(output / "plan.json", plan.model_dump() if plan else None)
    (output / "sources.jsonl").write_text("\n".join(s.model_dump_json() for s in sources) + "\n")
    print(json.dumps({k: cohort[k] for k in ("planned_independent_roots", "eligible_roots", "material_rejected_roots")}))
