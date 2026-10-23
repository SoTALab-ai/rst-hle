"""Price actual gateway receipts, including failed/retried HTTP attempts, exactly once."""
import json
import math
from collections import defaultdict
from pathlib import Path

from .llm import BudgetExceeded, Gateway


def validate_pricing(settings,pricing):
    if pricing.get("currency") != "USD" or not pricing.get("observed_at"):
        raise ValueError("Provide a dated USD gateway price snapshot")
    fx=pricing.get("rmb_reference_exchange_rate")
    if not isinstance(fx,(int,float)) or not math.isfinite(fx) or fx<=0:
        raise ValueError("Positive reference exchange rate required")
    for role in ("generator","auditor","solver","judge"):
        model=getattr(settings,role).model
        rates=pricing.get("rates_per_million",{}).get(model)
        if not rates:
            raise ValueError("Missing verified gateway rates for "+model)
        for tier in [rates]+([rates["above_limit"]] if "input_tier_limit" in rates else []):
            for name in ("input","output","cache_read","cache_write"):
                value=tier.get(name)
                if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value<0:
                    raise ValueError("Invalid per-million rate: "+model+"/"+name)


def price_usage(model, usage, pricing):
    if not all(isinstance(usage.get(k), int) for k in ("prompt_tokens", "completion_tokens")):
        return None
    rates = pricing["rates_per_million"][model]
    prompt, output = usage["prompt_tokens"], usage["completion_tokens"]
    if prompt > rates.get("input_tier_limit", 10**12):
        rates = rates["above_limit"]
    details = usage.get("prompt_tokens_details", {})
    read = details.get("cached_tokens", 0)
    write = details.get("cache_write_tokens", 0)
    return (max(0, prompt-read-write)*rates["input"] + output*rates["output"] +
            read*rates["cache_read"] + write*rates["cache_write"])/1_000_000


def measure_cost(store, run_id, directory, settings, pricing):
    receipts = {}
    request_candidates = {}
    if hasattr(store, "get_request"):
        rows = store.db.execute("SELECT id FROM diamond.model_requests WHERE run_id=%s", (run_id,)).fetchall()
        traces = [store.get_request(r[0]) for r in rows]
    else:
        traces = [json.loads(p.read_text()) for p in Path(directory).glob("*.json") if p.name != "budget.json"]
    for trace in traces:
        request_candidates[trace["request_id"]] = trace.get("salt", "").split(":")[0]
        for attempt in trace.get("attempts", []):
            receipts[(attempt["request_id"], attempt["ordinal"])] = attempt
    # Cancellation may leave a durable HTTP receipt without a final logical response/DB index.
    for path in (Path(directory)/"attempts").rglob("*.json"):
        attempt = json.loads(path.read_text())
        if attempt.get("run_id") == run_id:
            receipts[(attempt["request_id"], attempt["ordinal"])] = attempt
    known = 0.0
    unknown = 0
    roles = defaultdict(lambda: {"http_attempts":0, "known_cost_usd":0.0, "unknown_usage_attempts":0})
    candidates = defaultdict(float)
    for attempt in receipts.values():
        role = attempt["role"]
        row = roles[role]
        row["http_attempts"] += 1
        cost = price_usage(getattr(settings,role).model, attempt.get("usage",{}), pricing)
        if cost is None:
            unknown += 1
            row["unknown_usage_attempts"] += 1
        else:
            known += cost
            row["known_cost_usd"] += cost
            candidates[request_candidates.get(attempt["request_id"],"unattributed")] += cost
    return {"known_cost_usd":known, "rmb_reference":known*pricing["rmb_reference_exchange_rate"],
            "http_attempts":len(receipts), "logical_request_receipts":len(traces),
            "unknown_usage_attempts":unknown, "by_role":dict(roles), "by_candidate_usd":dict(candidates),
            "pricing_snapshot_date":pricing["observed_at"],
            "note":"Returned usage only; unknown/cancelled usage is not zero. Excludes VM and human labor."}


class MeteredGateway(Gateway):
    """A guard on already-returned usage; in-flight and unreturned fees remain unknown."""
    def __init__(self, *args, pricing, visible_cost_limit_usd, **kwargs):
        super().__init__(*args, **kwargs)
        validate_pricing(self.settings,pricing)
        self.pricing = pricing
        self.visible_cost_limit_usd = visible_cost_limit_usd

    async def _fetch(self, role, config, payload, request_id, base, key):
        if self.store:
            rows = self.store.db.execute("SELECT role,usage FROM diamond.model_requests WHERE run_id=%s",
                                         (self.run_id,)).fetchall()
            known = sum(price_usage(getattr(self.settings,r).model,u,self.pricing) or 0 for r,u in rows)
            if known >= self.visible_cost_limit_usd:
                raise BudgetExceeded("Visible returned-usage budget reached; unknown/in-flight fees remain separate")
        return await super()._fetch(role,config,payload,request_id,base,key)
