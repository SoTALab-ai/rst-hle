"""A queryable paper catalog; full texts stay in the existing immutable source store."""
import json
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
    sources = {s.id: s for s in store.sources()}
    for paper in papers:
        paper = paper.sealed()
        source = sources.get(paper.source_id)
        if not source or source.id != "arxiv:" + paper.arxiv_id:
            raise ValueError("Catalog requires its exact versioned full-text source")
        if hasattr(store, "save_paper"):
            store.save_paper(paper)
        else:
            store.db.execute("CREATE TABLE IF NOT EXISTS paper_catalog(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            store.db.execute("INSERT OR REPLACE INTO paper_catalog VALUES(?,?)",
                             (paper.source_id, paper.model_dump_json()))
            store.db.commit()


def catalog_rows(store, domain=None):
    if hasattr(store, "papers"):
        return store.papers(domain)
    if not store.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_catalog'").fetchone():
        return []
    rows = [json.loads(r[0]) for r in store.db.execute("SELECT body FROM paper_catalog ORDER BY id")]
    return [r for r in rows if domain is None or r["domain"] == domain]
