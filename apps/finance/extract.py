"""Local-model fallback for alerts no regex understands.

Numbers are masked first. The host's AI policy pins this app to local model
servers (LM Studio / Ollama), so even a misconfigured call can't send a bank
email to a cloud provider.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from hub.plugin import AppContext

from .parsers import IGNORE, Parsed, mask, parse_date, to_paise

SYSTEM = (
    "You read Indian bank alert emails and extract a single transaction. "
    "If the email is not about money moving (OTP, statement, marketing, reminder), "
    "set is_transaction to false and leave the other fields null. "
    "amount is in rupees as a number. direction is 'debit' if money left the account "
    "or card, 'credit' if it arrived. counterparty is the merchant, payee, or UPI id. "
    "date is YYYY-MM-DD."
)


class ParsedTxn(BaseModel):
    is_transaction: bool
    amount: float | None = Field(default=None, description="Rupees, e.g. 1299.50")
    direction: Literal["debit", "credit"] | None = None
    instrument: Literal["upi", "credit_card", "debit_card", "netbanking", "atm"] | None = None
    account_last4: str | None = Field(default=None, description="Last 4 digits of account/card")
    counterparty: str | None = None
    date: str | None = Field(default=None, description="YYYY-MM-DD")
    reference: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> ParsedTxn:
        if self.is_transaction and (not self.amount or self.amount <= 0 or not self.direction):
            raise ValueError("a transaction needs a positive amount and a direction")
        return self


def ai_parse(ctx: AppContext, subject: str, body: str) -> Parsed | str:
    """Raises hub AIError subclasses on failure; callers decide what to do."""
    result = ctx.ai.extract(
        f"Subject: {subject}\n\n{mask(body)[:4000]}",
        schema=ParsedTxn,
        system=SYSTEM,
        max_tokens=400,
    )
    if not result.is_transaction:
        return IGNORE
    last4 = "".join(c for c in (result.account_last4 or "") if c.isdigit())[-4:] or None
    occurred: date | None = parse_date(result.date) if result.date else None
    return Parsed(
        amount_paise=to_paise(f"{result.amount:.2f}"),
        direction=result.direction,
        instrument=result.instrument or ("upi" if "@" in (result.counterparty or "") else "netbanking"),
        account_mask=last4,
        counterparty_raw=(result.counterparty or "").strip() or None,
        reference=result.reference,
        occurred_on=occurred,
        parser="ai",
    )
