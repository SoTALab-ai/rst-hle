-- 独立于旧arXiv来源目录：正文/补充PDF与逐页模型转录可提前入库。
CREATE TABLE diamond.library_papers (
    id text PRIMARY KEY, title text NOT NULL, doi text, journal text NOT NULL,
    domain text NOT NULL, metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.library_documents (
    id text PRIMARY KEY, pages integer NOT NULL CHECK(pages > 0),
    object_key text NOT NULL, object_sha256 text NOT NULL, object_size bigint NOT NULL CHECK(object_size > 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.library_paper_documents (
    paper_id text NOT NULL REFERENCES diamond.library_papers(id),
    ordinal integer NOT NULL CHECK(ordinal >= 0), role text NOT NULL CHECK(role IN ('main','supplement')),
    document_id text NOT NULL REFERENCES diamond.library_documents(id),
    PRIMARY KEY(paper_id,ordinal), UNIQUE(paper_id,document_id)
);
CREATE TABLE diamond.library_parses (
    id text PRIMARY KEY, document_id text NOT NULL REFERENCES diamond.library_documents(id),
    profile_sha256 text NOT NULL, profile jsonb NOT NULL,
    UNIQUE(document_id,profile_sha256), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.library_pages (
    parse_id text NOT NULL REFERENCES diamond.library_parses(id), page integer NOT NULL CHECK(page > 0),
    status text NOT NULL CHECK(status IN ('readable','empty','unreadable')),
    request_id text NOT NULL REFERENCES diamond.model_requests(id), actual_model text NOT NULL,
    object_key text NOT NULL, object_sha256 text NOT NULL, object_size bigint NOT NULL,
    PRIMARY KEY(parse_id,page), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.library_run_documents (
    run_id text NOT NULL REFERENCES diamond.runs(id), paper_id text NOT NULL REFERENCES diamond.library_papers(id),
    ordinal integer NOT NULL, parse_id text NOT NULL REFERENCES diamond.library_parses(id),
    PRIMARY KEY(run_id,paper_id,ordinal),
    FOREIGN KEY(paper_id,ordinal) REFERENCES diamond.library_paper_documents(paper_id,ordinal)
);
CREATE INDEX library_paper_lookup ON diamond.library_papers(domain,journal);
