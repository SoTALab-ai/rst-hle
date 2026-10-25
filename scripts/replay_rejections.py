"""离线回放冻结的入口/质检拒绝；输入和输出必须分离，报告包含私有锚点。"""
import argparse
import hashlib
from pathlib import Path

from diamond_factory.models import digest
from diamond_factory.rejection_replay import replay_rejections
from diamond_factory.runtime import atomic_json


def main():
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quotas", type=Path, default=Path("configs/quotas.v3.json"))
    parser.add_argument("--source-fidelity", choices=("strict", "advisory"), default="strict")
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.evidence.resolve()):
        parser.error("Cannot write into frozen evidence")
    result = replay_rejections(args.evidence, args.manifest_sha256, args.run_id,
                              json.loads(args.quotas.read_text()), source_fidelity=args.source_fidelity)
    result["replay_code_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for root in (Path("src/diamond_factory"), Path("prompts"), Path("scripts"))
        for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in (".py", ".txt")}
    result["quotas_sha256"] = hashlib.sha256(args.quotas.read_bytes()).hexdigest()
    if args.output.exists() and json.loads(args.output.read_text()) != result:
        parser.error("Replay code/evidence changed: choose a new run-id and output")
    atomic_json(args.output, result)
    print(json.dumps({"run_id": args.run_id, "report_sha256": digest(result),
        "failed_materials_replayed": len(result["planning_replays"]),
        "admitted_lines": sum(r["admitted_lines"] for r in result["planning_replays"]),
        "saved_solver_answers_verified": len(result["saved_trials"]), "new_glm_requests": 0,
        "additional_api_cost_usd": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
