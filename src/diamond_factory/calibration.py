"""Stratified reference-benchmark calibration. Never admits source benchmark questions to delivery."""
import asyncio
import json
from collections import Counter
from pathlib import Path

from .llm import Gateway
from .models import Judgment, StrictModel, digest


class BenchmarkQuestion(StrictModel):
    id: str
    category: str
    subtopic: str
    question: str
    answer: str
    answer_type: str
    origin: str
    reference_only: bool = True


def summarize(questions, trials, errors):
    rows = []
    for q in questions:
        found = [x for x in trials if x["question_id"] == q.id]
        if len({x["sample_id"] for x in found}) != len(found):
            raise ValueError("Duplicate calibration sample id")
        if len({x["solver_request"] for x in found}) != len(found):
            raise ValueError("Reused solver response in calibration")
        if len({x["judge_request"] for x in found}) != len(found):
            raise ValueError("Reused judge response in calibration")
        complete = len(found) == 16 and {x["sample_id"] for x in found} == set(range(16))
        n = sum(x["correct"] for x in found)
        band = ("zero_correct" if n == 0 else "in_band" if n <= 8 else "too_easy") if complete else "incomplete"
        rows.append({"id": q.id, "category": q.category, "subtopic": q.subtopic,
                     "samples": len(found), "correct_count": n if complete else None,
                     "observed_correct": n,
                     "band": band, "reference_only": True})
    by_category = []
    for category in sorted({q.category for q in questions}):
        selected = [r for r in rows if r["category"] == category]
        bands = Counter(r["band"] for r in selected)
        completed = len(selected) - bands["incomplete"]
        by_category.append({"category": category, "sampled_questions": len(selected), "complete": completed,
                            "zero_correct": bands["zero_correct"], "in_band": bands["in_band"],
                            "too_easy": bands["too_easy"], "incomplete": bands["incomplete"],
                            "in_band_fraction": bands["in_band"] / completed if completed else None})
    return {"reference_calibration_only": True, "delivery_items": 0, "items": rows,
            "by_category": by_category, "errors": errors,
            "totals": dict(Counter(r["band"] for r in rows))}


class Calibration:
    def __init__(self, settings, store, run_id, directory, gateway=None, reuse_run=None):
        self.settings, self.store = settings, store
        self.run_id, self.directory = run_id, Path(directory)
        self.gateway = gateway or Gateway(settings, self.directory / "requests", store=store, run_id=run_id)
        self.solver_system = Path(settings.prompts_dir, "solver_hle_system.txt").read_text().strip()
        self.judge_prompt = Path(settings.prompts_dir, "judge.txt").read_text()
        self.reuse_run = reuse_run

    async def run(self, questions, max_new_samples=None):
        if self.settings.samples != 16:
            raise ValueError("Difficulty calibration requires 16 samples")
        if len({q.id for q in questions}) != len(questions) or not questions:
            raise ValueError("Provide nonempty benchmark questions with unique ids")
        if any(not q.reference_only for q in questions):
            raise ValueError("Benchmark data must remain reference_only")
        if max_new_samples is not None and max_new_samples < 1:
            raise ValueError("max_new_samples must be positive")
        previous = self.store.runs().get(self.run_id)
        reuse_run = self.reuse_run or (previous.get("reuse_run") if previous else None)
        namespace = self.run_id
        if reuse_run:
            source = self.store.runs().get(reuse_run)
            if reuse_run == self.run_id or not source or source.get("mode") != "reference_calibration":
                raise ValueError("Reuse requires a different existing reference calibration run")
            if (source["settings"] != self.settings.model_dump() or
                    source["solver_system_sha256"] != digest(self.solver_system) or
                    source["judge_prompt_sha256"] != digest(self.judge_prompt)):
                raise ValueError("Reuse requires identical model, time, sampling and prompt settings")
            namespace = source.get("request_namespace", reuse_run)
        fingerprint = digest({"questions": [q.model_dump() for q in questions],
                              "settings": self.settings.model_dump(), "solver_system": self.solver_system,
                              "request_namespace": namespace,
                              "judge_prompt": self.judge_prompt, "calibration_code": Path(__file__).read_text(),
                              "gateway_code": Path(__file__).with_name("llm.py").read_text()})
        if previous and previous["fingerprint"] != fingerprint:
            raise ValueError("Calibration inputs/config/code changed; use a new run-id")
        trial_rows = list(previous.get("trial_records", [])) if previous else []
        failures = {(e["question_id"], e["sample_id"]): e for e in previous.get("errors", [])} if previous else {}
        meta = {"mode": "reference_calibration", "status": "running", "fingerprint": fingerprint,
                "settings": self.settings.model_dump(), "question_count": len(questions),
                "source_origins": sorted({q.origin for q in questions}),
                "question_manifest_sha256": digest([q.model_dump() for q in questions]),
                "solver_system_sha256": digest(self.solver_system), "judge_prompt_sha256": digest(self.judge_prompt),
                "reuse_run": reuse_run, "request_namespace": namespace,
                "reference_only": True, "trial_records": trial_rows}
        self.store.run(self.run_id, meta)
        done = {(x["question_id"], x["sample_id"]) for x in trial_rows}
        queue = asyncio.Queue()
        # Round-robin samples across domains, so early progress covers the entire small pilot.
        for sample_id in range(16):
            for question in questions:
                if (question.id, sample_id) not in done:
                    if max_new_samples is not None and queue.qsize() >= max_new_samples:
                        continue
                    queue.put_nowait((question, sample_id))

        async def worker():
            while not queue.empty():
                q, sample_id = queue.get_nowait()
                try:
                    response, sid, smodel = await self.gateway.call(
                        "solver", q.question, f"{namespace}:{q.id}:solver:{sample_id}",
                        system_prompt=self.solver_system)
                    judgment, jid, jmodel = await self.gateway.call(
                        "judge", self.judge_prompt.format(question=q.question, correct_answer=q.answer,
                                                         response=response),
                        f"{namespace}:{q.id}:judge:{sample_id}", Judgment)
                    record = {"question_id": q.id, "sample_id": sample_id, "correct": judgment.correct == "yes",
                              "solver_request": sid, "judge_request": jid,
                              "solver_actual_model": smodel, "judge_actual_model": jmodel,
                              "judgment": judgment.model_dump()}
                    trial_rows.append(record)
                    failures.pop((q.id, sample_id), None)
                    meta["trial_records"] = trial_rows
                    meta["errors"] = list(failures.values())
                    self.store.run(self.run_id, meta)
                    progress = {"event": "calibration_sample", "question_id": q.id, "category": q.category,
                                "sample_id": sample_id, "correct": judgment.correct, "done": len(trial_rows),
                                "total": len(questions) * 16}
                    if hasattr(self.store, "event"):
                        self.store.event(self.run_id, progress)
                    print(json.dumps(progress, ensure_ascii=False), flush=True)
                except (RuntimeError, ValueError, KeyError, TypeError) as error:
                    failure = {"question_id": q.id, "sample_id": sample_id,
                               "type": type(error).__name__, "message": str(error)[:180],
                               "request_id": getattr(error, "request_id", None)}
                    failures[(q.id, sample_id)] = failure
                    meta["errors"] = list(failures.values())
                    self.store.run(self.run_id, meta)
                    if hasattr(self.store, "event"):
                        self.store.event(self.run_id, {"event": "calibration_error", **failure})
                    print(json.dumps({"event": "calibration_error", **failure}, ensure_ascii=False), flush=True)
                finally:
                    queue.task_done()
        try:
            await asyncio.gather(*(worker() for _ in range(self.settings.concurrency)))
        except BaseException:
            meta["status"] = "interrupted"
            meta["errors"] = list(failures.values())
            self.store.run(self.run_id, meta)
            raise
        meta["status"] = ("completed" if len(trial_rows) == len(questions) * 16 else
                          "incomplete" if failures else "checkpointed")
        meta["errors"] = list(failures.values())
        self.store.run(self.run_id, meta)
        result = summarize(questions, trial_rows, meta["errors"])
        result.update({"run_id": self.run_id, "settings": self.settings.model_dump(),
                       "status": meta["status"], "source_origins": meta["source_origins"], "fingerprint": fingerprint})
        return result
