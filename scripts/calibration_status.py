"""Read-only calibration progress and usage-based gateway cost estimates; no model calls."""
import argparse
import json
import statistics
from pathlib import Path

from diamond_factory.calibration import BenchmarkQuestion, summarize
from diamond_factory.models import digest
from diamond_factory.postgres import PostgresStore, open_store

parser = argparse.ArgumentParser()
parser.add_argument("input", help="The exact reference manifest used by this run")
parser.add_argument("--run-id", required=True)
parser.add_argument("--rates", help="Local gateway pricing snapshot JSON (never assume public list pricing)")
parser.add_argument("--output")
args = parser.parse_args()
store = open_store("work/factory.sqlite")
meta = store.runs()[args.run_id]
questions = [BenchmarkQuestion.model_validate_json(line) for line in Path(args.input).read_text().splitlines()
             if line.strip()]
if digest([q.model_dump() for q in questions]) != meta["question_manifest_sha256"]:
    raise ValueError("Input manifest differs from the recorded calibration manifest")
trials, errors = meta.get("trial_records", []), meta.get("errors", [])
result = summarize(questions, trials, errors)
result.update(run_id=args.run_id, status=meta["status"], settings=meta["settings"],
              completed_samples=len(trials), expected_samples=len(questions) * 16,
              failed_samples=len(errors), reuse_run=meta.get("reuse_run"), fingerprint=meta["fingerprint"])
if isinstance(store, PostgresStore):
    ids = {t[role + "_request"] for t in trials for role in ("solver", "judge")}
    ids.update(e["request_id"] for e in errors if e.get("request_id"))
    receipts = store.db.execute("""SELECT object_key,object_sha256,object_size FROM diamond.model_requests
        WHERE run_id=%s OR id=ANY(%s)""", (args.run_id, list(ids))).fetchall()
    traces = [store.objects.get(*r) for r in receipts]
    result["unique_request_receipts"] = len(traces)
    result["new_http_attempts"] = store.db.execute("SELECT http_attempts FROM diamond.runs WHERE id=%s",
                                                    (args.run_id,)).fetchone()[0]
    own_receipts = store.db.execute("SELECT count(*) FROM diamond.model_requests WHERE run_id=%s",
                                   (args.run_id,)).fetchone()[0]
    result["http_attempts_without_receipts"] = max(0, result["new_http_attempts"] - own_receipts)
    result["solver_client_token_caps"] = sum(
        "max_tokens" in t["request"] or "max_completion_tokens" in t["request"]
        for t in traces if t["role"] == "solver")
    successful = [t["elapsed_seconds"] for t in traces
                  if t["role"] == "solver" and not t.get("response_error") and "elapsed_seconds" in t]
    result["successful_solver_latency_seconds"] = ({"min": min(successful), "median": statistics.median(successful),
                                                    "max": max(successful)} if successful else None)
    roles = {}
    missing = 0
    estimate = 0.0
    prices = json.loads(Path(args.rates).read_text()) if args.rates else None
    for t in traces:
        usage = t.get("usage", {})
        if "prompt_tokens" not in usage or "completion_tokens" not in usage:
            missing += 1
            continue
        row = roles.setdefault(t["role"], {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})
        prompt, output = usage["prompt_tokens"], usage["completion_tokens"]
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        write = usage.get("prompt_tokens_details", {}).get("cache_write_tokens", 0)
        row["prompt_tokens"] += prompt
        row["completion_tokens"] += output
        row["cached_tokens"] += cached
        if prices:
            rates = prices["rates_per_million"][t["request"]["model"]]
            if write and "cache_write" not in rates:
                raise ValueError("Cache-write rate required to estimate this trace")
            estimate += (max(0, prompt - cached - write) * rates["input"] + output * rates["output"] +
                         cached * rates["cache_read"] + write * rates.get("cache_write", 0)) / 1_000_000
    result["returned_usage"] = roles
    result["receipts_with_unknown_usage"] = missing
    if prices:
        result["estimated_cost_from_returned_usage"] = {
            "usd": estimate, "rmb_reference": estimate * prices["rmb_reference_exchange_rate"],
            "pricing_snapshot_date": prices["observed_at"],
            "note": "Estimate of unique returned usage, including reused and truncated responses; not the final invoice. "
                    "Unreturned/cancelled usage and untraced retry charges are unknown. "
                    "Provider usage.cost may be an upstream cost and is not used as the gateway charge."}
text = json.dumps(result, ensure_ascii=False, indent=2)
if args.output:
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
print(text)
