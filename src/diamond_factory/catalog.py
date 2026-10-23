"""A queryable paper catalog; full texts stay in the existing immutable source store."""
import json
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import StrictModel, digest


class Paper(StrictModel):
    source_id: str
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}v\d+$")
    title: str
    authors: list[str] = Field(min_length=1)
    domain: Literal["Math", "Physics"]
    subtopic: str
    journal: str
    publication_year: int = Field(ge=1900, le=2100)
    doi: str = Field(pattern=r"^10\.\d{4,9}/\S+$")
    publisher_url: str
    venue_evidence_url: str
    license: str = "unspecified"
    access_policy: str = "public_preprint_local_analysis_only"
    arxiv_categories: list[str]
    selection_reason: str = Field(min_length=10)
    theory_screen: Literal["unreviewed", "theoretical", "experimental"] = "unreviewed"
    theory_screen_reason: str = ""
    fulltext_format: Literal["pdf_text", "latex_and_pdf"] = "pdf_text"
    metadata_sha256: str = ""

    def sealed(self):
        body = self.model_dump(exclude={"metadata_sha256"})
        sha = digest(body)
        if self.metadata_sha256 and self.metadata_sha256 != sha:
            raise ValueError("Paper metadata hash mismatch")
        return self.model_copy(update={"metadata_sha256": sha})


def read_catalog(path):
    papers = [Paper.model_validate_json(s).sealed() for s in Path(path).read_text().splitlines() if s.strip()]
    if not papers or len({p.source_id for p in papers}) != len(papers):
        raise ValueError("Catalog must contain unique versioned source ids")
    return papers


def save_catalog(store, papers):
    """Explicit DB initialization/migration is required before importing catalog metadata."""
    for paper in papers:
        paper = paper.sealed()
        source = store.get_source(paper.source_id)
        if not source or source.id != "arxiv:" + paper.arxiv_id:
            raise ValueError("Catalog requires its exact versioned full-text source")
        if hasattr(store, "save_paper"):
            store.save_paper(paper)
        else:
            store.db.execute("CREATE TABLE IF NOT EXISTS paper_catalog(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            store.db.execute("INSERT OR REPLACE INTO paper_catalog VALUES(?,?)",
                             (paper.source_id, paper.model_dump_json()))
            store.db.commit()


def catalog_rows(store, domain=None,journal=None,theory_only=False):
    if hasattr(store, "papers"):
        rows=store.papers(domain)
    else:
        if not store.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_catalog'").fetchone():
            return []
        rows = [json.loads(r[0]) for r in store.db.execute("SELECT body FROM paper_catalog ORDER BY id")]
    return [r for r in rows if (domain is None or r["domain"] == domain) and
            (journal is None or journal.casefold() in r["journal"].casefold()) and
            (not theory_only or r.get("theory_screen")=="theoretical")]


def fetch_catalog_sources(store,papers,directory,prefer_latex=True):
    """A curated, pinned journal/arXiv manifest is the reusable discovery/collection boundary."""
    import httpx

    from .models import Source
    from .runtime import atomic_json
    from .sources import download, latex_from_archive, pdf_text
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    last_request=0.0

    def space_requests():
        nonlocal last_request
        time.sleep(max(0,3.1-(time.monotonic()-last_request)))
        last_request=time.monotonic()

    with httpx.Client(timeout=120,follow_redirects=True,headers={"User-Agent":"rst-hle/0.1 research-pilot"}) as client:
        for paper in papers:
            paper=paper.sealed()
            existing=store.get_source(paper.source_id)
            if existing:
                actual_format=("latex_and_pdf" if "[LaTeX file" in existing.text[:200] or
                               "\\documentclass" in existing.text[:200] else "pdf_text")
                revised=paper.model_copy(update={"fulltext_format":actual_format,"metadata_sha256":""}).sealed()
                save_catalog(store,[revised])
                yield {"source_id":existing.id,"sha256":existing.sha256,"characters":len(existing.text),
                       "journal":paper.journal,"format":actual_format,"reused_full_text":True}
                continue
            pdf=directory/(paper.arxiv_id+".pdf")
            if not pdf.exists():
                space_requests()
                download(client,"https://arxiv.org/pdf/"+paper.arxiv_id,pdf)
            text=pdf_text(pdf)
            if len(text.strip())<1000:
                raise ValueError("Insufficient full-text transcription: "+paper.arxiv_id)
            actual_format="pdf_text"
            if prefer_latex:
                cache=directory/(paper.arxiv_id+".source")
                try:
                    if not cache.exists():
                        space_requests()
                        with client.stream("GET","https://arxiv.org/src/"+paper.arxiv_id) as response:
                            response.raise_for_status()
                            chunks=[];size=0
                            for chunk in response.iter_bytes():
                                size+=len(chunk)
                                if size>20_000_000:
                                    raise ValueError("Source archive exceeds size cap")
                                chunks.append(chunk)
                        content=b"".join(chunks)
                        tex=latex_from_archive(content)
                        temporary=cache.with_suffix(".part")
                        temporary.write_bytes(content);temporary.replace(cache)
                    else:
                        tex=latex_from_archive(cache.read_bytes())
                    text=tex+"\n\n[PDF page transcription]\n"+text
                    actual_format="latex_and_pdf"
                except (httpx.HTTPError,ValueError,UnicodeError,OSError) as error:
                    atomic_json(directory/(paper.arxiv_id+".latex-unavailable.json"),
                                {"error_type":type(error).__name__,"fallback":"pdf_text"})
            source=Source(id=paper.source_id,title=paper.title,uri="https://arxiv.org/abs/"+paper.arxiv_id,
                          license=paper.license,text=text)
            store.add_source(source)
            paper=paper.model_copy(update={"fulltext_format":actual_format,"metadata_sha256":""}).sealed()
            save_catalog(store,[paper])
            yield {"source_id":source.id,"sha256":source.sha256,"characters":len(text),
                   "journal":paper.journal,"format":actual_format}
