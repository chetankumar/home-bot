-- Categories become user-managed. Descriptions are shown to the local model when it
-- picks a category, so editing one teaches the model what belongs there.
ALTER TABLE categories ADD COLUMN description TEXT;
ALTER TABLE categories ADD COLUMN created_by TEXT NOT NULL DEFAULT 'seed'
    CHECK (created_by IN ('seed', 'user', 'ai'));

-- What you said the spend was ("weekly vegetables from the market").
ALTER TABLE transactions ADD COLUMN narration TEXT;

UPDATE categories SET description = CASE name
    WHEN 'Groceries' THEN 'Supermarkets, vegetables, fruit, milk, household food'
    WHEN 'Food' THEN 'Restaurants, cafes, takeaway and food delivery'
    WHEN 'Transport' THEN 'Fuel, cabs, metro, bus, parking, tolls'
    WHEN 'Bills' THEN 'Electricity, water, gas, phone, internet, subscriptions, insurance'
    WHEN 'Shopping' THEN 'Clothes, electronics, home goods, online shopping'
    WHEN 'Health' THEN 'Doctors, pharmacy, hospital, fitness'
    WHEN 'Rent' THEN 'Rent and maintenance'
    WHEN 'Other' THEN 'Anything that fits nowhere else'
    WHEN 'Transfers' THEN 'Moving your own money: card-bill payments, transfers between your own accounts'
END;
