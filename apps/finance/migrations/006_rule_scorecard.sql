-- Every regex now lives in learned_parsers (the built-in ones are seeded on first use) and
-- carries a scorecard: `matches` goes up each time the regex reads an email, and rules are
-- always loaded most-matches-first.
UPDATE learned_parsers SET name = name || '_' || id
WHERE id NOT IN (SELECT MIN(id) FROM learned_parsers GROUP BY kind, name);

ALTER TABLE learned_parsers ADD COLUMN builtin INTEGER NOT NULL DEFAULT 0;
-- How a bank match becomes a transaction: upi | card | account | generic (see parsers.make_builder).
ALTER TABLE learned_parsers ADD COLUMN builder TEXT NOT NULL DEFAULT 'generic';
ALTER TABLE learned_parsers ADD COLUMN matches INTEGER NOT NULL DEFAULT 0;
ALTER TABLE learned_parsers ADD COLUMN last_matched_at TEXT;

CREATE UNIQUE INDEX learned_parsers_name ON learned_parsers(kind, name);
CREATE INDEX learned_parsers_score ON learned_parsers(kind, status, matches DESC);
