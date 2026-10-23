"""Select a reproducible, small stratified calibration sample from a local Feishu requirements export."""
import argparse
import hashlib
import json
import random
import re
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("source", help="Local requirements Markdown export; never upload the resulting benchmark data")
parser.add_argument("--output", default="work/private/calibration/selection24.jsonl")
parser.add_argument("--per-category", type=int, default=3)
parser.add_argument("--seed", default="20261023")
args = parser.parse_args()
source = Path(args.source)
text = source.read_text()
blocks, position = [], 0
decoder = json.JSONDecoder()
while match := re.search(r"(?m)^```json\s*\n", text[position:]):
    start = position + match.end()
    value, consumed = decoder.raw_decode(text[start:])
    blocks.append(value)
    position = start + consumed
grouped = defaultdict(list)
for index, block in enumerate(blocks):
    for j, example in enumerate(block["examples"]):
        grouped[block["category"]].append({
            "id": f"doc-hle-{index:02d}-{j}", "category": block["category"], "subtopic": block["subtopic"],
            "question": example["question"], "answer": example["answer"],
            "answer_type": "multipleChoice" if "Answer Choices:" in example["question"] else "exactMatch",
            "origin": "feishu-demand-HLE-Diamond-original-example", "reference_only": True})
selection = []
for category, rows in sorted(grouped.items()):
    random.Random(args.seed + ":" + category).shuffle(rows)
    seen = set()
    for row in rows:
        if row["subtopic"] in seen:
            continue
        selection.append(row)
        seen.add(row["subtopic"])
        if len(seen) == args.per_category:
            break
# Interleave categories so an eight-pair checkpoint covers all eight domains.
chosen = [[row for row in selection if row["category"] == category] for category in sorted(grouped)]
selection = [row for batch in zip_longest(*chosen) for row in batch if row is not None]
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selection))
path.with_suffix(".provenance.json").write_text(json.dumps({
    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "pool_size": sum(map(len, grouped.values())),
    "seed": args.seed, "per_category": args.per_category, "count": len(selection),
    "note": "Only the document's original HLE-Diamond examples; not a random sample of all 728 text questions."},
    ensure_ascii=False, indent=2))
print(json.dumps({"selected": len(selection), "categories": len(grouped), "output": str(path)}))
