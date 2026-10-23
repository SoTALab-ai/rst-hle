"""Reproducible reference-only sampling from the user's local requirements export."""
import json
import random
import re
from collections import defaultdict
from itertools import zip_longest

from .calibration import BenchmarkQuestion
from .gates import normalize
from .models import digest


def select_examples(text, per_category=3, seed="20261023", categories=None, exclude=(), new_subtopics=False):
    if per_category < 1:
        raise ValueError("per_category must be positive")
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
            grouped[block["category"]].append(BenchmarkQuestion(
                id=f"doc-hle-{index:02d}-{j}", category=block["category"], subtopic=block["subtopic"],
                question=example["question"], answer=example["answer"],
                answer_type="multipleChoice" if "Answer Choices:" in example["question"] else "exactMatch",
                origin="feishu-demand-HLE-Diamond-original-example", reference_only=True))
    wanted = sorted(categories or grouped)
    if not wanted or set(wanted) - set(grouped):
        raise ValueError("Unknown or empty reference categories")
    excluded_ids = {q.id for q in exclude}
    excluded_texts = {normalize(q.question) for q in exclude}
    excluded_topics = {(q.category, q.subtopic) for q in exclude} if new_subtopics else set()
    chosen = []
    for category in wanted:
        rows = list(grouped[category])
        random.Random(seed + ":" + category).shuffle(rows)
        seen, picked = set(), []
        for row in rows:
            if (row.id in excluded_ids or normalize(row.question) in excluded_texts or
                    (row.category, row.subtopic) in excluded_topics or row.subtopic in seen):
                continue
            picked.append(row)
            seen.add(row.subtopic)
            if len(picked) == per_category:
                break
        if len(picked) != per_category:
            raise ValueError(f"Insufficient unseen subtopics for {category}: {len(picked)}/{per_category}")
        chosen.append(picked)
    selection = [q for batch in zip_longest(*chosen) for q in batch if q is not None]
    if len({normalize(q.question) for q in selection}) != len(selection):
        raise ValueError("Duplicate reference question text across selected categories")
    provenance = {"pool_size": sum(map(len, grouped.values())), "seed": seed, "per_category": per_category,
                  "categories": wanted, "excluded_ids": sorted(excluded_ids), "new_subtopics": new_subtopics,
                  "count": len(selection), "selection_sha256": digest([q.model_dump() for q in selection]),
                  "note": "Reference discovery sample; not an unbiased estimate of all 728 Diamond text questions."}
    return selection, provenance
