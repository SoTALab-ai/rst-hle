"""Small execution primitives shared by batch stages."""
import asyncio
import json
from pathlib import Path


def atomic_json(path: Path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2))
    temporary.replace(path)


async def gather_cancel_on_error(*coroutines):
    tasks = [asyncio.create_task(c) for c in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
