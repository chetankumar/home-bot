-- Amazon order emails (auto-confirm@amazon.in and friends), kept apart from the bank
-- alert emails so they never show up on the bank Review page. Raw bodies are stored so
-- the parser can be improved and re-run without re-fetching.
CREATE TABLE order_emails (
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
CREATE INDEX order_emails_status ON order_emails(status);

-- One row per Amazon order number; later emails (shipped, delivered, cancelled)
-- update it.
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    order_number TEXT NOT NULL UNIQUE,
    ordered_at TEXT NOT NULL,           -- local ISO time of the earliest email seen
    total_paise INTEGER,                -- NULL when no total could be read
    status TEXT NOT NULL DEFAULT 'placed'
        CHECK (status IN ('placed', 'shipped', 'delivered', 'cancelled'))
);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    price_paise INTEGER
);
CREATE INDEX order_items_order ON order_items(order_id);

-- A bank transaction matched to an order. An order can have several transactions
-- (Amazon charges per shipment); a transaction belongs to at most one order.
ALTER TABLE transactions ADD COLUMN order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL;
ALTER TABLE transactions ADD COLUMN order_match TEXT
    CHECK (order_match IN ('exact', 'ambiguous', 'split', 'manual'));
CREATE INDEX transactions_order ON transactions(order_id);
