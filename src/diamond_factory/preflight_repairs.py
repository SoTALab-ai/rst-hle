"""前置验算按结构化结论归因；未知执行不冒充科学反例。"""
from pathlib import Path

from .models import PreflightRepairAdvice, digest


def certificate_repair_protocol(settings, prompts):
    return digest({"max_repairs": settings.certificate_max_repairs,
        "repair_prompt": prompts["consistency_certificate_repair"], "code": Path(__file__).read_text()})


def classify_preflight(review, certificate, substitution, *, basis="self_verified"):
    cases = [r.name for r in substitution.results if r.verdict != "pass"]
    common = {"failed_cases": cases, "uncovered_outputs": substitution.uncovered_outputs}
    if not review.passed(basis=basis):
        return PreflightRepairAdvice(category="scientific_review", owner="author",
            findings=review.failures(basis=basis), instruction="Repair the specified proof, premise or question defect "
                "before verifier entry; recheck dependent steps. Preserve locked gold and target. "
                "If the locked result is false or the defect cannot be resolved, reject this construction.", **common)
    if not certificate.faithful_translation:
        return PreflightRepairAdvice(category="certificate_translation", owner="certificate",
            findings=[certificate.reason], instruction="Repair only the certificate's translation and anchors. "
                "Do not modify the question, gold or proof. If faithful encoding is unavailable, reject.", **common)
    if substitution.verdict == "fail":
        return PreflightRepairAdvice(category="gold_counterexample", owner="author",
            findings=[r.reason+" lhs="+r.lhs_value+" rhs="+r.rhs_value
                      for r in substitution.results if r.verdict == "fail"],
            instruction="Investigate these faithfully translated failing original constraints in the "
                "proof/question before verifier entry. Do not repair a faithful failing check to fit the gold. "
                "A false locked golden requires rejection and a separately identified new construction.", **common)
    if not substitution.passed():
        return PreflightRepairAdvice(category="certificate_execution", owner="certificate",
            findings=[substitution.reason, *[r.reason for r in substitution.results if r.verdict == "unsupported"]],
            instruction="Repair only unsupported arithmetic, inadmissible test points, anchors or missing "
                "output coverage in the certificate; do not change question/gold/proof or weaken an original "
                "constraint. Preserve genuine counterexamples. If coverage stays unresolved, reject.", **common)
    return None
