"""把已核对期刊的DOI队列解析成待审PDF清单；不下载正文、不调用模型。"""
import argparse
import json
import os
import time
from pathlib import Path

import httpx

from diamond_factory.open_access import openalex_locations, scoap_locations
from diamond_factory.pdf_library import PaperPdfPlan, PdfInput
from diamond_factory.runtime import atomic_json

parser = argparse.ArgumentParser()
parser.add_argument("--discovery", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--per-journal", type=int, default=2)
args = parser.parse_args()
if not 1 <= args.per_journal <= 20:
    raise ValueError("Choose 1..20 candidates per journal")
rows = json.loads(Path(args.discovery).read_text())["records"]
selected, counts = [], {}
for row in rows:
    key = row["journal_id"]
    if counts.get(key, 0) < args.per_journal:
        selected.append(row)
        counts[key] = counts.get(key, 0) + 1
plans, resolutions, failures = [], [], []
with httpx.Client(timeout=45, follow_redirects=True, headers={"User-Agent": "rst-hle/0.1 paper-library"}) as client:
    for row in selected:
        time.sleep(1.1)
        doi = row["doi"]
        locations = []
        # SCOAP3只有相应物理期刊才查询；其他方向由OpenAlex查找仓储副本。
        if row["journal_id"] in ("prl", "prd", "jhep"):
            try:
                locations = scoap_locations(client, doi)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                failures.append({"doi": doi, "provider": "scoap3", "error_type": type(error).__name__})
        if not locations:
            try:
                locations = openalex_locations(client, doi, api_key=os.environ.get("OPENALEX_API_KEY"))
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                failures.append({"doi": doi, "provider": "openalex", "error_type": type(error).__name__})
        resolutions.append({"doi": doi, "locations": [p.model_dump() for p in locations]})
        if locations:
            chosen = locations[0]
            plan = PaperPdfPlan(doi=doi, title=row["title"], journal=row["journal"], domain=row["domain"],
                metadata_url=row["metadata_source"], theory_screen="unreviewed", supplement_status="not_checked",
                documents=[PdfInput(role="main", location=chosen.pdf_url, version=chosen.version,
                                    provenance_url=chosen.index_url)])
            plans.append(plan)
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("".join(p.model_dump_json()+"\n" for p in plans))
atomic_json(output.with_suffix(".resolution.json"), {"planned_dois": len(selected), "resolved": len(plans),
    "unresolved": len(selected)-len(plans), "resolutions": resolutions, "provider_failures": failures,
    "note": "只有元数据/开放地址匹配；正文、理论适用性、公式和补充材料尚未审查。"})
print(json.dumps({"planned_dois": len(selected), "resolved": len(plans),
                  "provider_failures": len(failures), "model_calls": 0}, ensure_ascii=False))
