import hashlib
import io
import json
import re
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pypdf import PdfReader

from .models import Source


def latex_from_archive(content: bytes) -> str:
    """Read TeX as inert data; never extract paths, execute TeX, or run archive code."""
    if len(content)>20_000_000:
        raise ValueError("Source archive exceeds size cap")
    try:
        with tarfile.open(fileobj=io.BytesIO(content),mode="r|*") as archive:
            rows=[]
            expanded=0
            for i,member in enumerate(archive):
                expanded+=member.size
                if i>=1000 or expanded>60_000_000:
                    raise ValueError("Expanded source archive exceeds limits")
                if not member.isfile() or not member.name.lower().endswith(".tex"):
                    continue
                if member.size>2_000_000:
                    raise ValueError("TeX member exceeds size cap")
                text=archive.extractfile(member).read().decode("utf-8","replace")
                if sum(len(t) for _,t in rows)+len(text)>5_000_000:
                    raise ValueError("Expanded TeX exceeds size cap")
                rows.append((member.name,text))
            # Prefer the main manuscript over supplementary documents, then larger source files.
            rows.sort(key=lambda row:("\\begin{document}" in row[1],
                not any(s in row[0].lower() for s in ("supp","appendix")),len(row[1])),reverse=True)
            text="\n\n".join("[LaTeX file "+name+"]\n"+body for name,body in rows)
    except tarfile.ReadError:
        import gzip
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
                content=stream.read(5_000_001)
        except (OSError,EOFError):
            pass
        if len(content)>5_000_000:
            raise ValueError("Expanded TeX exceeds size cap")
        text=content.decode("utf-8")
    if "\\begin" not in text or len(text)<1000:
        raise ValueError("No usable TeX source")
    return text


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


def download(client, url: str, path: Path, max_bytes=40_000_000):
    # Crawler only downloads arXiv. Other primary material enters through explicit local import.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with temporary.open("wb") as output:
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("Paper exceeds download size cap")
                    output.write(chunk)
        with temporary.open("rb") as content:
            if not content.read(1024).lstrip().startswith(b"%PDF-"):
                raise ValueError("Expected a PDF, received a different document")
        PdfReader(temporary)  # Do not cache a truncated file merely because its first bytes say PDF.
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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

