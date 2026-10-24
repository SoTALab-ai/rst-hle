"""只读查询多个个人数据库的实时费用；不调用模型、不重跑评测。"""
import argparse
import json
import os
from pathlib import Path

from diamond_factory.campaign_costs import CostStage, campaign_costs
from diamond_factory.postgres import PostgresStore
from diamond_factory.runtime import atomic_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    manifest = json.loads(Path(a.manifest).read_text())
    stores = {}
    try:
        for name, config in manifest['scopes'].items():
            store = PostgresStore(os.environ[config['database_url_env']], config['objects'])
            store.db.execute('SET default_transaction_read_only=on')
            stores[name] = store
        report = campaign_costs(stores, [CostStage.model_validate(r) for r in manifest['stages']])
        atomic_json(Path(a.output), report)
        print(json.dumps(report, ensure_ascii=False))
    finally:
        for store in stores.values():
            store.db.close()


if __name__ == '__main__':
    main()
