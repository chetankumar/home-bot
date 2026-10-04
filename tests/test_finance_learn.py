"""Regex compiler: the miss log, model-proposed regexes, and approval."""

from fastapi.testclient import TestClient

from apps.finance import learn
from tests.conftest import login
from tests.test_finance import at, count, fin  # noqa: F401  (fin is a fixture)

BASE = "/apps/finance"
BODY = "Hello, a spend of INR {amt} happened at {shop} via card XX9876 today."
TXN = {"is_transaction": True, "direction": "debit", "instrument": "credit_card", "date": "2026-10-02"}
PROPOSAL = {"parsers": [{
    "name": "Card Spend v9",
    "pattern": r"a spend of inr (?P<amount>[\d,.]+) happened at (?P<merchant>.+?) via card xx(?P<acct>\d{4})",
    "direction": "debit", "instrument": "credit_card"}]}


def seed_misses(fin):  # noqa: F811
    fin.gmail.add("a", BODY.format(amt=349, shop="ZOMATO"), at(2), subject="Transaction alert")
    fin.gmail.add("b", BODY.format(amt=120, shop="SWIGGY"), at(2, 13), subject="Transaction alert")
    fin.ollama.outputs = [{**TXN, "amount": 349, "counterparty": "ZOMATO"}, {**TXN, "amount": 120, "counterparty": "SWIGGY"}]
    fin.sync.run_sync(fin.ctx, now=at(3))


def outcomes(fin, kind="bank"):  # noqa: F811
    with fin.ctx.db() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT gmail_id, outcome FROM parse_misses WHERE kind = ?", (kind,))}


def test_misses_are_logged_with_what_happened(fin):  # noqa: F811
    seed_misses(fin)
    assert outcomes(fin) == {"a": "ai_parsed", "b": "ai_parsed"}
    fin.gmail.add("c", BODY.format(amt=5, shop="X"), at(2, 14), subject="Transaction alert")
    fin.ollama.outputs = [{"is_transaction": False}]
    fin.sync.run_sync(fin.ctx, now=at(3))
    assert outcomes(fin)["c"] == "ai_ignored"


def test_regex_reads_are_not_misses(fin):  # noqa: F811
    fin.gmail.add("u", "Rs.450.00 has been debited from account 4321 to VPA a@b X on 02-10-26.", at(2))
    fin.sync.run_sync(fin.ctx, now=at(3))
    assert outcomes(fin) == {}


def test_proposal_waits_for_approval_then_reads_new_emails(fin):  # noqa: F811
    seed_misses(fin)
    client = login(TestClient(fin.app))
    fin.ollama.outputs = [PROPOSAL]
    r = client.post(f"{BASE}/parsers/propose", data={"kind": "bank"}, follow_redirects=True)
    assert "ready to review" in r.text
    sent = fin.ollama.calls[-1][2][-1].content
    assert "ZOMATO" in sent and "SWIGGY" in sent
    with fin.ctx.db() as conn:
        rule = conn.execute("SELECT * FROM learned_parsers").fetchone()
    assert (rule["status"], rule["matched"], rule["samples"]) == ("proposed", 2, 2)

    # Not used while merely proposed.
    fin.gmail.add("d", BODY.format(amt=77, shop="BLINKIT"), at(4), subject="Transaction alert")
    fin.sync.run_sync(fin.ctx, now=at(5))  # no queued model output: the call fails, email stays unparsed
    assert count(fin.ctx, "SELECT status FROM emails WHERE gmail_id = 'd'") == "unparsed"

    client.post(f"{BASE}/parsers/{rule['id']}/approve")
    with fin.ctx.db() as conn:
        d = conn.execute("SELECT status, parser FROM emails WHERE gmail_id = 'd'").fetchone()
        t = conn.execute("SELECT amount_paise, source, counterparty_raw FROM transactions WHERE email_id = 'd'").fetchone()
    assert tuple(d) == ("parsed", "learned:card_spend_v9")
    assert tuple(t) == (7700, "regex", "BLINKIT")
    assert outcomes(fin) == {}  # the earlier model-read emails are now covered by the regex


def test_bad_proposals_are_rejected(fin):  # noqa: F811
    seed_misses(fin)
    wrong_amount = PROPOSAL["parsers"][0] | {"pattern": r"happened at (?P<amount>\d+)"}
    nested = PROPOSAL["parsers"][0] | {"pattern": r"(?P<amount>(\d+)+)"}
    fin.ollama.outputs = [{"parsers": [wrong_amount, nested]}]
    assert "didn't hold up" in learn.propose(fin.ctx, "bank")
    assert count(fin.ctx, "SELECT COUNT(*) FROM learned_parsers") == 0


def test_amazon_total_regex(fin):  # noqa: F811
    from apps.finance import orders as orders_mod

    body = "Order # 402-1234567-8901234\nSteel bottle\nQuantity: 1\nYou paid ₹ 899.00 today"
    fin.gmail.add("o1", body, at(2), subject='Ordered: "Steel bottle"', sender="auto-confirm@amazon.in")
    orders_mod.sync_orders(fin.ctx, at(3))
    assert outcomes(fin, "amazon") == {"o1": "ai_failed"}
    fin.ollama.outputs = [{"name": "paid", "total_pattern": r"you paid\s*₹\s*(?P<total>[\d,.]+)", "item_pattern": None}]
    assert "ready to review" in learn.propose(fin.ctx, "amazon")
    client = login(TestClient(fin.app))
    with fin.ctx.db() as conn:
        rid = conn.execute("SELECT id FROM learned_parsers").fetchone()[0]
    fin.gmail.add("o2", body.replace("402-1234567-8901234", "402-7654321-1098765"), at(4),
                  subject='Ordered: "Steel bottle"', sender="auto-confirm@amazon.in")
    client.post(f"{BASE}/parsers/{rid}/approve")
    orders_mod.sync_orders(fin.ctx, at(5))
    with fin.ctx.db() as conn:
        totals = [(r[0], r[1]) for r in conn.execute("SELECT total_paise, total_source FROM orders ORDER BY id")]
    assert totals == [(89900, "email"), (89900, "email")]
    assert outcomes(fin, "amazon") == {}


def test_parsers_page_renders(fin):  # noqa: F811
    seed_misses(fin)
    html = login(TestClient(fin.app)).get(f"{BASE}/parsers").text
    assert "HDFC alerts" in html and "2 email(s) missed" in html
