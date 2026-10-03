"""Parse Amazon.in order emails (auto-confirm@amazon.in) into orders and items.

Amazon's layout changes, and I have no sample of yours, so parsing is layered:
regexes for the common plain-text layout, then the subject line, then the local
model for emails that have an order number but no readable items or total. Raw
bodies are stored so improving a parser never needs a re-fetch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from hub.plugin import AppContext
from hub.services.ai import AIError

from .parsers import collapse, mask, to_paise

ORDER_NO = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")
CURRENCY = r"(?:₹|rs\.?|inr)\s*"
MONEY = re.compile(CURRENCY + r"([\d,]+(?:\.\d{1,2})?)", re.I)
TOTAL = re.compile(r"(?:order total|grand total|total amount|order value)\s*:?\s*" + CURRENCY + r"([\d,]+(?:\.\d{1,2})?)", re.I)
QTY = re.compile(r"^\s*(?:quantity|qty)\s*:?\s*(\d+)\s*$", re.I)
SUBJECT = re.compile(r"^\s*(ordered|shipped|delivered|out for delivery|cancel\w*)\s*:?\s*(.*)$", re.I | re.S)
MORE_ITEMS = re.compile(r"\s+and\s+\d+\s+more\s+items?\s*$", re.I)

# Lines that are never an item title when we look backwards from "Quantity:".
NOISE = re.compile(
    r"^(order\b|arriving|delivery|your order|ship|hello|hi\b|thank|total|item|sold by|sold|view|track|"
    r"return|payment|subtotal|grand|price|qty|quantity|[₹\d])", re.I
)
_STATUS_RANK = {"placed": 0, "shipped": 1, "delivered": 2}


@dataclass
class OrderItem:
    title: str
    quantity: int = 1
    price_paise: int | None = None


@dataclass
class ParsedOrder:
    order_number: str
    status: str = "placed"  # placed | shipped | delivered | cancelled
    total_paise: int | None = None
    items: list[OrderItem] = field(default_factory=list)
    parser: str = ""


def status_from(subject: str, body: str) -> str:
    m = SUBJECT.match(subject or "")
    word = (m.group(1).lower() if m else "")
    if word.startswith("cancel") or re.search(r"\b(has been|was) cancel", body[:600], re.I):
        return "cancelled"
    if word == "delivered":
        return "delivered"
    if word in ("shipped", "out for delivery"):
        return "shipped"
    return "placed"


def subject_title(subject: str) -> str | None:
    """'Ordered: "boAt Rockerz" and 2 more items' -> 'boAt Rockerz'."""
    m = SUBJECT.match(subject or "")
    if not m:
        return None
    title = MORE_ITEMS.sub("", m.group(2)).strip().strip("\"'“”‘’ ")
    return title or None


def _lines(body: str) -> list[str]:
    return [re.sub(r"[ \t ]+", " ", ln).strip() for ln in body.splitlines()]


def parse_items(body: str) -> list[OrderItem]:
    """Items are 'title / Quantity: n / price' triples in the plain-text layout."""
    lines = _lines(body)
    items: list[OrderItem] = []
    for i, line in enumerate(lines):
        m = QTY.match(line)
        if not m:
            continue
        title = next((ln for ln in reversed(lines[:i]) if ln), "")
        if not title or NOISE.match(title) or len(title) < 3:
            continue
        price = None
        for ahead in lines[i + 1 : i + 4]:
            pm = MONEY.search(ahead)
            if pm:
                price = to_paise(pm.group(1))
                break
        items.append(OrderItem(title[:300], int(m.group(1)), price))
    return items


def parse_total(body: str) -> int | None:
    m = TOTAL.search(body)
    return to_paise(m.group(1)) if m else None


def parse_email(subject: str, body: str) -> ParsedOrder | str | None:
    """-> ParsedOrder; 'ignore' for mail that isn't about an order; None if it should
    be an order but couldn't be read (the caller then tries the local model)."""
    text = f"{subject}\n{body}"
    m = ORDER_NO.search(text)
    if not m:
        return None if SUBJECT.match(subject or "") else "ignore"
    order = ParsedOrder(order_number=m.group(1), status=status_from(subject, body))
    order.items = parse_items(body)
    order.total_paise = parse_total(body)
    order.parser = "body"
    if not order.items:
        title = subject_title(subject)
        if title:
            order.items = [OrderItem(title)]
            order.parser = "subject"
    return order


# -- local-model fallback ----------------------------------------------------------------
SYSTEM = (
    "You read Amazon.in order emails. Set is_order to false if the email is not about a specific "
    "order (marketing, account notices). Otherwise list every item with its title, quantity and "
    "price in rupees, and the order total in rupees. Copy the order number exactly (format "
    "123-1234567-1234567)."
)


class OrderItemOut(BaseModel):
    title: str
    quantity: int = Field(default=1, ge=1)
    price: float | None = None


class OrderEmail(BaseModel):
    is_order: bool
    order_number: str | None = None
    total: float | None = Field(default=None, description="Order total in rupees")
    items: list[OrderItemOut] = Field(default_factory=list)


def ai_fill(ctx: AppContext, subject: str, body: str, order: ParsedOrder | None) -> ParsedOrder | str | None:
    """Ask the local model to read items/total. Returns the improved order, 'ignore',
    or the original (possibly None) if the model can't help. Never raises."""
    try:
        result = ctx.ai.extract(
            f"Subject: {subject}\n\n{mask(collapse_keep_lines(body))[:6000]}",
            schema=OrderEmail,
            system=SYSTEM,
            max_tokens=700,
        )
    except AIError as e:
        ctx.log.info("amazon: local model not used: %s", e)
        return order
    if not result.is_order:
        return "ignore" if order is None else order
    number = result.order_number if result.order_number and ORDER_NO.fullmatch(result.order_number) else None
    number = number or (order.order_number if order else None)
    if not number:
        return order
    out = order or ParsedOrder(order_number=number, status=status_from(subject, body))
    if result.items:
        out.items = [
            OrderItem(i.title.strip()[:300], i.quantity, to_paise(f"{i.price:.2f}") if i.price else None)
            for i in result.items
            if i.title.strip()
        ] or out.items
    if result.total and out.total_paise is None:
        out.total_paise = to_paise(f"{result.total:.2f}")
    out.parser = "ai"
    return out


def collapse_keep_lines(text: str) -> str:
    return "\n".join(collapse(ln) for ln in text.splitlines() if ln.strip())


def merge_status(old: str, new: str) -> str:
    """Cancelled is final; otherwise a status never goes backwards."""
    if "cancelled" in (old, new):
        return "cancelled"
    return new if _STATUS_RANK.get(new, 0) >= _STATUS_RANK.get(old, 0) else old
