"""Regex compiler: the miss log, model-proposed regexes, and approval."""

import json

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


def reply(answer, analysis="Comparing the emails."):
    """What the model sends back: its thinking, then the answer as JSON."""
    return f"<analysis>{analysis}</analysis>\n<json>{json.dumps(answer)}</json>"


def say(fin, *answers):  # noqa: F811
    """Queue the model's next replies (each an answer dict or a raw string)."""
    fin.ollama.texts = [a if isinstance(a, str) else reply(a) for a in answers]


def asked(fin):  # noqa: F811
    """The prompts sent to the model by the regex compiler so far (complete() calls)."""
    return [c[2] for c in fin.ollama.calls if c[0] == "complete"]


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
    say(fin, PROPOSAL)
    assert client.post(f"{BASE}/parsers/propose", data={"kind": "bank"}, follow_redirects=False).status_code == 303
    for _ in range(200):  # the button started the job in the background; wait for it
        if not fin.ctx.scheduler.is_running("learn_bank"):
            break
        time.sleep(0.05)
    assert "Proposal ready after 1 reply" in client.get(f"{BASE}/parsers").text
    sent = asked(fin)[-1][-1].content
    assert "ZOMATO" in sent and "SWIGGY" in sent
    with fin.ctx.db() as conn:
        rule = conn.execute("SELECT * FROM learned_parsers WHERE builtin = 0").fetchone()
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
    say(fin, {"parsers": [wrong_amount, nested]}, PROPOSAL)
    assert "ready to review" in learn.propose(fin.ctx, "bank")
    # Only the second answer was good; the first two proposals were never saved.
    assert count(fin.ctx, "SELECT COUNT(*) FROM learned_parsers WHERE builtin = 0") == 1


def test_amazon_total_regex(fin):  # noqa: F811
    from apps.finance import orders as orders_mod

    body = "Order # 402-1234567-8901234\nSteel bottle\nQuantity: 1\nYou paid ₹ 899.00 today"
    fin.gmail.add("o1", body, at(2), subject='Ordered: "Steel bottle"', sender="auto-confirm@amazon.in")
    orders_mod.sync_orders(fin.ctx, at(3))
    assert outcomes(fin, "amazon") == {"o1": "ai_failed"}
    say(fin, {"name": "paid", "total_pattern": r"you paid\s*₹\s*(?P<total>[\d,.]+)", "item_pattern": None})
    assert "ready to review" in learn.propose(fin.ctx, "amazon")
    client = login(TestClient(fin.app))
    with fin.ctx.db() as conn:
        rid = conn.execute("SELECT id FROM learned_parsers WHERE builtin = 0").fetchone()[0]
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


# -- scorecard ------------------------------------------------------------------------------
import random  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from apps.finance import amazon, parsers  # noqa: E402
from tests.conftest import fixture_email  # noqa: E402
from tests.test_finance_parsers import CASES  # noqa: E402


def scores(fin, kind="bank"):  # noqa: F811
    with fin.ctx.db() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT name, matches FROM learned_parsers WHERE kind = ?", (kind,))}


def test_builtins_are_seeded_once(fin):  # noqa: F811
    with fin.ctx.db() as conn:
        for _ in range(2):
            assert len(learn.bank_rules(conn)) == len(parsers.BUILTIN_SPECS)
            assert len(learn.amazon_rules(conn)) == len(amazon.BUILTIN_TOTALS)
    assert count(fin.ctx, "SELECT COUNT(*) FROM learned_parsers WHERE builtin = 1") == 12


def test_a_match_scores_the_regex_that_read_the_email(fin):  # noqa: F811
    fin.gmail.add("u", "Rs.450.00 has been debited from account 4321 to VPA a@b X on 02-10-26.", at(2))
    fin.gmail.add("v", "Rs.90.00 has been debited from account 4321 to VPA c@d Y on 02-10-26.", at(2, 13))
    fin.sync.run_sync(fin.ctx, now=at(3))
    s = scores(fin)
    assert s["upi_debit"] == 2 and sum(s.values()) == 2
    assert count(fin.ctx, "SELECT last_matched_at IS NOT NULL FROM learned_parsers WHERE name = 'upi_debit'")


def test_model_reads_and_misses_do_not_score(fin):  # noqa: F811
    seed_misses(fin)  # two emails only the model could read
    assert sum(scores(fin).values()) == 0


def test_rules_load_best_score_first(fin):  # noqa: F811
    with fin.ctx.db() as conn:
        assert [r.name for r in learn.bank_rules(conn)][:2] == ["upi_debit", "upi_credit"]  # ties keep seed order
        conn.execute("UPDATE learned_parsers SET matches = 5 WHERE name = 'atm_withdrawal_v2'")
        conn.execute("UPDATE learned_parsers SET matches = 9 WHERE name = 'netbanking_v2'")
        assert [r.name for r in learn.bank_rules(conn)][:3] == ["netbanking_v2", "atm_withdrawal_v2", "upi_debit"]
        conn.execute("UPDATE learned_parsers SET status = 'disabled' WHERE name = 'netbanking_v2'")
        assert "netbanking_v2" not in [r.name for r in learn.bank_rules(conn)]


def test_learned_names_stay_unique(fin):  # noqa: F811
    seed_misses(fin)
    say(fin, {"parsers": [PROPOSAL["parsers"][0] | {"name": "upi_debit"}]})
    learn.propose(fin.ctx, "bank")
    assert "upi_debit_2" in scores(fin)


@pytest.mark.parametrize("seed", range(6))
def test_bank_fixtures_read_the_same_in_any_score_order(seed):
    """Rules are tried best-score first, so no alert may depend on which one comes first."""
    rules = list(parsers.BUILTIN_RULES)
    random.Random(seed).shuffle(rules) if seed else rules.reverse()
    for case in CASES:
        want = parsers.parse_email(*fixture_email(case[0]))
        got = parsers.parse_email(*fixture_email(case[0]), rules)
        assert got == want, (case[0], seed)


@pytest.mark.parametrize("seed", range(4))
def test_amazon_totals_read_the_same_in_any_score_order(seed):
    rules = list(amazon.DEFAULT_RULES)
    rules.reverse() if seed % 2 == 0 else None
    for text, want in [
        ("Total: ₹1,299.00", 129900), ("Amount payable ₹450", 45000), ("Order Total: ₹99.00\nTotal: ₹5", 9900),
        ("Total: ₹5\nOrder Total: ₹99.00", 9900), ("Subtotal: ₹500.00", None), ("Item total ₹500", 50000),
        ("Total before tax: ₹500.00", None),
    ]:
        assert amazon.parse_total(text, rules) == want, text


def test_scorecard_page_lists_builtins_and_can_disable(fin):  # noqa: F811
    client = login(TestClient(fin.app))
    html = client.get(f"{BASE}/parsers").text
    assert "Scorecard" in html and "upi_debit" in html and "total_labelled" in html and "built-in" in html
    with fin.ctx.db() as conn:
        rid = conn.execute("SELECT id FROM learned_parsers WHERE name = 'upi_debit'").fetchone()[0]
    client.post(f"{BASE}/parsers/{rid}/disable")
    assert scores(fin) and count(fin.ctx, "SELECT status FROM learned_parsers WHERE id = ?", rid) == "disabled"
    client.post(f"{BASE}/parsers/{rid}/enable")
    assert count(fin.ctx, "SELECT status FROM learned_parsers WHERE id = ?", rid) == "active"


# -- negotiation: fresh short prompts, concrete feedback, repeat detection ------------------------
BAD = PROPOSAL["parsers"][0] | {"pattern": r"this wording is not in the emails (?P<amount>\d+)"}


def last_negotiation(fin, kind="bank"):  # noqa: F811
    with fin.ctx.db() as conn:
        return dict(conn.execute("SELECT * FROM regex_negotiations WHERE kind = ? ORDER BY id DESC", (kind,)).fetchone())


def test_failure_goes_back_in_a_fresh_prompt_not_a_longer_chat(fin):  # noqa: F811
    seed_misses(fin)
    say(fin, {"parsers": [BAD]}, PROPOSAL)
    assert "2 model replies" in learn.negotiate(fin.ctx, "bank")
    first, second = asked(fin)
    assert [m.role for m in first] == ["system", "user"] and [m.role for m in second] == ["system", "user"]
    text = second[-1].content
    assert "SWIGGY" in text and "ZOMATO" in text  # the emails are re-sent
    assert "Regex failed on email 1" in text and "matched 0 of 2" in text
    assert "this wording is not in the emails" in text  # the previous attempt and the "already failed" list
    n = last_negotiation(fin)
    assert (n["status"], n["replies"]) == ("succeeded", 2)
    assert count(fin.ctx, "SELECT COUNT(*) FROM learned_parsers WHERE builtin = 0 AND status = 'proposed'") == 1


def test_prompt_never_grows_with_the_number_of_failures(fin):  # noqa: F811
    seed_misses(fin)
    say(fin, *[{"parsers": [BAD | {"pattern": rf"attempt {i} (?P<amount>\d+)"}]} for i in range(8)], PROPOSAL)
    learn.negotiate(fin.ctx, "bank")
    sizes = [len(p[-1].content) for p in asked(fin)]
    assert max(sizes[2:]) < sizes[1] + 600  # later prompts are about as long as the second one
    assert all(len(p) == 2 for p in asked(fin))  # never any replayed assistant turns


def test_feedback_shows_where_the_pattern_breaks(fin):  # noqa: F811
    seed_misses(fin)
    near = PROPOSAL["parsers"][0] | {"pattern": r"a spend of inr (?P<amount>[\d,.]+) occurred at (?P<merchant>.+?) via"}
    say(fin, {"parsers": [near]}, PROPOSAL)
    learn.negotiate(fin.ctx, "bank")
    text = asked(fin)[-1][-1].content
    assert 'matches up to "...a spend of INR 349 ' in text or 'matches up to "...a spend of INR 349"' in text
    assert "happened at" in text  # what the email has where the pattern wanted 'occurred at'


@pytest.mark.parametrize("pattern,fragment", [
    (r"(?P<amount>(\d+)+)", "nests quantifiers"),
    (r"(?P<amount>[", "not a valid Python regex"),
    (r"inr (\d+)", "no named group (?P<amount>"),
    (r"happened at (?P<amount>\d+)", "matched 0 of 2"),
])
def test_feedback_names_the_specific_problem(fin, pattern, fragment):  # noqa: F811
    seed_misses(fin)
    say(fin, {"parsers": [PROPOSAL["parsers"][0] | {"pattern": pattern}]}, PROPOSAL)
    learn.negotiate(fin.ctx, "bank")
    assert fragment in asked(fin)[-1][-1].content


def test_amount_that_disagrees_with_the_earlier_reading_is_flagged(fin):  # noqa: F811
    seed_misses(fin)  # the model read 349 and 120
    off = PROPOSAL["parsers"][0] | {"pattern": r"a spend of inr \d(?P<amount>\d+) happened"}  # drops a digit
    say(fin, {"parsers": [off]}, PROPOSAL)
    learn.negotiate(fin.ctx, "bank")
    assert "your amount was 49 but it is 349" in asked(fin)[-1][-1].content


def test_a_repeated_answer_raises_the_temperature_and_narrows_to_one_email(fin):  # noqa: F811
    seed_misses(fin)
    other = BAD | {"pattern": r"something else (?P<amount>\d+)"}
    say(fin, {"parsers": [BAD]}, {"parsers": [BAD]}, {"parsers": [BAD]}, {"parsers": [other]}, PROPOSAL)
    learn.negotiate(fin.ctx, "bank")
    temps = [t for t in fin.ollama.temps]
    assert temps == [0.2, 0.2, 0.45, 0.7, 0.2]  # up for each repeat, back down after a new answer
    prompts = [p[-1].content for p in asked(fin)]
    assert "same answer" not in prompts[1]  # the first answer was not a repeat yet
    assert "same answer 2 times" in prompts[2] and "same answer 3 times" in prompts[3]
    assert "Focus on this one email" in prompts[2] and prompts[2].count("--- email") == 1
    assert "Focus on this one email" not in prompts[4]  # the new answer ended the isolation


def test_returning_to_an_earlier_failed_answer_counts_as_a_repeat(fin):  # noqa: F811
    seed_misses(fin)
    other = BAD | {"pattern": r"something else (?P<amount>\d+)"}
    say(fin, {"parsers": [BAD]}, {"parsers": [other]}, {"parsers": [BAD]}, PROPOSAL)
    learn.negotiate(fin.ctx, "bank")
    assert fin.ollama.temps == [0.2, 0.2, 0.2, 0.45]  # A, B, then A again: the repeat shows in the 4th call
    assert "Patterns that already failed" in asked(fin)[-1][-1].content


def test_a_model_that_only_varies_when_warmed_up_gets_out_of_the_loop(fin):  # noqa: F811
    """The real failure: at low temperature the model gives the identical answer forever."""
    seed_misses(fin)

    class Stubborn(list):
        def pop(self, _i=0):
            t = fin.ollama.temps[-1]
            return reply({"parsers": [BAD]}) if t < 0.6 else reply(PROPOSAL)

    fin.ollama.texts = Stubborn([0])  # non-empty; every pop() consults the temperature just used
    assert "ready to review" in learn.negotiate(fin.ctx, "bank")
    assert last_negotiation(fin)["replies"] <= 4


@pytest.mark.parametrize("text", [
    '<analysis>x</analysis><json>{"parsers": []}</json>',
    '<analysis>x</analysis>\n```json\n{"parsers": []}\n```',
    'Sure!\n{"parsers": []}',
    '<json>{"parsers": []}',  # cut off before the closing tag
])
def test_reply_formats(text):
    analysis, got, problem = learn.parse_reply(text, learn.BankProposals)
    assert got is not None and got.parsers == [] and problem is None


def test_an_unreadable_reply_is_feedback_not_a_crash(fin):  # noqa: F811
    seed_misses(fin)
    say(fin, "I cannot help with that.", '<json>{"parsers": "no"}</json>', PROPOSAL)
    assert "3 model replies" in learn.negotiate(fin.ctx, "bank")
    assert "found no JSON" in asked(fin)[1][-1].content
    assert "not valid for the required shape" in asked(fin)[2][-1].content


def test_the_analysis_is_kept_with_each_round(fin):  # noqa: F811
    seed_misses(fin)
    fin.ollama.texts = [reply({"parsers": [BAD]}, analysis="Email 1 says spend."), reply(PROPOSAL)]
    learn.negotiate(fin.ctx, "bank")
    with fin.ctx.db() as conn:
        rounds = json.loads(conn.execute("SELECT transcript FROM regex_negotiations").fetchone()[0])
    assert [r["n"] for r in rounds] == [1, 2] and "Email 1 says spend." in rounds[0]["reply"]
    assert rounds[0]["temperature"] == 0.2
    assert "--- email 1 ---" in rounds[0]["prompt"] and "--- email 1 ---" not in rounds[1]["prompt"]  # samples stored once


def test_break_point_finds_the_failing_part():
    text = "rs 500 debited from account 1234 on 02-10-26"
    out = learn.break_point(r"rs (\d+) debited from account (\d+) to vpa (\S+)", text)
    assert "debited from account 1234" in out and "to vpa" in out and "on 02-10-26" in out
    assert "Not even the start" in learn.break_point(r"completely different words", text)


def test_it_gives_up_after_twenty_replies_and_the_dashboard_says_so(fin):  # noqa: F811
    seed_misses(fin)
    say(fin, *[{"parsers": [BAD]}] * 25)
    before = len(asked(fin))
    assert "20 replies" in learn.negotiate(fin.ctx, "bank")
    assert len(asked(fin)) - before == 20 and len(fin.ollama.texts) == 5
    n = last_negotiation(fin)
    assert (n["status"], n["replies"], n["dismissed"]) == ("failed", 20, 0)
    assert count(fin.ctx, "SELECT COUNT(*) FROM learned_parsers WHERE builtin = 0") == 0
    assert max(fin.ollama.temps) == 0.9  # the stuck loop was pushed as far as it goes

    client = login(TestClient(fin.app))
    html = client.get(f"{BASE}/").text
    assert "gave up on HDFC alerts after 20 model replies" in html
    assert f"#neg-{n['id']}" in html
    assert "gave up" in client.get(f"{BASE}/parsers").text
    client.post(f"{BASE}/parsers/negotiations/{n['id']}/dismiss", data={"to": "/"})
    assert "gave up on HDFC" not in client.get(f"{BASE}/").text


def test_success_clears_an_older_failure(fin):  # noqa: F811
    seed_misses(fin)
    say(fin, *[{"parsers": [BAD]}] * 20)
    learn.negotiate(fin.ctx, "bank")
    say(fin, PROPOSAL)
    learn.negotiate(fin.ctx, "bank")
    assert "gave up" not in login(TestClient(fin.app)).get(f"{BASE}/").text


def test_unreachable_model_is_reported_but_is_not_giving_up(fin):  # noqa: F811
    seed_misses(fin)
    fin.ollama.texts = []  # FakeProvider raises when scripted with nothing
    assert "not available" in learn.negotiate(fin.ctx, "bank")
    n = last_negotiation(fin)
    assert (n["status"], n["replies"]) == ("unavailable", 0)
    html = login(TestClient(fin.app)).get(f"{BASE}/").text
    assert "could not reach the local model" in html and "gave up" not in html


def test_amazon_negotiation(fin):  # noqa: F811
    from apps.finance import orders as orders_mod

    body = "Order # 402-1234567-8901234\nSteel bottle\nQuantity: 1\nYou paid ₹ 899.00 today"
    fin.gmail.add("o1", body, at(2), subject='Ordered: "Steel bottle"', sender="auto-confirm@amazon.in")
    orders_mod.sync_orders(fin.ctx, at(3))
    wrong = {"name": "paid", "total_pattern": r"you owe ₹\s*(?P<total>[\d,.]+)", "item_pattern": None}
    right = {"name": "paid", "total_pattern": r"you paid\s*₹\s*(?P<total>[\d,.]+)", "item_pattern": None}
    say(fin, wrong, right)
    assert "2 model replies" in learn.negotiate(fin.ctx, "amazon")
    assert "Regex failed on email 1" in asked(fin)[-1][-1].content


def test_status_partial_reports_progress_and_reloads_when_done(fin):  # noqa: F811
    seed_misses(fin)
    client = login(TestClient(fin.app))
    html = client.get(f"{BASE}/parsers/status/bank").text
    assert "Ask the model for regexes" in html and "hx-get" not in html  # idle: no polling
    with fin.ctx.db() as conn:
        conn.execute("INSERT INTO regex_negotiations(kind, status, replies) VALUES ('bank', 'running', 7)")
    html = client.get(f"{BASE}/parsers/status/bank").text  # a run row with no live job: shown as interrupted
    assert "gave up" in html
    r = client.get(f"{BASE}/parsers/status/bank?watch=1", headers={"HX-Request": "true"})
    assert r.headers.get("HX-Refresh") == "true"
