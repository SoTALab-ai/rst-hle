import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pypdf import PdfReader

from .models import Source


def import_sources(path: str | Path):
    path = Path(path)
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        file = row.pop("path", None)
        if file:
            file = (path.parent / file).resolve()
            if file.suffix.lower() == ".pdf":
                row["text"] = pdf_text(file)
            else:
                row["text"] = file.read_text()
        row.setdefault("retrieved_at", datetime.now(UTC).isoformat())
        yield Source.model_validate(row)


def pdf_text(path: Path) -> str:
    pages = PdfReader(path).pages
    return "\n\n".join(f"[page {i+1}]\n" + (p.extract_text() or "") for i, p in enumerate(pages))


def download(client, url: str, path: Path):
    # Crawler only downloads arXiv. Other primary material enters through explicit local import.
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with path.open("wb") as output:
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 40_000_000:
                    path.unlink(missing_ok=True)
                    raise ValueError("Paper exceeds 40 MB download cap")
                output.write(chunk)


def crawl_arxiv(query: str | None, ids: list[str], limit: int, directory: Path):
    if not 1 <= limit <= 100:
        raise ValueError("Use batches of 1–100 papers")
    if any(not re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", x) for x in ids):
        raise ValueError("Use modern arXiv ids, e.g. 1706.03762v7")
    directory.mkdir(parents=True, exist_ok=True)
    params = {"max_results": limit, "start": 0}
    if ids:
        params["id_list"] = ",".join(ids)
    elif query:
        params["search_query"] = query
        params["sortBy"] = "submittedDate"
        params["sortOrder"] = "descending"
    else:
        raise ValueError("Provide --query or --ids")
    with httpx.Client(timeout=120, follow_redirects=True, headers={
            "User-Agent": "rst-hle/0.1 research-pilot"}) as client:
        response = client.get("https://export.arxiv.org/api/query", params=params)
        response.raise_for_status()
        xml = response.text
        (directory / ("feed-" + hashlib.sha256(xml.encode()).hexdigest()[:12] + ".xml")).write_text(xml)
        atom = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
        for entry in ET.fromstring(xml).findall("a:entry", atom)[:limit]:
            identity = entry.findtext("a:id", "", atom).split("/abs/")[-1]
            if not re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", identity):
                raise ValueError("Unexpected arXiv identifier in feed")
            time.sleep(3.1)  # arXiv requests are serialized and spaced at least three seconds.
            path = directory / (identity + ".pdf")
            if not path.exists():
                download(client, "https://arxiv.org/pdf/" + identity, path)
            text = pdf_text(path)
            # Metadata/abstract alone never qualifies as source full text.
            if len(text.strip()) < 1000:
                raise ValueError("Full-text extraction insufficient; import a corrected transcription")
            yield Source(id="arxiv:" + identity,
                         title=" ".join(entry.findtext("a:title", "", atom).split()),
                         uri="https://arxiv.org/abs/" + identity,
                         license=entry.findtext("x:license", "unspecified", atom), text=text,
                         retrieved_at=datetime.now(UTC).isoformat())

