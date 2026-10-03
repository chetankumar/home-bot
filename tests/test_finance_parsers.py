from datetime import date

import pytest

from apps.finance.parsers import IGNORE, mask, parse_email, to_paise
from apps.finance.tagging import counterparty_key
from tests.conftest import fixture_email

CASES = [
    # fixture, parser, amount_paise, direction, instrument, mask, counterparty, date, reference
    ("upi_debit.txt", "upi_debit", 45000, "debit", "upi", "4321", "swiggy.stores@axisbank SWIGGY", date(2026, 10, 2), "527512345678"),
    ("upi_debit_no_name.txt", "upi_debit", 12000, "debit", "upi", "4321", "paytmqr1abcd@paytm", date(2026, 10, 1), "527512345679"),
    ("upi_credit.txt", "upi_credit", 250000, "credit", "upi", "4321", "friend@okicici FRIEND NAME", date(2026, 10, 1), "527400000001"),
    ("credit_card_v1.txt", "card_spend_v1", 129900, "debit", "credit_card", "9876", "AMAZON PAY INDIA", date(2026, 10, 2), "012345"),
    ("credit_card_v2.txt", "card_spend_v2", 129900, "debit", "credit_card", "9876", "AMAZON PAY INDIA", date(2026, 10, 2), None),
    ("debit_card.txt", "card_spend_v2", 64000, "debit", "debit_card", "5555", "DMART AVENUE SUPERMARTS", date(2026, 10, 3), None),
    ("netbanking.txt", "netbanking_v1", 1500000, "debit", "netbanking", "4321", "NEFT to RENT LANDLORD", date(2026, 10, 1), None),
    ("atm.txt", "atm_withdrawal_v2", 200000, "debit", "atm", "4321", "ATM HDFC BANK KORAMANGALA", date(2026, 10, 3), None),
    ("card_payment.txt", "card_credit", 2500000, "credit", "credit_card", "9876", "Card bill payment", date(2026, 10, 5), None),
    ("refund.txt", "card_credit", 29900, "credit", "credit_card", "9876", "AMAZON", date(2026, 10, 6), None),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_parsers(case):
    name, parser, amount, direction, instrument, acct, cp, on, ref = case
    p = parse_email(*fixture_email(name))
    assert (p.parser, p.amount_paise, p.direction, p.instrument) == (parser, amount, direction, instrument)
    assert (p.account_mask, p.counterparty_raw, p.occurred_on, p.reference) == (acct, cp, on, ref)


@pytest.mark.parametrize("name", ["otp.txt", "statement.txt"])
def test_non_transactions_are_ignored(name):
    assert parse_email(*fixture_email(name)) == IGNORE


def test_unknown_format_is_left_for_fallback():
    assert parse_email(*fixture_email("unknown_format.txt")) is None


def test_amounts():
    assert to_paise("1,23,456.7") == 12345670
    assert to_paise("99") == 9900


def test_mask_hides_long_numbers_only():
    assert mask("A/c 50100123456789, card **4321, ref 5275") == "A/c XXXXXXXXXX6789, card **4321, ref 5275"


@pytest.mark.parametrize(
    "raw,instrument,expected",
    [
        ("swiggy.stores@AxisBank SWIGGY", "upi", ("swiggy.stores@axisbank", "upi")),
        ("AMAZON PAY INDIA", "credit_card", ("amazon pay", "merchant")),
        ("NEFT to RENT LANDLORD", "netbanking", ("rent landlord", "merchant")),
        ("POS 1234 DMART AVENUE PVT LTD", "debit_card", ("dmart avenue", "merchant")),
        ("ATM HDFC BANK KORAMANGALA", "atm", ("atm", "merchant")),
        (None, "upi", (None, None)),
    ],
)
def test_counterparty_key(raw, instrument, expected):
    assert counterparty_key(raw, instrument) == expected
