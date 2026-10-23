import gzip
import io
import tarfile

import pytest

from diamond_factory.catalog import Paper, fetch_catalog_sources
from diamond_factory.models import Source
from diamond_factory.sources import latex_from_archive
from diamond_factory.store import Store


def tex(title):
    return "\\documentclass{article}\n\\begin{document}\n"+(title+" ")*200+"\\end{document}"


def archive(entries):
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode="w:gz") as tar:
        for name,text in entries:
            content=text.encode();info=tarfile.TarInfo(name);info.size=len(content)
            tar.addfile(info,io.BytesIO(content))
    return buffer.getvalue()


def test_latex_main_is_first_and_archive_paths_are_never_written(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    content=archive([("supplement.tex",tex("supplement")*2),("main.tex",tex("main")),
                     ("../../escaped.tex",tex("untrusted name"))])
    result=latex_from_archive(content)
    assert result.index("main.tex")<result.index("supplement.tex")
    assert not list(tmp_path.rglob("*.tex"))


def test_latex_accepts_bounded_plain_gzip_and_rejects_expansion_bomb():
    assert "\\begin{document}" in latex_from_archive(gzip.compress(tex("main").encode()))
    with pytest.raises(ValueError,match="exceeds"):
        latex_from_archive(gzip.compress(("\\begin{document}"+"x"*5_000_100).encode()))
    with pytest.raises(ValueError,match="No usable"):
        latex_from_archive(b"<html>This is an error page, not a source archive.</html>")


def test_pinned_catalog_reuses_immutable_full_text_without_any_network(tmp_path,monkeypatch):
    store=Store(tmp_path/"s.sqlite")
    p=Paper(source_id="arxiv:1304.4132v2",arxiv_id="1304.4132v2",title="Fixture",authors=["Fixture"],
        domain="Math",subtopic="组合与图论",journal="Fixture journal",publication_year=2020,
        doi="10.1000/test",publisher_url="https://example.org/paper",venue_evidence_url="https://example.org/meta",
        arxiv_categories=["math.CO"],selection_reason="Controlled metadata used only by tests.").sealed()
    source=Source(id=p.source_id,title=p.title,uri="https://arxiv.org/abs/"+p.arxiv_id,text=tex("main"))
    store.add_source(source)
    monkeypatch.setattr("httpx.Client.get",lambda *_a,**_kw:pytest.fail("Must not buy/refetch an existing version"))
    rows=list(fetch_catalog_sources(store,[p],tmp_path/"papers"))
    assert rows[0]["reused_full_text"] is True and rows[0]["sha256"]==source.sha256
    assert rows[0]["format"]=="latex_and_pdf"
