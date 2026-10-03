-- Where an order's total came from: the email itself, the AI reading it, or an estimate
-- (the sum of the item prices) when the email gives no total line.
ALTER TABLE orders ADD COLUMN total_source TEXT
    CHECK (total_source IN ('email', 'items', 'ai'));

-- Why a transaction was matched to its order ("same amount, 2 min apart").
ALTER TABLE transactions ADD COLUMN order_match_note TEXT;

UPDATE orders SET total_source = 'email' WHERE total_paise IS NOT NULL;

-- Orders read before this migration with items and prices but no total: estimate the
-- total from the prices. Single-quantity orders only, since for multi-quantity lines
-- it is unclear whether the price is per unit or for the whole line.
UPDATE orders
SET total_paise = (SELECT SUM(price_paise) FROM order_items WHERE order_id = orders.id),
    total_source = 'items'
WHERE total_paise IS NULL
  AND EXISTS (SELECT 1 FROM order_items WHERE order_id = orders.id)
  AND NOT EXISTS (SELECT 1 FROM order_items
                  WHERE order_id = orders.id AND (price_paise IS NULL OR quantity != 1));
