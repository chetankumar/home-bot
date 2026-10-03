-- Times are local (hub timezone) ISO strings without offset: 2026-10-03T14:22:05.
-- Money is integer paise.

CREATE TABLE emails (
    gmail_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('parsed', 'unparsed', 'ignored')),
    parser TEXT,
    error TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX emails_status ON emails(status);
CREATE INDEX emails_received ON emails(received_at);

CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    counts_as_spend INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE recipients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL
);

-- One recipient can own several UPI ids / merchant names.
CREATE TABLE recipient_keys (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('upi', 'merchant')),
    recipient_id INTEGER NOT NULL REFERENCES recipients(id) ON DELETE CASCADE
);

CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    email_id TEXT UNIQUE REFERENCES emails(gmail_id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    direction TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    instrument TEXT NOT NULL
        CHECK (instrument IN ('upi', 'credit_card', 'debit_card', 'netbanking', 'atm')),
    account_mask TEXT,
    counterparty_raw TEXT,
    counterparty_key TEXT,
    reference TEXT,
    recipient_id INTEGER REFERENCES recipients(id) ON DELETE SET NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    -- 1 when the category was set by hand on this row; recipient re-tagging leaves it alone.
    category_manual INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'regex' CHECK (source IN ('regex', 'ai', 'manual'))
);
CREATE INDEX transactions_occurred ON transactions(occurred_at);
CREATE INDEX transactions_key ON transactions(counterparty_key);

INSERT INTO categories(name, counts_as_spend) VALUES
    ('Groceries', 1),
    ('Food', 1),
    ('Transport', 1),
    ('Bills', 1),
    ('Shopping', 1),
    ('Health', 1),
    ('Rent', 1),
    ('Other', 1),
    -- Card-bill payments and self-transfers move money, they don't spend it.
    ('Transfers', 0);
