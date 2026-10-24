"""在个人VM按冻结期刊候选补齐论文PDF；不启动题目生成。"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from diamond_factory.config import load
from diamond_factory.paper_acquisition import acquire_library
from diamond_factory.postgres import PostgresStore

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--discovery', required=True)
p.add_argument('--rates', required=True)
p.add_argument('--directory', required=True)
p.add_argument('--run-id', required=True)
p.add_argument('--objects', required=True)
p.add_argument('--config', default='configs/paper-reader.toml')
p.add_argument('--reuse-root')
p.add_argument('--target-per-domain', type=int, default=100)
a = p.parse_args()
if a.target_per_domain < 1:
    raise ValueError('Target must be positive')
store = PostgresStore(os.environ['DIAMOND_DATABASE_URL'], a.objects)
store.objects.compress_json = True
try:
    with store.lock():
        result = asyncio.run(acquire_library(store, json.loads(Path(a.discovery).read_text()), load(a.config),
            json.loads(Path(a.rates).read_text()), Path(a.directory), a.run_id,
            reuse_root=a.reuse_root, target_per_domain=a.target_per_domain))
        print(json.dumps(result, ensure_ascii=False))
finally:
    store.db.close()
