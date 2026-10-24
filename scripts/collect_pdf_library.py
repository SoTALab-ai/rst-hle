"""采集PDF正文与补充材料；不拼TeX、不自动导入出题库、不调用模型。"""
import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import time
from pathlib import Path

from diamond_factory import pdf_library
from diamond_factory.pdf_library import PaperPdfPlan, collect_library
from diamond_factory.runtime import atomic_json

parser = argparse.ArgumentParser()
parser.add_argument("--manifest", required=True)
parser.add_argument("--directory", default="work/pdf-library")
args = parser.parse_args()
manifest = Path(args.manifest)
plans = [PaperPdfPlan.model_validate_json(line) for line in manifest.read_text().splitlines() if line.strip()]
directory = Path(args.directory)
directory.mkdir(parents=True, exist_ok=True)
with (directory/".collection.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    frozen = {"input_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "input_path": str(manifest.resolve()),
        "collector_sha256": hashlib.sha256(Path(pdf_library.__file__).read_bytes()).hexdigest(),
        "entry_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pypdf_version": importlib.metadata.version("pypdf")}
    freeze = directory/"collection-manifest.json"
    if freeze.exists() and json.loads(freeze.read_text()) != frozen:
        raise ValueError("Collection input/code/parser changed; use a new directory")
    atomic_json(freeze, frozen)
    result = collect_library(plans, directory, local_base=manifest.parent, pause=time.sleep)
print(json.dumps({k: v for k, v in result.items() if k not in ("papers", "failures")}, ensure_ascii=False))
raise SystemExit(1 if result["failed"] else 0)
