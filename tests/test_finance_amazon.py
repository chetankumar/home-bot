"""Amazon order emails: parsing, matching to payments, sync, and the pages."""

import pytest
from fastapi.testclient import TestClient

from apps.finance import amazon, categorise, matching
from apps.finance import orders as orders_mod
from apps.finance.queries import txn_rows
from tests.conftest import fixture_email, login
from tests.test_finance import at, count, fin  # noqa: F401  (fin is a fixture)

BASE = "/apps/finance"
SENDER = "auto-confirm@amazon.in"


def amz(name):
    return fixture_email(name, "amazon")


def add_amazon(fin, mid, name, when):  # noqa: F811
    subject, body = amz(name)
    fin.gmail.add(mid, body, when, subject=subject, sender=SENDER)


def add_txn(fin, day, rupees, cp="AMAZON PAY INDIA", hour=12):  # noqa: F811
    with fin.ctx.db() as conn:
        return conn.execute(
            "INSERT INTO transactions(occurred_at, amount_paise, direction, instrument, counterparty_raw, counterparty_key)"
            " VALUES (?, ?, 'debit', 'credit_card', ?, ?)",
            (f"2026-10-{day:02d}T{hour:02d}:00:00", round(rupees * 100), cp, cp.lower()),
        ).lastrowid


def rows(fin, sql, *args):  # noqa: F811
    with fin.ctx.db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# -- parsing --------------------------------------------------------------------------------------
def test_two_item_confirmation():
    o = amazon.parse_email(*amz("ordered_two_items.txt"))
    assert (o.order_number, o.status, o.total_paise, o.parser) == ("402-1234567-8901234", "placed", 459700, "body")
    assert [(i.title[:21], i.quantity, i.price_paise) for i in o.items] == [
        ("boAt Rockerz 255 Pro+", 1, 109900), ("Philips HL7756/00 Mix", 2, 349800)]


def test_rs_and_qty_variants():
    o = amazon.parse_email(*amz("ordered_single_rs.txt"))
    assert (o.order_number, o.total_paise, o.items[0].price_paise) == ("171-7654321-0987654", 45000, 45000)


def test_subject_is_the_fallback_for_items():
    o = amazon.parse_email(*amz("ordered_subject_only.txt"))
    assert (o.parser, o.total_paise, [i.title for i in o.items]) == ("subject", None, ["Duracell AA Batteries 10 Pack"])


@pytest.mark.parametrize("name,status", [("shipped.txt", "shipped"), ("delivered.txt", "delivered"), ("cancelled.txt", "cancelled"), ("item_cancelled.txt", "cancelled")])
def test_status_updates(name, status):
    assert amazon.parse_email(*amz(name)).status == status


def test_marketing_ignored_and_unreadable_order_left_for_review():
    assert amazon.parse_email(*amz("marketing.txt")) == "ignore"
    assert amazon.parse_email(*amz("unreadable_order.txt")) is None  # "Ordered:" but no order number


def test_status_never_goes_backwards_and_cancelled_is_final():
    assert amazon.merge_status("delivered", "shipped") == "delivered"
    assert amazon.merge_status("placed", "shipped") == "shipped"
    assert amazon.merge_status("shipped", "cancelled") == "cancelled"
    assert amazon.merge_status("cancelled", "delivered") == "cancelled"


# -- matching ---------------------------------------------------------------------------------------
def add_order(fin, number, day, rupees, status="placed"):  # noqa: F811
    with fin.ctx.db() as conn:
        return conn.execute(
            "INSERT INTO orders(order_number, ordered_at, total_paise, status) VALUES (?, ?, ?, ?)",
            (number, f"2026-10-{day:02d}T09:00:00", None if rupees is None else round(rupees * 100), status),
        ).lastrowid


def match(fin):  # noqa: F811
    with fin.ctx.db() as conn:
        return matching.match_orders(conn)


def linked(fin):  # noqa: F811
    return {r["id"]: (r["order_id"], r["order_match"]) for r in rows(fin, "SELECT id, order_id, order_match FROM transactions")}


def test_exact_match_inside_the_window(fin):  # noqa: F811
    o = add_order(fin, "A", 1, 1099)
    t = add_txn(fin, 2, 1099)
    assert match(fin).exact == 1 and linked(fin) == {t: (o, "exact")}
    assert match(fin).total == 0  # re-running changes nothing


def test_amount_and_window_must_both_fit(fin):  # noqa: F811
    add_order(fin, "A", 10, 1099)
    wrong_amount = add_txn(fin, 11, 1098)
    too_early = add_txn(fin, 8, 1099)
    too_late = add_txn(fin, 26, 1099)  # 16 days after: outside the 14-day window
    same_day_before = add_txn(fin, 9, 1099)  # one day before is allowed (time zones)
    match(fin)
    assert linked(fin)[same_day_before][0] is not None
    assert [linked(fin)[t][0] for t in (wrong_amount, too_early, too_late)] == [None, None, None]


def test_only_amazon_debits_are_candidates(fin):  # noqa: F811
    add_order(fin, "A", 1, 500)
    swiggy = add_txn(fin, 2, 500, cp="SWIGGY")
    assert match(fin).total == 0 and linked(fin)[swiggy][0] is None
    upi = add_txn(fin, 2, 500, cp="amazonpay@apl AMAZON PAY")
    assert match(fin).exact == 1 and linked(fin)[upi][0] is not None


def test_same_amount_orders_pair_by_closest_date_and_are_flagged(fin):  # noqa: F811
    o1, o2 = add_order(fin, "B", 3, 499), add_order(fin, "C", 5, 499)
    t1, t2 = add_txn(fin, 4, 499), add_txn(fin, 6, 499)
    assert match(fin).ambiguous == 2
    assert linked(fin) == {t1: (o1, "ambiguous"), t2: (o2, "ambiguous")}


def test_unique_pairs_resolve_first_then_the_rest_is_unambiguous(fin):  # noqa: F811
    o1, o2 = add_order(fin, "B", 3, 499), add_order(fin, "C", 20, 499)
    t1 = add_txn(fin, 4, 499)  # only o1 is within 14 days of this
    t2 = add_txn(fin, 22, 499)  # only o2 is
    assert match(fin).exact == 2 and linked(fin) == {t1: (o1, "exact"), t2: (o2, "exact")}


def test_split_shipments_match_as_a_group(fin):  # noqa: F811
    o = add_order(fin, "D", 8, 3000)
    a, b, other = add_txn(fin, 9, 1000), add_txn(fin, 10, 2000), add_txn(fin, 9, 777)
    assert match(fin).split == 1
    assert linked(fin) == {a: (o, "split"), b: (o, "split"), other: (None, None)}


def test_cancelled_unknown_total_and_manual_links_are_left_alone(fin):  # noqa: F811
    cancelled = add_order(fin, "X", 1, 500, status="cancelled")
    add_order(fin, "Y", 1, None)
    t = add_txn(fin, 2, 500)
    assert match(fin).total == 0 and linked(fin)[t][0] is None
    with fin.ctx.db() as conn:
        matching.link_manually(conn, cancelled, t)
    add_order(fin, "Z", 2, 500)
    assert match(fin).total == 0  # t is taken (by hand), so Z does not steal it
    assert linked(fin)[t] == (cancelled, "manual")
    with fin.ctx.db() as conn:
        matching.unlink(conn, cancelled)
    assert linked(fin)[t] == (None, None)


# -- sync ---------------------------------------------------------------------------------------------
def test_sync_reads_orders_only_from_the_amazon_sender_and_matches(fin):  # noqa: F811
    add_txn(fin, 2, 4597)
    add_amazon(fin, "a1", "ordered_two_items.txt", at(1, 9))
    subject, body = fixture_email("upi_debit.txt")
    fin.gmail.add("bank1", body, at(2, 9), subject=subject)  # a bank alert must not become an order
    result = fin.sync.run_sync(fin.ctx, now=at(3))
    assert result["orders"] == {"fetched": 1, "new": 1, "parsed": 1, "ignored": 0, "unparsed": 0, "matched": 1}
    o = rows(fin, "SELECT * FROM orders")[0]
    assert (o["order_number"], o["total_paise"], o["ordered_at"]) == ("402-1234567-8901234", 459700, "2026-10-01T09:00:00")
    assert [i["quantity"] for i in rows(fin, "SELECT * FROM order_items ORDER BY id")] == [1, 2]
    assert rows(fin, "SELECT order_id, order_match FROM transactions WHERE amount_paise = 459700")[0]["order_match"] == "exact"
    assert fin.anthropic.calls == [] and fin.ollama.calls == []  # a clean email needs no model


def test_sync_first_run_looks_back_90_days_then_a_day_before_the_newest(fin):  # noqa: F811
    add_amazon(fin, "a1", "ordered_single_rs.txt", at(2, 9))
    fin.sync.run_sync(fin.ctx, now=at(3))
    fin.sync.run_sync(fin.ctx, now=at(4))
    amazon_q = [q for q in fin.gmail.queries if SENDER in q]
    assert "after:" in amazon_q[0] and amazon_q[0].startswith(f"from:({SENDER})")
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    first = int(re.search(r"after:(\d+)", amazon_q[0]).group(1))
    assert first == int(datetime(2026, 7, 5, tzinfo=ist).timestamp())  # 90 days before 3 Oct, at midnight
    second = int(re.search(r"after:(\d+)", amazon_q[1]).group(1))
    assert second == int(datetime(2026, 10, 1, 9, tzinfo=ist).timestamp())


def test_sync_is_idempotent_and_later_emails_update_the_order(fin):  # noqa: F811
    add_amazon(fin, "a1", "ordered_two_items.txt", at(1, 9))
    fin.sync.run_sync(fin.ctx, now=at(2))
    assert fin.sync.run_sync(fin.ctx, now=at(2))["orders"]["new"] == 0
    assert count(fin.ctx, "SELECT COUNT(*) FROM orders") == 1

    add_amazon(fin, "a2", "shipped.txt", at(2, 10))  # names one item only
    add_amazon(fin, "a3", "delivered.txt", at(4, 10))
    fin.sync.run_sync(fin.ctx, now=at(5))
    o = rows(fin, "SELECT * FROM orders")[0]
    assert (o["status"], o["ordered_at"]) == ("delivered", "2026-10-01T09:00:00")
    assert count(fin.ctx, "SELECT COUNT(*) FROM order_items") == 2  # the fuller list survived


def test_cancellation_releases_the_payment(fin):  # noqa: F811
    add_txn(fin, 2, 450)
    add_amazon(fin, "a1", "ordered_single_rs.txt", at(1, 9))
    fin.sync.run_sync(fin.ctx, now=at(3))
    assert rows(fin, "SELECT order_id FROM transactions")[0]["order_id"] is not None
    add_amazon(fin, "a2", "cancelled.txt", at(3, 10))
    fin.sync.run_sync(fin.ctx, now=at(4))
    assert rows(fin, "SELECT order_id FROM transactions")[0]["order_id"] is None
    assert rows(fin, "SELECT status FROM orders")[0]["status"] == "cancelled"


def test_payment_arriving_later_is_matched_on_the_next_sync(fin):  # noqa: F811
    add_amazon(fin, "a1", "ordered_single_rs.txt", at(1, 9))
    assert fin.sync.run_sync(fin.ctx, now=at(2))["orders"]["matched"] == 0
    add_txn(fin, 3, 450)
    assert fin.sync.run_sync(fin.ctx, now=at(4))["orders"]["matched"] == 1


def test_amazon_failure_never_fails_the_bank_sync(fin):  # noqa: F811
    subject, body = fixture_email("upi_debit.txt")
    fin.gmail.add("bank1", body, at(2, 9), subject=subject)
    real = fin.gmail.search

    def search(query, limit=2000):
        if SENDER in query:
            raise RuntimeError("amazon boom")
        return real(query, limit)

    fin.gmail.search = search
    result = fin.sync.run_sync(fin.ctx, now=at(3))
    assert result["parsed"] == 1 and result["orders"] == {"error": "amazon boom"}


# -- local-model fallback ---------------------------------------------------------------------------------
def test_model_fills_in_items_and_total_the_regexes_missed(fin):  # noqa: F811
    add_amazon(fin, "a1", "ordered_subject_only.txt", at(1, 9))
    fin.ollama.outputs = [{"is_order": True, "order_number": "403-1111111-2222222", "total": 899.5,
                           "items": [{"title": "Duracell AA Batteries 10 Pack", "quantity": 1, "price": 649.5},
                                     {"title": "Duracell AAA 8 Pack", "quantity": 1, "price": 250}]}]
    fin.sync.run_sync(fin.ctx, now=at(2))
    o = rows(fin, "SELECT * FROM orders")[0]
    assert o["total_paise"] == 89950
    assert [i["title"] for i in rows(fin, "SELECT * FROM order_items ORDER BY id")] == [
        "Duracell AA Batteries 10 Pack", "Duracell AAA 8 Pack"]
    assert rows(fin, "SELECT parser FROM order_emails")[0]["parser"] == "ai"
    assert fin.anthropic.calls == []
    assert {u["provider"] for u in fin.app.state.hub.ai.usage_summary()} == {"ollama"}


def test_model_down_keeps_what_the_subject_gave(fin):  # noqa: F811
    add_amazon(fin, "a1", "ordered_subject_only.txt", at(1, 9))
    fin.ollama.extract = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
    result = fin.sync.run_sync(fin.ctx, now=at(2))
    assert result["orders"]["parsed"] == 1
    assert [i["title"] for i in rows(fin, "SELECT * FROM order_items")] == ["Duracell AA Batteries 10 Pack"]
    assert rows(fin, "SELECT total_paise FROM orders")[0]["total_paise"] is None


def test_unreadable_email_waits_for_review_then_reparse_recovers_it(fin):  # noqa: F811
    client = login(TestClient(fin.app))
    add_amazon(fin, "a1", "unreadable_order.txt", at(1, 9))
    fin.ollama.extract = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
    assert fin.sync.run_sync(fin.ctx, now=at(2))["orders"]["unparsed"] == 1
    assert "Mystery item" in client.get(f"{BASE}/orders").text  # shown for checking
    fin.ollama.extract = lambda *a, **k: (_ for _ in ()).throw(AssertionError("replaced below"))
    del fin.ollama.extract  # back to FakeProvider.extract
    fin.ollama.outputs = [{"is_order": True, "order_number": "171-0000000-1111111", "total": 120,
                           "items": [{"title": "Mystery item", "quantity": 1, "price": 120}]}]
    r = client.post(f"{BASE}/orders/reparse", data={"use_ai": "1"})
    assert "1 parsed" in r.text
    assert rows(fin, "SELECT order_number FROM orders") == [{"order_number": "171-0000000-1111111"}]


def test_non_order_amazon_mail_is_ignored_without_the_model(fin):  # noqa: F811
    add_amazon(fin, "a1", "marketing.txt", at(1, 9))
    assert fin.sync.run_sync(fin.ctx, now=at(2))["orders"]["ignored"] == 1
    assert fin.ollama.calls == []


# -- pages ------------------------------------------------------------------------------------------------------
def seeded(fin):  # noqa: F811
    add_txn(fin, 2, 4597)
    add_amazon(fin, "a1", "ordered_two_items.txt", at(1, 9))
    add_amazon(fin, "a2", "ordered_single_rs.txt", at(1, 10))
    fin.sync.run_sync(fin.ctx, now=at(3))
    return login(TestClient(fin.app))


def test_orders_page_lists_items_and_payment_state(fin):  # noqa: F811
    html = seeded(fin).get(f"{BASE}/orders").text
    assert "Philips HL7756/00 Mixer Grinder" in html and "Classmate Notebook" in html
    assert "no matching charge yet" in html  # the Rs 450 order has no payment
    assert "1% " not in html and "50%" in html  # 1 of 2 orders matched
    unmatched = seeded(fin).get(f"{BASE}/orders?show=unmatched").text
    assert "Classmate Notebook" in unmatched and "Philips" not in unmatched


def test_manual_link_unlink_and_match_now(fin):  # noqa: F811
    client = seeded(fin)
    oid = rows(fin, "SELECT id FROM orders WHERE order_number = '171-7654321-0987654'")[0]["id"]
    t = add_txn(fin, 3, 450.5)  # off by 50 paise: won't auto-match
    client.post(f"{BASE}/orders/match")
    assert rows(fin, "SELECT order_id FROM transactions WHERE id = ?", t)[0]["order_id"] is None
    client.post(f"{BASE}/orders/{oid}/link", data={"txn_id": str(t)})
    assert rows(fin, "SELECT order_id, order_match FROM transactions WHERE id = ?", t) == [{"order_id": oid, "order_match": "manual"}]
    client.post(f"{BASE}/orders/{oid}/unlink")
    assert rows(fin, "SELECT order_id FROM transactions WHERE id = ?", t)[0]["order_id"] is None


def test_marking_an_order_not_real_releases_its_payment_and_restore_matches_again(fin):  # noqa: F811
    client = seeded(fin)
    oid = rows(fin, "SELECT id FROM orders WHERE order_number = '171-7654321-0987654'")[0]["id"]
    t = add_txn(fin, 3, 450)
    client.post(f"{BASE}/orders/match")
    assert rows(fin, "SELECT order_id FROM transactions WHERE id = ?", t)[0]["order_id"] == oid
    client.post(f"{BASE}/orders/{oid}/status", data={"status": "cancelled"})
    assert rows(fin, "SELECT status FROM orders WHERE id = ?", oid)[0]["status"] == "cancelled"
    assert rows(fin, "SELECT order_id FROM transactions WHERE id = ?", t)[0]["order_id"] is None
    client.post(f"{BASE}/orders/match")
    assert rows(fin, "SELECT order_id FROM transactions WHERE id = ?", t)[0]["order_id"] is None
    client.post(f"{BASE}/orders/{oid}/status", data={"status": "placed"})
    client.post(f"{BASE}/orders/match")
    assert rows(fin, "SELECT order_id FROM transactions WHERE id = ?", t)[0]["order_id"] == oid
    assert client.post(f"{BASE}/orders/9999/status", data={"status": "cancelled"}).status_code == 404


def test_matched_transaction_shows_items_and_prefills_the_narration(fin):  # noqa: F811
    client = seeded(fin)
    html = client.get(f"{BASE}/transactions?month=2026-10").text
    assert "🛒 boAt Rockerz 255 Pro+ Bluetooth Neckband with Upto 60 Hours…, Philips HL7756/00 Mixer Grinder 750W" in html
    assert 'value="boAt Rockerz' in html  # one click to categorise from what you bought


def test_model_sees_the_ordered_items_when_categorising(fin):  # noqa: F811
    client = seeded(fin)
    tid = rows(fin, "SELECT id FROM transactions WHERE order_id IS NOT NULL")[0]["id"]
    fin.ollama.outputs = [{"category": "Shopping", "is_new": False, "counts_as_spend": True}]
    client.post(f"{BASE}/transactions/{tid}/narrate", data={"narration": "gadgets"})
    assert "Amazon order items: boAt Rockerz" in fin.ollama.calls[0][2][-1].content
    with fin.ctx.db() as conn:
        assert "Amazon order items" in categorise.transaction_context(conn, tid)


def test_summary_truncates(fin):  # noqa: F811
    with fin.ctx.db() as conn:
        oid = conn.execute("INSERT INTO orders(order_number, ordered_at, total_paise) VALUES ('Q', '2026-10-01T00:00:00', 1)").lastrowid
        for t in ["A" * 100, "B", "C", "D", "E"]:
            conn.execute("INSERT INTO order_items(order_id, title) VALUES (?, ?)", (oid, t))
        from apps.finance.queries import order_summary
        s = order_summary(conn, oid)
    assert s == "A" * 59 + "…, B, C +2 more"
    assert txn_rows is not None and orders_mod.DEFAULT_SENDERS == [SENDER]


def test_settings_edit_amazon_senders(fin):  # noqa: F811
    client = login(TestClient(fin.app))
    client.post(f"{BASE}/settings", data={"budget_rupees": "", "sender_list": "a@b.com", "amazon_sender_list": "auto-confirm@amazon.in\norder-update@amazon.in"})
    assert fin.ctx.kv.get("amazon_senders") == ["auto-confirm@amazon.in", "order-update@amazon.in"]
    assert "order-update@amazon.in" in client.get(f"{BASE}/settings").text


# -- migration ------------------------------------------------------------------------------------------------------
def test_migration_003_upgrades_a_database_with_data(tmp_path):
    from hub.services.db import Database
    from tests.conftest import ROOT

    src = ROOT / "apps" / "finance" / "migrations"
    mig = tmp_path / "m"
    mig.mkdir()
    for name in ("001_init.sql", "002_dynamic_categories.sql"):
        (mig / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
    db = Database(tmp_path / "f.db", mig)
    with db() as conn:
        conn.execute("INSERT INTO transactions(occurred_at, amount_paise, direction, instrument, narration)"
                     " VALUES ('2026-10-01T10:00:00', 5000, 'debit', 'upi', 'kept')")
    (mig / "003_amazon_orders.sql").write_text((src / "003_amazon_orders.sql").read_text(encoding="utf-8"), encoding="utf-8")
    assert Database(tmp_path / "f.db", mig).applied == ["003_amazon_orders.sql"]
    with db() as conn:
        t = conn.execute("SELECT amount_paise, narration, order_id, order_match FROM transactions").fetchone()
    assert tuple(t) == (5000, "kept", None, None)


# -- regression: a stray "rs ," in an email crashed the whole scan --------------------------------------
STRAY = """Hello Test User, your order 402-9999999-8888888 is confirmed.

Order #
402-9999999-8888888

Boat Headphones
Quantity: 1
Sold by cloudtail, rs , seller of record
₹799.00

Order Total: ₹799.00
"""


def test_stray_currency_lookalikes_are_not_amounts():
    assert amazon.MONEY.search("many orders , rs , ok") is None
    assert amazon.MONEY.search("Total: Rs , please") is None
    assert amazon.MONEY.search("Price ₹1,299.50").group(1) == "1,299.50"
    assert amazon.MONEY.search("INR 450").group(1) == "450"
    o = amazon.parse_email("Ordered: \"Boat Headphones\"", STRAY)
    assert (o.total_paise, [(i.title, i.price_paise) for i in o.items]) == (79900, [("Boat Headphones", 79900)])


def test_one_unreadable_email_does_not_stop_the_scan(fin, monkeypatch):  # noqa: F811
    import hub_apps.finance.amazon as amazon_mod

    add_amazon(fin, "good1", "ordered_single_rs.txt", at(1, 9))
    fin.gmail.add("bad", STRAY, at(2, 9), subject='Ordered: "Poison"', sender=SENDER)
    add_amazon(fin, "good2", "ordered_two_items.txt", at(3, 9))
    real = amazon_mod.parse_email

    def parse(subject, body, *learned):
        if "Poison" in subject:
            raise ValueError("bad amount ','")
        return real(subject, body, *learned)

    monkeypatch.setattr(amazon_mod, "parse_email", parse)
    result = fin.sync.run_sync(fin.ctx, now=at(4))["orders"]
    assert "error" not in result and (result["new"], result["parsed"], result["unparsed"]) == (3, 2, 1)
    assert count(fin.ctx, "SELECT COUNT(*) FROM orders") == 2  # both good orders were saved
    bad = rows(fin, "SELECT status, error FROM order_emails WHERE gmail_id = 'bad'")[0]
    assert bad["status"] == "unparsed" and "ValueError: bad amount" in bad["error"]
    assert fin.sync.run_sync(fin.ctx, now=at(5))["orders"]["new"] == 0  # and the next sync is fine
    html = login(TestClient(fin.app)).get(f"{BASE}/orders").text
    assert "Poison" in html and "bad amount" in html  # visible for review, with the reason


def test_one_unreadable_bank_alert_does_not_stop_the_sync(fin, monkeypatch):  # noqa: F811
    import hub_apps.finance.sync as sync_mod

    for i, (mid, name) in enumerate([("b1", "upi_debit.txt"), ("b2", "credit_card_v1.txt")]):
        subject, body = fixture_email(name)
        fin.gmail.add(mid, body, at(1 + i, 9), subject=subject)
    real = sync_mod.parse_email

    def parse(subject, body, *extra):
        if "Credit Card" in body:
            raise RuntimeError("parser blew up")
        return real(subject, body, *extra)

    monkeypatch.setattr(sync_mod, "parse_email", parse)
    result = fin.sync.run_sync(fin.ctx, now=at(3))
    assert (result["new"], result["parsed"], result["unparsed"]) == (2, 1, 1)
    row = rows(fin, "SELECT status, error FROM emails WHERE gmail_id = 'b2'")[0]
    assert row["status"] == "unparsed" and "parser blew up" in row["error"]
    assert count(fin.ctx, "SELECT COUNT(*) FROM transactions") == 1


# -- the Orders page's own sync -----------------------------------------------------------------------------
def wait_idle(fin, job="orders"):  # noqa: F811
    import time

    for _ in range(300):
        if not fin.ctx.scheduler.is_running(job):
            return
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_orders_have_their_own_scheduled_job(fin):  # noqa: F811
    jobs = {j["job_id"]: j for j in fin.app.state.hub.scheduler.jobs() if j["app_id"] == "finance"}
    assert set(jobs) == {"sync", "orders", "learn_bank", "learn_amazon"}  # the learn_* jobs only run on demand
    assert "hour='19'" in jobs["orders"]["trigger"] and "minute='45'" in jobs["orders"]["trigger"]


def test_orders_job_scans_amazon_only_and_remembers_the_result(fin):  # noqa: F811
    add_txn(fin, 2, 450)
    add_amazon(fin, "a1", "ordered_single_rs.txt", at(1, 9))
    subject, body = fixture_email("upi_debit.txt")
    fin.gmail.add("bank1", body, at(2, 9), subject=subject)
    assert fin.ctx.scheduler.run_now("orders", wait=True)
    assert [q for q in fin.gmail.queries if "hdfcbank" in q] == []  # no bank mail touched
    assert count(fin.ctx, "SELECT COUNT(*) FROM emails") == 0
    assert fin.ctx.scheduler.last_run("orders")["status"] == "ok"
    last = fin.ctx.kv.get("last_orders_sync")
    assert (last["new"], last["parsed"], last["matched"]) == (1, 1, 1)


def test_orders_sync_button_shows_live_status_then_refreshes(fin):  # noqa: F811
    import threading

    client = login(TestClient(fin.app))
    gate = threading.Event()
    real = fin.gmail.search
    fin.gmail.search = lambda q, limit=2000: (gate.wait(5), real(q, limit))[1]
    add_amazon(fin, "a1", "ordered_single_rs.txt", at(1, 9))

    page = client.get(f"{BASE}/orders").text
    assert "Sync orders" in page and 'hx-post="/apps/finance/orders/sync"' in page
    r = client.post(f"{BASE}/orders/sync")
    assert "Syncing orders…" in r.text and "every 2s" in r.text  # polling while it runs
    assert "HX-Refresh" not in r.headers
    assert client.post(f"{BASE}/orders/sync").text.count("every 2s") == 1  # a second click doesn't start another
    gate.set()
    wait_idle(fin)
    done = client.get(f"{BASE}/orders/sync/status?watch=1")
    assert done.headers["HX-Refresh"] == "true" and "every 2s" not in done.text
    assert "1 new email(s)" in client.get(f"{BASE}/orders").text
    assert count(fin.ctx, "SELECT COUNT(*) FROM orders") == 1


def test_orders_sync_failure_is_recorded_and_shown(fin):  # noqa: F811
    client = login(TestClient(fin.app))

    def boom(q, limit=2000):
        raise RuntimeError("gmail is down")

    fin.gmail.search = boom
    client.post(f"{BASE}/orders/sync")
    wait_idle(fin)
    run = fin.ctx.scheduler.last_run("orders")
    assert run["status"] == "error" and "gmail is down" in run["error"]
    assert fin.ctx.kv.get("last_orders_sync")["error"] == "gmail is down"
    assert "gmail is down" in client.get(f"{BASE}/orders").text


def test_orders_sync_needs_gmail_connected(fin):  # noqa: F811
    client = login(TestClient(fin.app))
    fin.gmail.connected = False
    html = client.get(f"{BASE}/orders").text
    assert "Gmail not connected" in html and "Sync orders" not in html
    client.post(f"{BASE}/orders/sync")
    assert fin.ctx.scheduler.last_run("orders") is None  # nothing was started


def test_orders_cron_is_configurable(hub_app):
    from tests.conftest import BASE_TOML

    toml = {**BASE_TOML, "apps": {**BASE_TOML["apps"], "finance": {**BASE_TOML["apps"]["finance"], "orders_cron": "5 6 * * *"}}}
    app = hub_app(toml=toml)
    job = next(j for j in app.state.hub.scheduler.jobs() if j["id"] == "finance:orders")
    assert "hour='6'" in job["trigger"] and "minute='5'" in job["trigger"]
