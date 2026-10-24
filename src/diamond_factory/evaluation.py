"""Bounded transport-format recovery; never resample a solver or retry a valid negative grade."""
import json

from .llm import ModelResponseError
from .models import Judgment


async def judge_answer(gateway, prompt, salt, schema_repairs=1, *, system_prompt=None):
    for repair in range(schema_repairs + 1):
        attempt_salt = salt if repair == 0 else salt + ":schema-repair:" + str(repair)
        try:
            options = {"system_prompt": system_prompt} if system_prompt else {}
            return await gateway.call("judge", prompt, attempt_salt, Judgment, **options)
        except ModelResponseError as error:
            if error.reason != "invalid_schema" or repair == schema_repairs:
                raise
            event = {"event": "judge_schema_repair", "failed_request_id": error.request_id,
                     "repair": repair + 1, "solver_resampled": False}
            if getattr(gateway, "store", None) is not None:
                gateway.store.event(gateway.run_id, event)
            print(json.dumps(event), flush=True)
