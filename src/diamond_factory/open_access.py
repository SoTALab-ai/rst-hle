"""按DOI定位开放PDF；索引命中不等于正文身份或补充材料已经核验。"""
from urllib.parse import quote

from pydantic import Field

from .models import StrictModel, digest
from .pdf_library import public_url


class PdfLocation(StrictModel):
    doi: str
    pdf_url: str
    version: str
    index_url: str
    metadata_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    repository: str
    license: str | None


def _json(client, url, *, params=None, headers=None):
    with client.stream("GET", url, params=params, headers=headers) as response:
        response.raise_for_status()
        content = bytearray()
        for block in response.iter_bytes():
            content.extend(block)
            if len(content) > 8_000_000:
                raise ValueError("Open access metadata exceeds size limit")
        import json
        return json.loads(content)


def _doi(value):
    return str(value).removeprefix("https://doi.org/").removeprefix("http://doi.org/").casefold()


def openalex_locations(client, doi, *, api_key=None):
    url = "https://api.openalex.org/works/https://doi.org/" + quote(doi, safe="/")
    headers = {"Authorization": "Bearer " + api_key} if api_key else None
    raw = _json(client, url, headers=headers)
    if not isinstance(raw, dict) or _doi(raw.get("doi")) != _doi(doi):
        raise ValueError("OpenAlex DOI identity mismatch")
    records = raw.get("locations", [])
    if not isinstance(records, list):
        raise TypeError("Invalid OpenAlex locations")
    result, seen = [], set()
    for record in records:
        if not isinstance(record, dict) or not record.get("is_oa") or not isinstance(record.get("pdf_url"), str):
            continue
        link = record["pdf_url"]
        try:
            public_url(link)
        except (ValueError, TypeError):
            continue
        if link in seen:
            continue
        seen.add(link)
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        result.append(PdfLocation(doi=doi, pdf_url=link, version=record.get("version") or "unspecifiedVersion",
            index_url=url, metadata_sha256=digest(raw), repository=source.get("display_name") or "unspecified",
            license=record.get("license")))
    order = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
    return sorted(result, key=lambda p: order.get(p.version, 3))


def scoap_locations(client, doi):
    url = "https://repo.scoap3.org/api/records/"
    raw = _json(client, url, params={"search": 'doi:"' + doi + '"', "format": "json", "size": 5})
    hits = raw.get("hits") if isinstance(raw, dict) else None
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
        raise TypeError("Invalid SCOAP3 metadata")
    result = []
    for record in hits["hits"]:
        if not isinstance(record, dict) or not isinstance(record.get("metadata"), dict):
            continue
        metadata = record.get("metadata", {})
        if not any(_doi(d.get("value")) == _doi(doi) for d in metadata.get("dois", [])):
            continue
        files = [f for f in metadata.get("_files", []) if f.get("filetype") == "pdf"]
        # 多PDF需要人工区分正文、补充或其他版本，不能把所有PDF拼成正文。
        if len(files) != 1:
            continue
        link = public_url(files[0]["file"])
        result.append(PdfLocation(doi=doi, pdf_url=link, version="publishedVersion",
            index_url="https://repo.scoap3.org/records/" + str(record["id"]), metadata_sha256=digest(metadata),
            repository="SCOAP3", license="CC-BY"))
    return result
