"""显式安装独立读取门禁，达到材料上限后排空在途调用并停止冻结的旧读取器。"""
import argparse
import json
import os
from pathlib import Path

from diamond_factory.postgres import PostgresStore
from diamond_factory.reader_control import ReaderLimit, install_control, supervise_limit

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('action', choices=['init', 'run'])
p.add_argument('--objects', required=True)
p.add_argument('--policy')
p.add_argument('--directory')
a = p.parse_args()
store = PostgresStore(os.environ['DIAMOND_DATABASE_URL'], a.objects)
store.objects.compress_json = True
try:
    if a.action == 'init':
        install_control(store)
    else:
        if not a.policy or not a.directory:
            p.error('run requires --policy and --directory')
        print(json.dumps(supervise_limit(store, ReaderLimit.model_validate_json(Path(a.policy).read_text()),
                                        a.directory), ensure_ascii=False))
finally:
    store.db.close()
