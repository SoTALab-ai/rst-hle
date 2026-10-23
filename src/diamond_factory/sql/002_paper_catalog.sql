CREATE TABLE diamond.paper_catalog (
    source_id text PRIMARY KEY REFERENCES diamond.sources(id) ON DELETE CASCADE,
    arxiv_id text NOT NULL UNIQUE,
    domain text NOT NULL CHECK(domain IN ('Math','Physics')),
    subtopic text NOT NULL,
    journal text NOT NULL,
    publication_year integer NOT NULL,
    doi text NOT NULL,
    metadata_sha256 text NOT NULL,
    body jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX paper_catalog_domain_journal ON diamond.paper_catalog(domain,journal,publication_year);
CREATE INDEX paper_catalog_doi ON diamond.paper_catalog(doi);
CREATE TABLE diamond.paper_metadata_versions (
    source_id text NOT NULL REFERENCES diamond.sources(id) ON DELETE CASCADE,
    metadata_sha256 text NOT NULL,
    body jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source_id,metadata_sha256)
);
