-- One row per run of the regex compiler: a conversation with the local model that repeats,
-- feeding back why each proposal failed, until a regex holds up or the reply limit is hit.
-- A failed run is shown on the dashboard until dismissed.
CREATE TABLE regex_negotiations (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('bank', 'amazon')),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed', 'unavailable')),
    replies INTEGER NOT NULL DEFAULT 0,       -- model replies used so far
    samples INTEGER NOT NULL DEFAULT 0,       -- missed emails the model was shown
    last_error TEXT,                          -- why it failed (the last round's feedback)
    transcript TEXT,                          -- JSON list of {role, content}
    dismissed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
CREATE INDEX regex_negotiations_kind ON regex_negotiations(kind, id DESC);
