"""Select reference examples locally; never upload benchmark text or labels to public Git."""
import argparse
import hashlib
import json
from pathlib import Path

from diamond_factory.calibration import BenchmarkQuestion
from diamond_factory.reference_selection import select_examples

parser = argparse.ArgumentParser()
parser.add_argument("source", help="Local requirements Markdown export")
parser.add_argument("--output", default="work/private/calibration/selection24.jsonl")
parser.add_argument("--per-category", type=int, default=3)
parser.add_argument("--seed", default="20261023")
parser.add_argument("--categories", nargs="+")
parser.add_argument("--exclude", action="append", default=[], help="Prior reference manifest; repeatable")
parser.add_argument("--new-subtopics", action="store_true")
args = parser.parse_args()
source = Path(args.source)
excluded = [BenchmarkQuestion.model_validate_json(line) for file in args.exclude
            for line in Path(file).read_text().splitlines() if line.strip()]
selection, provenance = select_examples(source.read_text(), args.per_category, args.seed,
                                        args.categories, excluded, args.new_subtopics)
provenance.update(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  exclusion_files={file: hashlib.sha256(Path(file).read_bytes()).hexdigest() for file in args.exclude})
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("".join(q.model_dump_json() + "\n" for q in selection))
path.with_suffix(".provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2))
print(json.dumps({"selected": len(selection), "categories": len(provenance["categories"]), "output": str(path)}))
