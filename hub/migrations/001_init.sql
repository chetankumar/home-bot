CREATE TABLE kv (
    app_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (app_id, key)
);

CREATE TABLE ai_usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    app_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    op TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    ok INTEGER NOT NULL,
    error TEXT
);
CREATE INDEX ai_usage_app ON ai_usage(app_id, ts);

CREATE TABLE oauth_tokens (
    provider TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE job_runs (
    id INTEGER PRIMARY KEY,
    app_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX job_runs_job ON job_runs(app_id, job_id, id);
