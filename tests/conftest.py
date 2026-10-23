import json
from pathlib import Path

import pytest

from diamond_factory.config import load
from diamond_factory.models import Audit, Candidate, Evidence, Judgment, Question, Solution, Source, Trial


@pytest.fixture
def settings():
    return load("configs/pilot.toml")


@pytest.fixture
def source():
    return Source(id="test-theorem", title="Synthetic graph exercise, not a paper", uri="synthetic://test",
                  kind="synthetic", text="Every tree with n vertices has exactly n minus one edges. "
                  "A tree is a connected graph with no cycle. This source is a synthetic test fixture, "
                  "and does not demonstrate HLE-level difficulty.")


@pytest.fixture
def candidate(source):
    ev = Evidence(source_id=source.id, locator="sentence 1",
                  quote="Every tree with n vertices has exactly n minus one edges.",
                  supports="The edge count follows from the tree theorem.")
    item = Question(question="A connected undirected graph without cycles has 37 vertices. "
                             "How many edges does it have? Return one integer.",
                    answer="36", answer_type="exactMatch", category="Math", subtopic="组合与图论",
                    material_key="tree-edge-count-37", required_outputs=["edge count"],
                    assumptions=["finite undirected simple tree"], evidence=[ev],
                    rationale="A connected graph without cycles is a tree. A tree with n vertices has n-1 edges, "
                              "so the answer is 37-1=36.")
    solution = Solution(topic="tree edge count", assumptions=item.assumptions,
                        derivation=["The graph is a tree", "A tree with 37 vertices has 37-1=36 edges"],
                        answer="36", evidence=[ev], reasoning_delta="Use the defining properties to infer the edge count.")
    audit = Audit(valid=True, self_contained=True, uniquely_answerable=True, answer_supported=True,
                  substantive_delta=True, category_correct=True, no_shortcut=True,
                  reason="All tree premises and the exact answer have been checked.")
    return Candidate(id="candidate1", parent_id=None, root_id="candidate1", generation=0,
                     operator="bootstrap", source_ids=[source.id], solution=solution, item=item,
                     audit=audit, status="quality_seed", run_id="test")


def trials(count):
    return [Trial(sample_id=i, response="36" if i < count else "37", solver_request=f"s{i}",
                  judge_request=f"j{i}", solver_actual_model="glm-5.2", judge_actual_model="gpt-6-luna",
                  judgment=Judgment(extracted_final_answer="36" if i < count else "37", reasoning="Matched gold",
                                    correct="yes" if i < count else "no", confidence=100, strict=True))
            for i in range(16)]


@pytest.fixture
def quotas():
    return json.loads(Path("configs/quotas.v3.json").read_text())
