CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
