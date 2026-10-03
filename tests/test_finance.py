"""Finance app wired through the real host: sync, tagging, stats, AI fallback, pages."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from apps.finance import stats
from hub.services.gmail import NotConnected
from tests.conftest import FakeGmail, fixture_email, login
from tests.test_ai import FakeProvider

IST = ZoneInfo("Asia/Kolkata")


def at(day: int, hour: int = 12, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=IST)


@pytest.fixture
def fin(hub_app):
    app = hub_app()
    hub = app.state.hub
    entry = hub.registry.apps["finance"]
    assert entry.status == "loaded", entry.error
    ctx = entry.ctx
    gmail = FakeGmail()
    ctx._gmail = gmail
    ollama = FakeProvider("ollama")
    anthropic = FakeProvider("anthropic")
    hub.ai.providers = {"ollama": ollama, "anthropic": anthropic}
    sync = __import__("hub_apps.finance.sync", fromlist=["run_sync"])
    return type("Fin", (), {"app": app, "ctx": ctx, "gmail": gmail, "ollama": ollama,
                            "anthropic": anthropic, "sync": sync})


def add_fixture(gmail, mid, name, when):
    subject, body = fixture_email(name)
    gmail.add(mid, body, when, subject=subject)


def count(ctx, sql, *args):
    with ctx.db() as conn:
        return conn.execute(sql, args).fetchone()[0]


def test_first_sync_starts_at_first_of_month_ist(fin):
    fin.sync.run_sync(fin.ctx, now=at(3))
    q = fin.gmail.queries[0]
    assert "from:(alerts@hdfcbank.net OR alerts@hdfcbank.bank.in)" in q
    # 2026-10-01 00:00 IST == 2026-09-30 18:30 UTC
    assert q.endswith(f"after:{int(datetime(2026, 10, 1, tzinfo=IST).timestamp())}")


def test_sync_is_idempotent_and_classifies(fin):
    add_fixture(fin.gmail, "m1", "upi_debit.txt", at(2, 9))
    add_fixture(fin.gmail, "m2", "credit_card_v1.txt", at(2, 21))
    add_fixture(fin.gmail, "m3", "otp.txt", at(2, 21))
    result = fin.sync.run_sync(fin.ctx, now=at(3))
    assert result.pop("orders")["fetched"] == 0  # no Amazon mail in this mailbox
    assert result == {"fetched": 3, "new": 3, "parsed": 2, "ignored": 1, "unparsed": 0}

    again = fin.sync.run_sync(fin.ctx, now=at(3))
    assert again["new"] == 0
    assert sorted(fin.gmail.fetched) == ["m1", "m2", "m3"]  # nothing re-downloaded
    assert count(fin.ctx, "SELECT COUNT(*) FROM emails") == 3
    assert count(fin.ctx, "SELECT COUNT(*) FROM transactions") == 2
    # later runs overlap by a day from the newest stored email
    bank = [q for q in fin.gmail.queries if "hdfcbank" in q]
    assert bank[1].endswith(f"after:{int(datetime(2026, 10, 1, 21, tzinfo=IST).timestamp())}")
    assert fin.ctx.kv.get("last_sync")["new"] == 0


def test_same_gmail_id_twice_yields_one_row(fin):
    from hub_apps.finance.sync import insert_transaction

    from apps.finance.parsers import parse_email

    add_fixture(fin.gmail, "dup", "upi_debit.txt", at(2))
    fin.sync.run_sync(fin.ctx, now=at(3))
    with fin.ctx.db() as conn:
        p = parse_email(*fixture_email("upi_debit.txt"))
        assert insert_transaction(conn, "dup", "2026-10-02T12:00:00", p, "regex") is None
    assert count(fin.ctx, "SELECT COUNT(*) FROM transactions") == 1


def test_occurred_at_uses_receive_time_or_alert_date(fin):
    add_fixture(fin.gmail, "same", "upi_debit.txt", at(2, 9))  # alert says 02-10-26
    add_fixture(fin.gmail, "late", "netbanking.txt", at(2, 9))  # alert says 01-10-26
    fin.sync.run_sync(fin.ctx, now=at(3))
    with fin.ctx.db() as conn:
        rows = dict(conn.execute("SELECT email_id, occurred_at FROM transactions").fetchall())
    assert rows == {"same": "2026-10-02T09:00:00", "late": "2026-10-01T00:00:00"}


def test_tagging_backfills_and_applies_to_future(fin):
    client = login(TestClient(fin.app))
    add_fixture(fin.gmail, "a", "upi_debit.txt", at(1))
    add_fixture(fin.gmail, "b", "upi_debit.txt", at(2))
    fin.sync.run_sync(fin.ctx, now=at(3))
    assert count(fin.ctx, "SELECT COUNT(*) FROM transactions WHERE recipient_id IS NULL") == 2

    page = client.get("/apps/finance/recipients")
    assert "swiggy.stores@axisbank" in page.text
    food = count(fin.ctx, "SELECT id FROM categories WHERE name = 'Food'")
    r = client.post(
        "/apps/finance/recipients",
        data={"name": "Swiggy", "category_id": str(food), "key": "swiggy.stores@axisbank", "kind": "upi"},
    )
    assert "2 transactions tagged" in r.text
    assert count(fin.ctx, "SELECT COUNT(*) FROM transactions WHERE recipient_id IS NOT NULL AND category_id = ?", food) == 2

    add_fixture(fin.gmail, "c", "upi_debit.txt", at(4))
    fin.sync.run_sync(fin.ctx, now=at(4, 18))
    assert count(fin.ctx, "SELECT category_id FROM transactions WHERE email_id = 'c'") == food

    # Changing the recipient's category re-tags its rows, except hand-set ones.
    rid = count(fin.ctx, "SELECT id FROM recipients WHERE name = 'Swiggy'")
    txn = count(fin.ctx, "SELECT id FROM transactions WHERE email_id = 'a'")
    health = count(fin.ctx, "SELECT id FROM categories WHERE name = 'Health'")
    client.post(f"/apps/finance/transactions/{txn}", data={"category_id": str(health), "recipient_id": str(rid)})
    groceries = count(fin.ctx, "SELECT id FROM categories WHERE name = 'Groceries'")
    client.post(f"/apps/finance/recipients/{rid}", data={"name": "Swiggy", "category_id": str(groceries)})
    with fin.ctx.db() as conn:
        cats = dict(conn.execute("SELECT email_id, category_id FROM transactions").fetchall())
    assert cats == {"a": health, "b": groceries, "c": groceries}


def test_second_key_for_same_recipient(fin):
    client = login(TestClient(fin.app))
    add_fixture(fin.gmail, "a", "credit_card_v1.txt", at(1))
    add_fixture(fin.gmail, "b", "refund.txt", at(2))
    fin.sync.run_sync(fin.ctx, now=at(3))
    client.post("/apps/finance/recipients", data={"name": "Amazon", "key": "amazon pay", "kind": "merchant"})
    client.post("/apps/finance/recipients", data={"name": "amazon", "key": "AMAZON"})  # same recipient, typed key
    assert count(fin.ctx, "SELECT COUNT(*) FROM recipients") == 1
    assert count(fin.ctx, "SELECT COUNT(DISTINCT recipient_id) FROM transactions") == 1
    assert count(fin.ctx, "SELECT COUNT(*) FROM recipient_keys") == 2


def seed(ctx, rows):
    """rows: (occurred_at, amount_rupees, direction, category name or None)"""
    with ctx.db() as conn:
        for i, (when, rupees, direction, cat) in enumerate(rows):
            cid = conn.execute("SELECT id FROM categories WHERE name = ?", (cat,)).fetchone()[0] if cat else None
            conn.execute(
                "INSERT INTO transactions(occurred_at, amount_paise, direction, instrument,"
                " counterparty_raw, counterparty_key, category_id) VALUES (?, ?, ?, 'upi', ?, ?, ?)",
                (when, rupees * 100, direction, f"cp{i}", f"cp{i}@upi", cid),
            )


def test_burn_and_projection_for_fixed_date(fin):
    seed(fin.ctx, [
        ("2026-10-01T10:00:00", 1000, "debit", "Food"),
        ("2026-10-05T10:00:00", 2000, "debit", None),        # uncategorised still counts
        ("2026-10-06T10:00:00", 3000, "debit", "Groceries"),
        ("2026-10-07T10:00:00", 25000, "debit", "Transfers"),  # card bill payment: not spend
        ("2026-10-08T10:00:00", 50000, "credit", None),       # salary: not spend
        ("2026-10-11T10:00:00", 999, "debit", "Food"),        # after 'today'... still this month
        ("2026-09-30T23:59:59", 700, "debit", "Food"),        # previous month
    ])
    with fin.ctx.db() as conn:
        b = stats.burn(conn, 2026, 10, date(2026, 10, 10), budget=30000_00)
        cats = stats.by_category(conn, 2026, 10)
    assert b.spent == 6999_00
    assert (b.days_elapsed, b.days_in_month) == (10, 31)
    assert b.daily_rate == round(6999_00 / 10)
    assert b.projected == round(6999_00 / 10) * 31
    assert b.remaining == 30000_00 - 6999_00
    assert dict(cats) == {"Groceries": 3000_00, "Food": 1999_00, "Uncategorised": 2000_00}
    with fin.ctx.db() as conn:
        past = stats.burn(conn, 2026, 9, date(2026, 10, 10), budget=None)
    assert (past.spent, past.projected, past.days_elapsed) == (700_00, 700_00, 30)


def test_untagged_queue_is_debits_only(fin):
    seed(fin.ctx, [("2026-10-01T10:00:00", 50000, "credit", None), ("2026-10-02T10:00:00", 10, "debit", None)])
    with fin.ctx.db() as conn:
        assert [q["key"] for q in stats.untagged(conn)] == ["cp1@upi"]
        assert stats.untagged_count(conn) == 1


def test_target_burn_rate_to_get_back_to_budget(fin):
    seed(fin.ctx, [("2026-10-01T10:00:00", 6999, "debit", "Food")])
    day10 = date(2026, 10, 10)  # 10 of 31 days gone, 21 left; rate = 699.90/day, projected 21,696.90
    with fin.ctx.db() as conn:
        ok = stats.burn(conn, 2026, 10, day10, budget=30000_00)
        over = stats.burn(conn, 2026, 10, day10, budget=9000_00)
        blown = stats.burn(conn, 2026, 10, day10, budget=5000_00)
        none = stats.burn(conn, 2026, 10, day10, budget=None)
        past = stats.burn(conn, 2026, 9, day10, budget=9000_00)
        last_day = stats.burn(conn, 2026, 10, date(2026, 10, 31), budget=9000_00)
    # on track: allowance is what's left over the days left, and no cut is needed
    assert (ok.days_left, ok.target_daily, ok.cut_pct) == (21, (30000_00 - 6999_00) // 21, None)
    # projected 21,697 > 9,000: must fall to (9000-6999)/21 = 95.28/day, a ~86% cut from 699.90
    assert over.target_daily == 2001_00 // 21 == 9528
    assert over.cut_pct == round(100 * (1 - 9528 / 69900)) == 86
    assert over.target_daily * over.days_left <= over.remaining  # never plans to overshoot
    # already over budget: nothing left to spend
    assert (blown.remaining, blown.target_daily, blown.cut_pct) == (-1999_00, 0, 100)
    # no budget, a finished month and the last day have no daily target
    assert none.target_daily is None and past.target_daily is None and last_day.target_daily is None


def test_dashboard_shows_target_burn(fin, monkeypatch):
    import hub_apps.finance.routes as routes_mod

    class Frozen(datetime):  # "today" is 10 Oct 2026, so 21 days are left
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 12, tzinfo=tz)

    monkeypatch.setattr(routes_mod, "datetime", Frozen)
    client = login(TestClient(fin.app))
    seed(fin.ctx, [("2026-10-01T10:00:00", 6999, "debit", "Food")])

    def dash(budget_rupees):
        fin.ctx.kv.set("budget_paise", budget_rupees * 100)
        return client.get("/apps/finance/").text

    html = dash(9000)  # projected far above budget: cut needed
    assert "Target daily burn" in html and "₹95" in html and "cut 86%" in html and "for 21 days" in html
    html = dash(30000)  # on track: an allowance, not a cut
    assert "Daily allowance" in html and "₹1,095" in html and "cut" not in html.split("Daily allowance")[1][:200]
    html = dash(5000)  # already over: nothing can bring it back this month
    assert "Target daily burn" in html and "already ₹1,999 over budget" in html
    fin.ctx.kv.delete("budget_paise")
    assert "Target daily burn" not in client.get("/apps/finance/").text


def test_trailing_average_needs_a_full_month(fin):
    seed(fin.ctx, [("2026-09-15T10:00:00", 9000, "debit", "Food")])
    with fin.ctx.db() as conn:
        conn.execute("INSERT INTO emails VALUES ('e', '2026-09-10T00:00:00', 's', 's', 'b', 'parsed', NULL, NULL, datetime('now'))")
        assert stats.trailing_average(conn, date(2026, 10, 3)) is None  # history starts mid-September
        conn.execute("INSERT INTO emails VALUES ('f', '2026-08-31T00:00:00', 's', 's', 'b', 'parsed', NULL, NULL, datetime('now'))")
        assert stats.trailing_average(conn, date(2026, 10, 3)) == 9000_00


def test_inr_formatting():
    assert stats.inr(123456789) == "₹12,34,567.89"
    assert stats.inr(99_950, decimals=False) == "₹1,000"
    assert stats.inr(-5000) == "-₹50.00"


def test_ai_fallback_is_local_and_masked(fin):
    fin.ollama.outputs = [{"is_transaction": True, "amount": 349, "direction": "debit",
                           "instrument": "credit_card", "account_last4": "9876",
                           "counterparty": "ZOMATO", "date": "2026-10-02"}]
    subject, body = fixture_email("unknown_format.txt")
    fin.gmail.add("z", body + " Acct 50100123456789.", at(2), subject=subject)
    result = fin.sync.run_sync(fin.ctx, now=at(3))
    assert result["parsed"] == 1
    assert fin.anthropic.calls == []
    sent = fin.ollama.calls[0][2][-1].content
    assert "50100123456789" not in sent and "6789" in sent
    with fin.ctx.db() as conn:
        t = conn.execute("SELECT * FROM transactions").fetchone()
    assert (t["amount_paise"], t["source"], t["counterparty_key"]) == (34900, "ai", "zomato")
    usage = fin.app.state.hub.ai.usage_summary()
    assert {u["provider"] for u in usage} == {"ollama"}


def test_ai_down_leaves_email_unparsed_and_sync_succeeds(fin):
    def boom(*a, **k):
        raise ConnectionError("connection refused")

    fin.ollama.extract = boom
    for i in range(3):
        subject, body = fixture_email("unknown_format.txt")
        fin.gmail.add(f"u{i}", body, at(2, 9 + i), subject=subject)
    result = fin.sync.run_sync(fin.ctx, now=at(3))
    assert result["unparsed"] == 3 and "unavailable" in result["ai_error"]
    errors = [u for u in fin.app.state.hub.ai.usage_summary() if u["errors"]]
    assert errors[0]["calls"] == 1  # model disabled after the first failure


def test_sync_when_gmail_not_connected_records_error(fin):
    fin.gmail.connected = False
    with pytest.raises(NotConnected):
        fin.sync.run_sync(fin.ctx, now=at(3))
    assert "not connected" in fin.ctx.kv.get("last_sync")["error"]


def test_review_manual_entry_and_reparse(fin):
    client = login(TestClient(fin.app))
    fin.ollama.extract = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
    subject, body = fixture_email("unknown_format.txt")
    fin.gmail.add("u1", body, at(2), subject=subject)
    fin.gmail.add("u2", body, at(2), subject=subject)
    fin.sync.run_sync(fin.ctx, now=at(3))
    page = client.get("/apps/finance/review")
    assert "ZOMATO" in page.text
    client.post("/apps/finance/review/u1/manual", data={"amount": "349", "direction": "debit",
                                                        "instrument": "credit_card", "counterparty": "Zomato", "on": "2026-10-02"})
    client.post("/apps/finance/review/u2/ignore")
    assert count(fin.ctx, "SELECT COUNT(*) FROM emails WHERE status = 'unparsed'") == 0
    assert count(fin.ctx, "SELECT source FROM transactions") == "manual"
    r = client.post("/apps/finance/review/reparse", data={})
    assert "0 parsed, 0 ignored, 0 still unparsed" in r.text  # the hand-ignored email is untouched
    assert count(fin.ctx, "SELECT status FROM emails WHERE gmail_id = 'u2'") == "ignored"


def test_pages_render(fin):
    client = login(TestClient(fin.app))
    add_fixture(fin.gmail, "m1", "upi_debit.txt", at(2))
    fin.sync.run_sync(fin.ctx, now=at(3))
    client.post("/apps/finance/settings", data={"budget_rupees": "30000", "sender_list": "a@x.com\nb@y.com"})
    assert fin.ctx.kv.get("budget_paise") == 30000_00
    assert fin.ctx.kv.get("senders") == ["a@x.com", "b@y.com"]
    for path in ["/", "/?month=2026-10", "/transactions?month=2026-10", "/recipients", "/review", "/settings"]:
        r = client.get("/apps/finance" + path)
        assert r.status_code == 200, path
    dash = client.get("/apps/finance/?month=2026-10").text
    assert "₹450" in dash and "1 recipient to tag" in dash
    status = client.get("/apps/finance/sync/status?watch=1")
    assert status.headers.get("HX-Refresh") == "true"
