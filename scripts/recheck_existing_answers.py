"""在已初始化的独立库里重新判分，不修改历史题面、金标或solver响应。"""
import argparse
import asyncio
import json
from pathlib import Path

from diamond_factory.config import load
from diamond_factory.experiment import ExperimentPlan
from diamond_factory.models import Candidate
from diamond_factory.postgres import open_store
from diamond_factory.rechecking import ExistingAnswerRecheck, RecheckOnlyGateway

parser=argparse.ArgumentParser()
parser.add_argument('--config',required=True)
parser.add_argument('--plan',required=True)
parser.add_argument('--rates',required=True)
parser.add_argument('--run-id',required=True)
parser.add_argument('--source-candidate',required=True)
parser.add_argument('--source-sha256',required=True)
args=parser.parse_args()
if not args.run_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in args.run_id):
    raise ValueError('Invalid run-id')
settings=load(args.config)
plan=ExperimentPlan.model_validate_json(Path(args.plan).read_text())
pricing=json.loads(Path(args.rates).read_text())
source=Candidate.model_validate_json(Path(args.source_candidate).read_text())
store=open_store('work/factory.sqlite')
directory=Path('work/runs')/args.run_id
gateway=RecheckOnlyGateway(settings,directory/'requests',store,args.run_id,pricing=pricing,
                          visible_cost_limit_usd=plan.visible_cost_limit_usd)
with store.lock():
    result=asyncio.run(ExistingAnswerRecheck(settings,store,args.run_id,'pilot',directory,gateway,
        plan=plan,pricing=pricing).recheck(source,expected_source_candidate_sha256=args.source_sha256))
print(json.dumps(result.model_dump(),ensure_ascii=False))
