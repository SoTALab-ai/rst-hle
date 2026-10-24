import json

import pytest

from diamond_factory.models import digest
from diamond_factory.paper_acquisition import balanced_candidates, resolve_one


def test_candidates_rotate_journals_and_exclude_crossref_updates():
    rows = [{"domain": d, "journal_id": j, "doi": f"{j}/{i}", "updates": []}
            for d, j in (("Math", "a"), ("Math", "b"), ("Physics", "c")) for i in range(3)]
    rows.append({"domain": "Math", "journal_id": "a", "doi": "correction", "updates": ["doi"]})
    selected = balanced_candidates(rows)
    assert [r["journal_id"] for r in selected[:6]] == ["a", "b", "c", "a", "b", "c"]
    assert len(selected) == 9 and all(r["doi"] != "correction" for r in selected)


def test_resolution_checkpoint_reuse_and_corruption_refusal(tmp_path):
    row = {"doi": "10.1000/fixture", "journal_id": "annals"}
    body = {"doi": row["doi"], "locations": [], "failures": ["HTTPStatusError"]}
    path = tmp_path/(digest(row["doi"])+".json")
    path.write_text(json.dumps({"body": body, "seal": digest(body)}))
    assert resolve_one(row, tmp_path) == body
    path.write_text(json.dumps({"body": body, "seal": "invalid"}))
    with pytest.raises(ValueError, match="Corrupt"):
        resolve_one(row, tmp_path)
