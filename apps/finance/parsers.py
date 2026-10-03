"""Regex parsers for HDFC Bank transaction alert emails.

HDFC's wording has changed over the years, so each parser targets one
family of alerts and they are tried in order. Raw bodies are kept in the
database, so parsers can be tightened and re-run without re-fetching.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

IGNORE = "ignore"  # sentinel: not a transaction (OTP, statement, reminder...)


@dataclass
class Parsed:
    amount_paise: int
    direction: str  # debit | credit
    instrument: str  # upi | credit_card | debit_card | netbanking | atm
    account_mask: str | None = None  # last 4 digits only
    counterparty_raw: str | None = None
    reference: str | None = None
    occurred_on: date | None = None
    parser: str = ""


# -- building blocks -----------------------------------------------------------------
AMT = r"(?:rs\.?|inr|₹)\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)"
DATE = (
    r"(?P<date>\d{1,2}[-/]\d{1,2}[-/]\d{2,4}"
    r"|\d{1,2}[- ][a-z]{3,9},?[- ]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2})"
)
ACCT = r"(?:\*+|x+)?\s?(?P<acct>\d{4})"
ACCOUNT = r"(?:your )?(?:hdfc bank )?(?:account|a/c)(?: no\.?)? " + ACCT
VPA = r"(?P<vpa>[\w.\-]+@[\w.\-]+)"
TO_DOT = r"(?=\.(?:\s|$)|$)"

REFERENCE_RES = [
    re.compile(r"reference number is:?\s*(\w+)", re.I),
    re.compile(r"\bref(?:erence)?\.? ?(?:no\.?|number)?:?\s*(\d{6,})", re.I),
    re.compile(r"authori[sz]ation code:?-?\s*(\w+)", re.I),
]

IGNORE_RE = re.compile(
    r"\bOTP\b|one[- ]time password|e-?statement|\bstatement\b|payment due|is due on|"
    r"minimum amount due|logged ?in|login|password|beneficiary|kyc|reward points",
    re.I,
)

_DATE_FORMATS = [
    "%d-%m-%y", "%d-%m-%Y", "%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d",
    "%d %b, %Y", "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %B, %Y", "%d %B %Y", "%d %b, %y",
]


def to_paise(amount: str) -> int:
    try:
        value = Decimal(amount.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"bad amount {amount!r}") from None
    return int((value * 100).quantize(Decimal(1)))


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip()).title()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def find_reference(text: str) -> str | None:
    for rx in REFERENCE_RES:
        m = rx.search(text)
        if m:
            return m.group(1)
    return None


def clean_name(name: str | None) -> str | None:
    if not name:
        return None
    name = re.sub(r"\s+", " ", name).strip(" .,:-")
    return name or None


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# -- parsers ---------------------------------------------------------------------------
Builder = Callable[[re.Match[str], str], Parsed]


def _base(m: re.Match[str], direction: str, instrument: str, cp: str | None, text: str) -> Parsed:
    g = m.groupdict()
    return Parsed(
        amount_paise=to_paise(g["amount"]),
        direction=direction,
        instrument=instrument,
        account_mask=g.get("acct"),
        counterparty_raw=clean_name(cp),
        reference=find_reference(text),
        occurred_on=parse_date(g.get("date")),
    )


def _upi(direction: str) -> Builder:
    def build(m, text):
        cp = " ".join(x for x in (m.group("vpa"), clean_name(m.group("name"))) if x)
        return _base(m, direction, "upi", cp, text)

    return build


def _card(direction: str) -> Builder:
    def build(m, text):
        instrument = "credit_card" if m.group("card").lower() == "credit" else "debit_card"
        g = m.groupdict()
        cp = g.get("merchant") or ("Card bill payment" if direction == "credit" else None)
        return _base(m, direction, instrument, cp, text)

    return build


def _account(direction: str, instrument: str) -> Builder:
    def build(m, text):
        cp = m.groupdict().get("merchant")
        if instrument == "atm" and not (cp or "").upper().startswith("ATM"):
            cp = " ".join(x for x in ("ATM", cp) if x)
        return _base(m, direction, instrument, cp, text)

    return build


PARSERS: list[tuple[str, re.Pattern[str], Builder]] = [
    (
        "upi_debit",
        re.compile(
            AMT + r" (?:has been |is )?debited from " + ACCOUNT + r" to vpa " + VPA
            + r"(?: (?P<name>.*?))? on " + DATE, re.I),
        _upi("debit"),
    ),
    (
        "upi_credit",
        re.compile(
            AMT + r" (?:has been |is )?(?:successfully )?credited to " + ACCOUNT + r" by vpa "
            + VPA + r"(?: (?P<name>.*?))? on " + DATE, re.I),
        _upi("credit"),
    ),
    (
        # Older: "Thank you for using your HDFC Bank Credit Card ending 1234 for Rs 500.00 at X on 03-10-2026 14:22:10"
        "card_spend_v1",
        re.compile(
            r"using (?:your )?hdfc bank (?P<card>credit|debit) card ending " + ACCT + r" for "
            + AMT + r" at (?P<merchant>.+?) on " + DATE, re.I),
        _card("debit"),
    ),
    (
        # Newer: "Rs.500.00 is debited from your HDFC Bank Credit Card ending 1234 towards X on 03 Oct, 2026"
        "card_spend_v2",
        re.compile(
            AMT + r" (?:has been |is )?(?:debited|spent) (?:from|on) (?:your )?hdfc bank "
            r"(?P<card>credit|debit) card ending " + ACCT + r" (?:towards|at) (?P<merchant>.+?) on "
            + DATE, re.I),
        _card("debit"),
    ),
    (
        "atm_withdrawal_v1",
        re.compile(
            r"(?P<card>debit) card ending " + ACCT + r" for atm withdrawal for " + AMT
            + r"(?: in (?P<merchant>.+?))?(?: at .+?)? on " + DATE, re.I),
        _account("debit", "atm"),
    ),
    (
        "atm_withdrawal_v2",
        re.compile(
            AMT + r" (?:has been |is )?withdrawn from " + ACCOUNT
            + r".*?(?: at (?P<merchant>atm.+?))? on " + DATE, re.I),
        _account("debit", "atm"),
    ),
    (
        "card_credit",
        re.compile(
            AMT + r"(?: from (?P<merchant>.+?))? (?:has been |is )?credited to (?:your )?hdfc bank "
            r"(?P<card>credit|debit) card ending " + ACCT + r"(?: on " + DATE + ")?", re.I),
        _card("credit"),
    ),
    (
        "netbanking_v1",
        re.compile(
            AMT + r" (?:has been |is )?debited from " + ACCOUNT
            + r" (?:towards|to|for) (?P<merchant>.+?) on " + DATE, re.I),
        _account("debit", "netbanking"),
    ),
    (
        "netbanking_v2",
        re.compile(
            r"netbanking transaction of " + AMT + r" from " + ACCOUNT
            + r" to (?P<merchant>.+?) on " + DATE, re.I),
        _account("debit", "netbanking"),
    ),
    (
        "account_credit",
        re.compile(
            AMT + r" (?:has been |is )?(?:successfully )?(?:credited|deposited) (?:to|in) "
            + ACCOUNT + r"(?: on " + DATE + r")?(?: (?:by|from) (?P<merchant>.+?)" + TO_DOT + ")?",
            re.I),
        _account("credit", "netbanking"),
    ),
]


def parse_email(subject: str, body: str) -> Parsed | str | None:
    """Return Parsed for a transaction, IGNORE for non-transaction mail, None if unsure."""
    text = collapse(body)
    for name, rx, build in PARSERS:
        m = rx.search(text)
        if m:
            try:
                parsed = build(m, text)
            except ValueError:
                continue
            if parsed.amount_paise <= 0:
                continue
            parsed.parser = name
            return parsed
    if IGNORE_RE.search(subject) or IGNORE_RE.search(text[:400]):
        return IGNORE
    return None


# -- masking -----------------------------------------------------------------------------
_LONG_DIGITS = re.compile(r"\d[\d ]{6,}\d")


def mask(text: str) -> str:
    """Hide long digit runs (account/card numbers, phone numbers) except the last 4.

    Used before handing an email to a model, even a local one.
    """

    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 8:
            return m.group(0)
        return "X" * (len(digits) - 4) + digits[-4:]

    return _LONG_DIGITS.sub(repl, text)
