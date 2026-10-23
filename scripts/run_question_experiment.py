import argparse
import asyncio
import json
from pathlib import Path

from diamond_factory.config import load
from diamond_factory.costs import MeteredGateway
from diamond_factory.experiment import ExperimentPlan, QuestionExperiment
from diamond_factory.postgres import open_store

parser = argparse.ArgumentParser()
parser.add_argument("--config",default="configs/math-physics-experiment.toml")
parser.add_argument("--plan",required=True)
parser.add_argument("--rates",required=True)
parser.add_argument("--run-id",required=True)
args = parser.parse_args()
if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
    raise ValueError("Invalid run-id")
settings = load(args.config)
plan = ExperimentPlan.model_validate_json(Path(args.plan).read_text())
pricing = json.loads(Path(args.rates).read_text())
store = open_store("work/factory.sqlite")
directory = Path("work/runs")/args.run_id
gateway = MeteredGateway(settings,directory/"requests",store,args.run_id,
                         pricing=pricing,visible_cost_limit_usd=plan.visible_cost_limit_usd)
with store.lock():
    result = asyncio.run(QuestionExperiment(settings,store,args.run_id,"pilot",directory,
                                            gateway,plan=plan,pricing=pricing).run_experiment())
print(json.dumps({k:result[k] for k in ["run_id","generated_versions","roots_with_retained_question","cost"]},
                 ensure_ascii=False))
