-- DOI规范化身份先入库，再解析开放下载位置；PDF版本仍单独保留。
CREATE TABLE diamond.library_metadata (
    doi text PRIMARY KEY CHECK (doi = lower(doi) AND doi ~ '^10[.][0-9]{4,9}/[^[:space:]]+$'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.library_metadata_observations (
    doi text NOT NULL REFERENCES diamond.library_metadata(doi),
    metadata_sha256 text NOT NULL, body jsonb NOT NULL,
    first_run_id text NOT NULL REFERENCES diamond.runs(id),
    created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(doi, metadata_sha256)
);
CREATE INDEX library_paper_doi_lookup ON diamond.library_papers(lower(doi));
