-- Emails the built-in regexes could not read, and what happened to them instead.
-- A row is removed once a regex (built-in or approved) reads the email.
CREATE TABLE parse_misses (
    kind TEXT NOT NULL CHECK (kind IN ('bank', 'amazon')),
    gmail_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ai_parsed', 'ai_ignored', 'ai_failed', 'ai_skipped')),
    noted_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, gmail_id)
);

-- Regexes the local model proposed from sample emails. Only 'active' ones are used,
-- and a proposal becomes active only when the user approves it.
--   bank:   pattern has named groups (amount, date, acct, merchant, vpa); direction and
--           instrument are fixed per parser.
--   amazon: pattern captures the order `total`; item_pattern captures each item's
--           `title` (and optionally `qty`, `price`).
CREATE TABLE learned_parsers (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('bank', 'amazon')),
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    item_pattern TEXT,
    direction TEXT CHECK (direction IN ('debit', 'credit')),
    instrument TEXT,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'active', 'rejected', 'disabled')),
    samples INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    preview TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at TEXT
);
CREATE INDEX learned_parsers_status ON learned_parsers(kind, status);
