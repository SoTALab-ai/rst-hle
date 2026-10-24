-- 超时计错独立保存，不伪造答卷、实际响应模型或judge外键。
CREATE TABLE diamond.qc_deadlines (
    candidate_id text NOT NULL REFERENCES diamond.candidates(id),
    sample_id smallint NOT NULL CHECK (sample_id BETWEEN 0 AND 15),
    solver_request text NOT NULL REFERENCES diamond.model_requests(id),
    requested_model text NOT NULL,
    deadline_seconds double precision NOT NULL CHECK (deadline_seconds > 0),
    elapsed_seconds double precision NOT NULL CHECK (elapsed_seconds >= deadline_seconds),
    reason text NOT NULL CHECK (reason = 'deadline_exceeded'),
    PRIMARY KEY(candidate_id, sample_id), UNIQUE(candidate_id, solver_request)
);
