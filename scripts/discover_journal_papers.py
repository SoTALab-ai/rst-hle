"""Collect recent top-journal DOI metadata; makes no model calls or database writes."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from diamond_factory.discovery import Journal, discover
from diamond_factory.runtime import atomic_json

p=argparse.ArgumentParser()
p.add_argument('--journals',default='configs/papers/journals.json')
p.add_argument('--since',required=True)
p.add_argument('--until',default=datetime.now(UTC).date().isoformat())
p.add_argument('--per-journal',type=int,default=20)
p.add_argument('--output',default='work/paper-discovery.json')
a=p.parse_args()
journals=[Journal.model_validate(x) for x in json.loads(Path(a.journals).read_text())]
result=discover(journals,since=a.since,until=a.until,per_journal=a.per_journal)
atomic_json(Path(a.output),result)
print(json.dumps({'records':len(result['records']),'failed_journals':len(result['failures']),
                  'output':a.output,'catalog_admissions':0}))
raise SystemExit(1 if result['failures'] else 0)
