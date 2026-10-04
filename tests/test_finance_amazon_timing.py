"""Matching Amazon orders to payments by timing, estimated totals, and the bank backfill."""

import re
from datetime import datetime, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from apps.finance import amazon, matching
from tests.conftest import login
from tests.test_finance import at, count, fin  # noqa: F401  (fin is a fixture)
from tests.test_finance_amazon import SENDER, rows

BASE = "/apps/finance"
IST = ZoneInfo("Asia/Kolkata")


def bank_alert(fin, when, rupees, merchant="AMAZON PAY INDIA", mid=None):  # noqa: F811
    """A stored HDFC alert email plus its parsed transaction, as sync would leave them."""
    mid = mid or f"bank-{when.isoformat()}-{rupees}"
    iso = when.replace(tzinfo=None).isoformat(timespec="seconds")
    with fin.ctx.db() as conn:
        conn.execute(
            "INSERT INTO emails(gmail_id, received_at, sender, subject, body, status) VALUES (?, ?, 'a', 's', 'b', 'parsed')",
            (mid, iso))
        return conn.execute(
            "INSERT INTO transactions(email_id, occurred_at, amount_paise, direction, instrument, counterparty_raw,"
            " counterparty_key) VALUES (?, ?, ?, 'debit', 'credit_card', ?, ?)",
            (mid, iso, round(rupees * 100), merchant, merchant.lower())).lastrowid


def order(fin, number, when, total=None, source="email", items=()):  # noqa: F811
    with fin.ctx.db() as conn:
        oid = conn.execute(
            "INSERT INTO orders(order_number, ordered_at, total_paise, total_source) VALUES (?, ?, ?, ?)",
            (number, when.replace(tzinfo=None).isoformat(timespec="seconds"),
             None if total is None else round(total * 100), source if total is not None else None)).lastrowid
        for price, qty in items:
            conn.execute("INSERT INTO order_items(order_id, title, quantity, price_paise) VALUES (?, ?, ?, ?)",
                         (oid, "Thing", qty, round(price * 100)))
    return oid


def run(fin, **kw):  # noqa: F811
    with fin.ctx.db() as conn:
        return matching.match_orders(conn, **kw)


def link_of(fin, txn_id):  # noqa: F811
    return rows(fin, "SELECT order_id, order_match, order_match_note FROM transactions WHERE id = ?", txn_id)[0]


T0 = datetime(2026, 9, 3, 10, 0)


# -- timing ---------------------------------------------------------------------------------------------
def test_a_charge_minutes_after_an_order_matches_without_any_total(fin):  # noqa: F811
    o = order(fin, "A", T0)  # no total, no items: nothing to compare but the clock
    t = bank_alert(fin, T0 + timedelta(minutes=2), 189)
    assert run(fin).ambiguous == 1
    assert link_of(fin, t) == {"order_id": o, "order_match": "ambiguous", "order_match_note": "2 min apart, amount not compared"}


def test_a_bank_alert_that_arrives_just_before_the_order_email_still_matches(fin):  # noqa: F811
    o = order(fin, "A", T0)
    t = bank_alert(fin, T0 - timedelta(minutes=1), 189)
    run(fin)
    assert link_of(fin, t)["order_id"] == o


def test_a_charge_hours_away_is_not_a_timing_match(fin):  # noqa: F811
    order(fin, "A", T0)
    t = bank_alert(fin, T0 + timedelta(hours=3), 189)
    assert run(fin).total == 0 and link_of(fin, t)["order_id"] is None
    t2 = bank_alert(fin, T0 + timedelta(minutes=31), 189, mid="late")
    assert run(fin).total == 0 and link_of(fin, t2)["order_id"] is None
    assert run(fin, window_minutes=45).total == 1  # the window is configurable


def test_timing_needs_the_alert_email_time_not_just_the_day(fin):  # noqa: F811
    order(fin, "A", T0)
    with fin.ctx.db() as conn:  # a hand-entered transaction: a day, no email time
        conn.execute("INSERT INTO transactions(occurred_at, amount_paise, direction, instrument, counterparty_raw,"
                     " counterparty_key) VALUES ('2026-09-03T10:01:00', 18900, 'debit', 'credit_card', 'AMAZON', 'amazon')")
    assert run(fin).total == 0


def test_plausible_amounts_only_when_the_order_has_prices(fin):  # noqa: F811
    o = order(fin, "A", T0, total=189, source="items", items=[(189, 1)])
    far = bank_alert(fin, T0 + timedelta(minutes=1), 4000)
    assert run(fin).total == 0 and link_of(fin, far)["order_id"] is None  # Rs 4,000 is not a Rs 189 order
    near = bank_alert(fin, T0 + timedelta(minutes=2), 229, mid="fee")  # delivery fee on top
    assert run(fin).ambiguous == 1
    assert link_of(fin, near) == {"order_id": o, "order_match": "ambiguous",
                                  "order_match_note": "2 min apart, ₹40 above the item prices"}


def test_equal_amount_beats_a_closer_but_different_charge(fin):  # noqa: F811
    o = order(fin, "A", T0, total=189, source="email", items=[(189, 1)])
    wrong = bank_alert(fin, T0 + timedelta(minutes=1), 200, mid="wrong")
    right = bank_alert(fin, T0 + timedelta(minutes=9), 189, mid="right")
    run(fin)
    assert link_of(fin, right) == {"order_id": o, "order_match": "exact", "order_match_note": "same amount, 9 min apart"}
    assert link_of(fin, wrong)["order_id"] is None


def test_orders_placed_together_pair_by_amount_not_by_clock(fin):  # noqa: F811
    a = order(fin, "A", T0, total=189, items=[(189, 1)])
    b = order(fin, "B", T0 + timedelta(minutes=1), total=381, items=[(381, 1)])
    c = order(fin, "C", T0 + timedelta(minutes=2), total=277, items=[(277, 1)])
    # alerts arrive in a different order than the orders were placed
    tc = bank_alert(fin, T0 + timedelta(minutes=3), 277)
    ta = bank_alert(fin, T0 + timedelta(minutes=4), 189)
    tb = bank_alert(fin, T0 + timedelta(minutes=5), 381)
    assert run(fin).exact == 3
    assert [link_of(fin, t)["order_id"] for t in (ta, tb, tc)] == [a, b, c]


def test_two_orders_with_the_same_amount_the_closer_one_wins(fin):  # noqa: F811
    first = order(fin, "A", T0, total=500, items=[(500, 1)])
    second = order(fin, "B", T0 + timedelta(minutes=90), total=500, items=[(500, 1)])
    t = bank_alert(fin, T0 + timedelta(minutes=1), 500)
    assert run(fin).exact == 1
    assert link_of(fin, t)["order_id"] == first
    t2 = bank_alert(fin, T0 + timedelta(minutes=91), 500, mid="second")
    run(fin)
    assert link_of(fin, t2)["order_id"] == second


def test_an_ambiguous_clock_is_left_for_you_not_guessed(fin):  # noqa: F811
    order(fin, "A", T0)
    order(fin, "B", T0 + timedelta(minutes=1))
    t = bank_alert(fin, T0 + timedelta(minutes=2), 300)  # fits both orders, nothing to tell them apart
    assert run(fin).total == 0 and link_of(fin, t)["order_id"] is None


def test_shipments_matching_item_prices(fin):  # noqa: F811
    o = order(fin, "A", T0, items=[(100, 1), (200, 2), (300, 1)])  # qty 2: no estimated total
    a = bank_alert(fin, T0 + timedelta(days=1), 300, mid="s1")
    b = bank_alert(fin, T0 + timedelta(days=2), 300, mid="s2")
    assert run(fin).split == 1
    assert [link_of(fin, t)["order_id"] for t in (a, b)] == [o, o]
    assert "shipment of 1 of the order's 3 items" in link_of(fin, a)["order_match_note"]
    assert "shipment of 2 of the order's 3 items" in link_of(fin, b)["order_match_note"]


# -- estimated totals ------------------------------------------------------------------------------------------
def parse(name):
    from tests.conftest import fixture_email

    return amazon.parse_email(*fixture_email(name, "amazon"))


def test_missing_total_is_estimated_from_item_prices_when_stored(fin):  # noqa: F811
    parsed = parse("ordered_no_total.txt")
    assert parsed.total_paise is None and parsed.items[0].price_paise == 18900
    from apps.finance import orders as orders_mod

    with fin.ctx.db() as conn:
        oid = orders_mod.upsert_order(conn, parsed, "2026-09-03T10:00:00")
        o = dict(conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone())
    assert (o["total_paise"], o["total_source"]) == (18900, "items")


def test_estimate_skips_quantities_and_missing_prices():
    est = amazon.estimate_total
    assert est([amazon.OrderItem("a", 1, 100), amazon.OrderItem("b", 1, 250)]) == 350
    assert est([amazon.OrderItem("a", 2, 100)]) is None  # per unit or per line? unknown
    assert est([amazon.OrderItem("a", 1, None)]) is None and est([]) is None


def test_a_real_total_replaces_an_estimate_but_never_the_reverse(fin):  # noqa: F811
    from apps.finance import orders as orders_mod

    with fin.ctx.db() as conn:
        est = amazon.ParsedOrder("402-1111111-2222222", items=[amazon.OrderItem("x", 1, 18900)])
        oid = orders_mod.upsert_order(conn, est, "2026-09-03T10:00:00")
        real = amazon.ParsedOrder("402-1111111-2222222", total_paise=22900, total_source="email")
        orders_mod.upsert_order(conn, real, "2026-09-03T11:00:00")
        assert tuple(conn.execute("SELECT total_paise, total_source FROM orders WHERE id = ?", (oid,)).fetchone()) == (22900, "email")
        orders_mod.upsert_order(conn, amazon.ParsedOrder("402-1111111-2222222", items=[amazon.OrderItem("x", 1, 18900)]),
                                "2026-09-03T12:00:00")
        assert conn.execute("SELECT total_paise FROM orders WHERE id = ?", (oid,)).fetchone()[0] == 22900


def test_looser_total_labels_but_never_a_subtotal():
    assert amazon.parse_total("Total: ₹1,299.00") == 129900
    assert amazon.parse_total("Amount payable ₹450") == 45000
    assert amazon.parse_total("Order Total: ₹99.00\nTotal: ₹5") == 9900  # the explicit label wins
    assert amazon.parse_total("Subtotal: ₹500.00") is None
    assert amazon.parse_total("Item total ₹500") == 50000  # 'total' with a currency right after it
    assert amazon.parse_total("Total before tax: ₹500.00") is None


# -- your case, end to end through the real sync ---------------------------------------------------------------
def order_email(number, title, rupees):
    return (f'Ordered: "{title}"',
            f"Hello Test User,\nThank you for shopping with us.\n\nOrder #\n{number}\n\n{title}\nQuantity: 1\n₹{rupees:,.2f}\n\nArriving Saturday\n")


def card_alert(rupees, merchant="AMAZON PAY INDIA"):
    return ("Alert", f"Dear Card Member, Thank you for using your HDFC Bank Credit Card ending 9876 for Rs {rupees:.2f} at {merchant} on 03-09-2026 10:01:00. Authorization code:- 012345")


def test_no_total_in_the_emails_still_matches_through_a_real_sync(fin):  # noqa: F811
    shop = [("408-7053808-1878750", "Nexllent Baby Wipes", 189, 0), ("408-8477038-5364351", "Cipla Nicotex Gums", 381, 5),
            ("408-8711359-9144303", "Wesons Stevia Drops", 277, 10)]
    for number, title, rupees, minute in shop:
        subject, body = order_email(number, title, rupees)
        fin.gmail.add(f"o{minute}", body, at(3, 10, month=9) + timedelta(minutes=minute), subject=subject, sender=SENDER)
        subject, body = card_alert(rupees + (40 if minute == 10 else 0))  # the third also paid a delivery fee
        fin.gmail.add(f"b{minute}", body, at(3, 10, month=9) + timedelta(minutes=minute + 2), subject=subject)
    result = fin.sync.run_sync(fin.ctx, now=at(5, month=9))
    assert (result["parsed"], result["orders"]["parsed"], result["orders"]["matched"]) == (3, 3, 3)
    got = rows(fin, "SELECT o.order_number, t.amount_paise, t.order_match, t.order_match_note FROM transactions t"
                    " JOIN orders o ON o.id = t.order_id ORDER BY t.amount_paise")
    assert [(g["order_number"][-4:], g["amount_paise"], g["order_match"]) for g in got] == [
        ("8750", 18900, "exact"), ("4303", 31700, "ambiguous"), ("4351", 38100, "exact")]  # by amount
    assert got[1]["order_match_note"] == "2 min apart, ₹40 above the item prices"
    assert {o["total_source"] for o in rows(fin, "SELECT total_source FROM orders")} == {"items"}


def test_page_explains_each_match_and_shows_the_email_behind_an_estimate(fin):  # noqa: F811
    client = login(TestClient(fin.app))
    subject, body = order_email("408-7053808-1878750", "Nexllent Baby Wipes", 189)
    fin.gmail.add("o1", body, at(3, 10, month=9), subject=subject, sender=SENDER)
    subject, body = card_alert(229)
    fin.gmail.add("b1", body, at(3, 10, month=9) + timedelta(minutes=2), subject=subject)
    fin.sync.run_sync(fin.ctx, now=at(5, month=9))
    html = client.get(f"{BASE}/orders").text
    assert "≈ ₹189" in html and "estimated from item prices" in html
    assert "check this match" in html and "2 min apart, ₹40 above the item prices" in html
    assert "view email" in html and "Thank you for shopping with us" in html
    assert "2 min apart" in client.get(f"{BASE}/transactions?month=2026-09").text  # the 🛒 tooltip


# -- bank backfill --------------------------------------------------------------------------------------------------
def test_backfill_reaches_back_once_then_returns_to_normal(fin):  # noqa: F811
    client = login(TestClient(fin.app))
    fin.sync.run_sync(fin.ctx, now=at(3))  # a first sync stores its own start
    subject, body = card_alert(189)
    fin.gmail.add("old", body, at(3, 10, month=9), subject=subject)  # an alert from before this month
    r = client.post(f"{BASE}/settings/backfill", data={"from_date": "2020-01-01"}, follow_redirects=False)
    assert r.status_code == 303 and fin.ctx.kv.get("backfill_from") == "2020-01-01"
    for _ in range(300):
        if not fin.ctx.scheduler.is_running("sync"):
            break
        import time
        time.sleep(0.01)
    queries = fin.gmail.queries
    epoch = int(datetime(2020, 1, 1, tzinfo=IST).timestamp())
    assert any(f"after:{epoch}" in q and "hdfcbank" in q for q in queries)   # bank alerts
    assert any(f"after:{epoch}" in q and SENDER in q for q in queries)       # and Amazon orders
    assert fin.ctx.kv.get("backfill_from") is None                           # one-shot
    assert count(fin.ctx, "SELECT COUNT(*) FROM emails WHERE gmail_id = 'old'") == 1
    n_before = len(queries)
    fin.sync.run_sync(fin.ctx, now=at(4))
    assert not any(f"after:{epoch}" in q for q in fin.gmail.queries[n_before:])  # back to the normal window
    assert count(fin.ctx, "SELECT COUNT(*) FROM emails WHERE gmail_id = 'old'") == 1  # no duplicates


def test_backfill_form_validation(fin):  # noqa: F811
    client = login(TestClient(fin.app))
    for data, text in [({"from_date": "nonsense"}, "Pick a date"), ({"from_date": "2099-01-01"}, "future")]:
        r = client.post(f"{BASE}/settings/backfill", data=data, follow_redirects=False)
        assert text in unquote(r.headers["location"])
    fin.gmail.connected = False
    r = client.post(f"{BASE}/settings/backfill", data={"from_date": "2020-01-01"}, follow_redirects=False)
    assert "Connect Gmail" in unquote(r.headers["location"]) and fin.ctx.kv.get("backfill_from") is None
    assert "Fetch older bank alerts" in client.get(f"{BASE}/settings").text


def test_orders_older_than_bank_history_point_to_the_backfill(fin):  # noqa: F811
    client = login(TestClient(fin.app))
    order(fin, "OLD", datetime(2026, 8, 27, 10, 0), total=277, items=[(277, 1)])
    with fin.ctx.db() as conn:
        conn.execute("INSERT INTO emails(gmail_id, received_at, sender, subject, body, status)"
                     " VALUES ('e0', '2026-10-01T00:00:00', 'a', 's', 'b', 'parsed')")
    html = client.get(f"{BASE}/orders").text
    assert "before your synced bank history" in html and "Fetch older bank alerts" in html


# -- migration 004 ---------------------------------------------------------------------------------------------------
def test_migration_004_backfills_estimates_for_orders_already_stored(tmp_path):
    from hub.services.db import Database
    from tests.conftest import ROOT

    src = ROOT / "apps" / "finance" / "migrations"
    mig = tmp_path / "m"
    mig.mkdir()
    for name in ("001_init.sql", "002_dynamic_categories.sql", "003_amazon_orders.sql"):
        (mig / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
    db = Database(tmp_path / "f.db", mig)
    with db() as conn:
        def add(number, total, items):
            oid = conn.execute("INSERT INTO orders(order_number, ordered_at, total_paise) VALUES (?, '2026-09-03T10:00:00', ?)",
                               (number, total)).lastrowid
            for price, qty in items:
                conn.execute("INSERT INTO order_items(order_id, title, quantity, price_paise) VALUES (?, 'x', ?, ?)", (oid, qty, price))
        add("known", 50000, [(50000, 1)])                # already has a total: left alone
        add("estimate", None, [(18900, 1), (1000, 1)])  # summed
        add("multi", None, [(18900, 2)])                 # per unit or per line? not guessed
        add("noprice", None, [(None, 1)])
        add("noitems", None, [])
    (mig / "004_order_totals_and_match_notes.sql").write_text(
        (src / "004_order_totals_and_match_notes.sql").read_text(encoding="utf-8"), encoding="utf-8")
    assert Database(tmp_path / "f.db", mig).applied == ["004_order_totals_and_match_notes.sql"]
    with db() as conn:
        got = {r["order_number"]: (r["total_paise"], r["total_source"]) for r in conn.execute("SELECT * FROM orders")}
    assert got == {"known": (50000, "email"), "estimate": (19900, "items"), "multi": (None, None),
                   "noprice": (None, None), "noitems": (None, None)}
    assert re.search(r"order_match_note", (src / "004_order_totals_and_match_notes.sql").read_text(encoding="utf-8"))
