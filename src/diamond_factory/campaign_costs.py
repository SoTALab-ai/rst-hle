"""按数据库/原始run身份汇总实付收据，缓存复用不重复计费。"""
from pathlib import Path

from .config import load
from .costs import measure_cost
from .models import StrictModel


class CostStage(StrictModel):
    scope: str
    stage: str
    run_id: str
    directory: str
    config: str
    pricing: dict


def campaign_costs(stores, stages):
    seen, rows = set(), []
    for stage in stages:
        identity = (stage.scope, stage.run_id)
        if identity in seen:
            raise ValueError('Duplicate original billing run in campaign')
        seen.add(identity)
        store = stores[stage.scope]
        cost = measure_cost(store, stage.run_id, Path(stage.directory)/'requests',
                            load(stage.config), stage.pricing)
        rows.append({'scope': stage.scope, 'stage': stage.stage, 'run_id': stage.run_id,
                     'state': store.runs().get(stage.run_id, {}).get('status', 'not_started'), **cost})
    return {'stages': rows, 'known_cost_usd': sum(r['known_cost_usd'] for r in rows),
        'rmb_reference': sum(r['rmb_reference'] for r in rows),
        'http_attempts': sum(r['http_attempts'] for r in rows),
        'unknown_usage_attempts': sum(r['unknown_usage_attempts'] for r in rows),
        'unsettled_reserved_attempts': sum(r['unsettled_reserved_attempts'] for r in rows),
        'infrastructure_cost_usd': None,
        'note': '按返回usage与各批冻结价格估算；非网关账单。含拒绝、失败和重试；未知非零。'
                '缓存计入原支付run，不在复用run重复累计。VM/磁盘/网络尚未对账，未并入合计。'}
