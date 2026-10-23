CREATE SCHEMA IF NOT EXISTS diamond;
CREATE TABLE diamond.schema_migrations (
    version integer PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.runs (
    id text PRIMARY KEY,
    mode text NOT NULL,
    status text NOT NULL,
    fingerprint text NOT NULL,
    body jsonb NOT NULL,
    http_attempts integer NOT NULL DEFAULT 0 CHECK (http_attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.sources (
    id text PRIMARY KEY, title text NOT NULL, uri text NOT NULL, kind text NOT NULL,
    text_sha256 text NOT NULL, object_key text NOT NULL, object_sha256 text NOT NULL,
    object_size bigint NOT NULL CHECK (object_size >= 0), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.model_requests (
    id text PRIMARY KEY, run_id text NOT NULL REFERENCES diamond.runs(id),
    role text NOT NULL, actual_model text NOT NULL, usage jsonb NOT NULL,
    object_key text NOT NULL, object_sha256 text NOT NULL, object_size bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.candidates (
    id text PRIMARY KEY, run_id text NOT NULL REFERENCES diamond.runs(id),
    parent_id text REFERENCES diamond.candidates(id), root_id text NOT NULL,
    generation integer NOT NULL CHECK (generation >= 0),
    category text NOT NULL, subtopic text NOT NULL, material_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('quality_seed','rejected','screened','accepted')),
    object_key text NOT NULL, object_sha256 text NOT NULL, object_size bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX candidates_category_status ON diamond.candidates(category, subtopic, status);
CREATE INDEX candidates_lineage ON diamond.candidates(root_id, generation);
-- The first production policy counts at most one item per root/material, even across competing writers.
CREATE UNIQUE INDEX accepted_root_once ON diamond.candidates(root_id) WHERE status='accepted';
CREATE UNIQUE INDEX accepted_material_once ON diamond.candidates(material_key) WHERE status='accepted';
CREATE TABLE diamond.candidate_sources (
    candidate_id text NOT NULL REFERENCES diamond.candidates(id),
    source_id text NOT NULL REFERENCES diamond.sources(id), PRIMARY KEY(candidate_id, source_id)
);
CREATE TABLE diamond.qc_samples (
    candidate_id text NOT NULL REFERENCES diamond.candidates(id),
    sample_id smallint NOT NULL CHECK (sample_id BETWEEN 0 AND 15),
    solver_request text NOT NULL REFERENCES diamond.model_requests(id),
    judge_request text NOT NULL REFERENCES diamond.model_requests(id),
    correct boolean NOT NULL, extracted_answer text NOT NULL,
    PRIMARY KEY(candidate_id, sample_id), UNIQUE(candidate_id, solver_request),
    UNIQUE(candidate_id, judge_request)
);
CREATE TABLE diamond.stage_events (
    id bigserial PRIMARY KEY, run_id text NOT NULL REFERENCES diamond.runs(id),
    event text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE diamond.quotas (
    manifest_hash text NOT NULL, category text NOT NULL, subtopic text NOT NULL,
    target integer NOT NULL CHECK(target >= 0), PRIMARY KEY(manifest_hash, category, subtopic)
);
CREATE TABLE diamond.jobs (
    id text PRIMARY KEY, queue text NOT NULL, payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed')),
    lease_owner text, lease_token text, lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claim ON diamond.jobs(queue,status,lease_until,created_at);
CREATE TABLE diamond.exports (
    id text PRIMARY KEY, manifest jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
