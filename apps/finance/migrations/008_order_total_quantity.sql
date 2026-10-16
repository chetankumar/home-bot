-- The price stored on an item is the unit price. Orders that still have no total
-- (migration 004 skipped any line with quantity other than 1) get one from
-- unit price × quantity, when every item has a price.
UPDATE orders
SET total_paise = (SELECT SUM(price_paise * quantity) FROM order_items WHERE order_id = orders.id),
    total_source = 'items'
WHERE total_paise IS NULL
  AND EXISTS (SELECT 1 FROM order_items WHERE order_id = orders.id)
  AND NOT EXISTS (SELECT 1 FROM order_items WHERE order_id = orders.id AND price_paise IS NULL);
