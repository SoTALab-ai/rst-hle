-- 独立控制面显式迁移；不修改旧diamond迁移链或运行中的模型/解析指纹。
CREATE TABLE rst_hle_control.reader_limits (
    id text PRIMARY KEY,
    target_run_id text NOT NULL UNIQUE REFERENCES diamond.runs(id) ON DELETE CASCADE,
    material_limit integer NOT NULL CHECK(material_limit > 0),
    manifest_sha256 text NOT NULL, manifest jsonb NOT NULL,
    state text NOT NULL DEFAULT 'watching' CHECK(state IN ('watching','draining','stopped','worker_exited')),
    denied_reservations bigint NOT NULL DEFAULT 0,
    blocked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION rst_hle_control.complete_materials(target text) RETURNS bigint
LANGUAGE sql STABLE AS $$
    WITH docs AS (
        SELECT r.paper_id,r.ordinal,d.pages,count(lp.page) AS stored,
            count(lp.page) FILTER(WHERE lp.status='unreadable') AS unclear,
            count(lp.page) FILTER(WHERE lp.status='readable') AS readable
        FROM diamond.library_run_documents r JOIN diamond.library_parses p ON p.id=r.parse_id
        JOIN diamond.library_documents d ON d.id=p.document_id
        LEFT JOIN diamond.library_pages lp ON lp.parse_id=p.id
        WHERE r.run_id=target GROUP BY r.paper_id,r.ordinal,d.pages)
    SELECT count(*) FROM (
        SELECT docs.paper_id FROM docs JOIN diamond.library_papers p ON p.id=docs.paper_id
        WHERE p.domain IN ('Math','Physics') GROUP BY docs.paper_id
        HAVING bool_and(stored=pages AND unclear=0) AND sum(readable)>0
    ) complete;
$$;
CREATE FUNCTION rst_hle_control.guard_reader_request() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE policy rst_hle_control.reader_limits%ROWTYPE;
BEGIN
    SELECT * INTO policy FROM rst_hle_control.reader_limits WHERE target_run_id=OLD.id;
    IF FOUND AND (policy.state <> 'watching' OR
                  rst_hle_control.complete_materials(OLD.id) >= policy.material_limit) THEN
        UPDATE rst_hle_control.reader_limits SET
            state=CASE WHEN state='watching' THEN 'draining' ELSE state END,
            blocked_at=coalesce(blocked_at,now()), denied_reservations=denied_reservations+1
        WHERE id=policy.id;
        -- 旧Gateway将无RETURNING行视作未获HTTP预约，尚未发送模型请求。
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER rst_hle_reader_admission BEFORE UPDATE OF http_attempts ON diamond.runs
FOR EACH ROW WHEN(NEW.http_attempts > OLD.http_attempts)
EXECUTE FUNCTION rst_hle_control.guard_reader_request();
