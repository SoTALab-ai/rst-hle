"""只读核对既有过易题证据，并输出脱敏人工审计账本。"""
import argparse
import json
from pathlib import Path

from diamond_factory.easy_audit import audit_exports

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--evidence-dir', required=True)
parser.add_argument('--findings', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
output = audit_exports(args.evidence_dir, args.findings)
Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({k: v for k, v in output.items() if k not in ('cases', 'labels')}, ensure_ascii=False))
