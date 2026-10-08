"""Parse Amazon.in order emails (auto-confirm@amazon.in) into orders and items.

Amazon's layout changes, and I have no sample of yours, so parsing is layered:
regexes for the common plain-text layout, then the subject line, then the local
model for emails that have an order number but no readable items or total. Raw
bodies are stored so improving a parser never needs a re-fetch.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from hub.plugin import AppContext
from hub.services.ai import AIError

from .parsers import clean_name, collapse, mask, to_paise

ORDER_NO = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")
CURRENCY = r"(?:₹|(?<![a-z])(?:rs\.?|inr))\s*"  # not the "rs" inside "orders"
AMOUNT = r"(\d[\d,]*(?:\.\d{1,2})?)"  # starts with a digit: never a bare comma
MONEY = re.compile(CURRENCY + AMOUNT, re.I)
# Order-total rules. They live in the database like every other regex (learn.py seeds these two
# and learned ones join them), tried best-score first, so they must not overlap: the loose rule
# stands down when an explicit label is present. "Subtotal" and "Item total"-style lines are not
# the amount charged, so the labelled rule never matches them.
AMOUNT_TOTAL = r"(?P<total>\d[\d,]*(?:\.\d{1,2})?)"
_LABELLED = r"(?:order total|grand total|total amount|order value)\s*:?\s*"
TOTAL_LABELLED = _LABELLED + CURRENCY + AMOUNT_TOTAL
TOTAL_LOOSE = (
    r"(?s)\A(?!.*?" + _LABELLED + CURRENCY + r"\d).*?"
    r"(?:amount payable|amount to be paid|total payable|payment total|(?<![a-z])(?<!sub)total)\s*:?\s*"
    + CURRENCY + AMOUNT_TOTAL
)
BUILTIN_TOTALS = [("total_labelled", TOTAL_LABELLED), ("total_loose", TOTAL_LOOSE)]
QTY = re.compile(r"^\s*(?:quantity|qty)\s*:?\s*(\d+)\s*$", re.I)
SUBJECT = re.compile(r"^\s*(?:item\s+)?(ordered|shipped|delivered|out for delivery|cancel\w*)\s*:?\s*(.*)$", re.I | re.S)
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
class AmazonRule:
    """One row of learned_parsers for Amazon: a total pattern, an item pattern, or both."""

    id: int
    name: str
    total: re.Pattern[str] | None
    items: re.Pattern[str] | None
    builtin: bool = False


DEFAULT_RULES = [AmazonRule(0, n, re.compile(p, re.I), None, True) for n, p in BUILTIN_TOTALS]


@dataclass
class ParsedOrder:
    order_number: str
    status: str = "placed"  # placed | shipped | delivered | cancelled
    total_paise: int | None = None
    total_source: str | None = None  # email | ai | items (estimated from item prices)
    items: list[OrderItem] = field(default_factory=list)
    parser: str = ""
    rules_hit: list[str] = field(default_factory=list)  # names of the regex rules that read it


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


def parse_total(body: str, rules: Sequence[AmazonRule] | None = None) -> int | None:
    return find_total(body, rules)[0]


def find_total(body: str, rules: Sequence[AmazonRule] | None = None) -> tuple[int | None, str | None]:
    """(total in paise, name of the rule that found it). Any rule may win."""
    flat = collapse(body)
    for rule in DEFAULT_RULES if rules is None else rules:
        m = rule.total.search(flat) if rule.total else None
        if m:
            try:
                return to_paise(m.group("total")), rule.name
            except ValueError:
                continue
    return None, None


def amazon_items(rule: AmazonRule, body: str) -> list[OrderItem]:
    out: list[OrderItem] = []
    if not rule.items:
        return out
    for m in rule.items.finditer(body):
        g = m.groupdict()
        title = clean_name(g.get("title"))
        if not title or len(title) < 3:
            continue
        try:
            price = to_paise(g["price"]) if g.get("price") else None
            qty = int(g["qty"]) if g.get("qty") else 1
        except ValueError:
            price, qty = None, 1
        out.append(OrderItem(title[:300], max(qty, 1), price))
    return out[:30]


def estimate_total(items: list[OrderItem]) -> int | None:
    """Sum of item prices, when every item has one and each is a single unit.

    For quantity > 1 it is unclear whether the price is per unit or for the line, so no
    estimate is made (timing can still match the order).
    """
    if not items or any(i.price_paise is None or i.quantity != 1 for i in items):
        return None
    return sum(i.price_paise for i in items)


def parse_email(subject: str, body: str, rules: Sequence[AmazonRule] | None = None) -> ParsedOrder | str | None:
    """-> ParsedOrder; 'ignore' for mail that isn't about an order; None if it should
    be an order but couldn't be read (the caller then tries the local model).

    `rules` come from the database, best-scoring first (learn.amazon_rules); without them the
    built-in total rules are used."""
    text = f"{subject}\n{body}"
    m = ORDER_NO.search(text)
    if not m:
        return None if SUBJECT.match(subject or "") else "ignore"
    order = ParsedOrder(order_number=m.group(1), status=status_from(subject, body))
    order.total_paise, hit = find_total(body, rules)
    order.total_source = "email" if order.total_paise is not None else None
    if hit:
        order.rules_hit.append(hit)
    order.items = parse_items(body)  # the line-by-line reader is code, not a regex rule
    if not order.items:
        for rule in rules or ():
            order.items = amazon_items(rule, body)
            if order.items:
                order.rules_hit.append(rule.name)
                break
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
        out.total_source = "ai"
    out.parser = "ai"
    return out


def collapse_keep_lines(text: str) -> str:
    return "\n".join(collapse(ln) for ln in text.splitlines() if ln.strip())


def merge_status(old: str, new: str) -> str:
    """Cancelled is final; otherwise a status never goes backwards."""
    if "cancelled" in (old, new):
        return "cancelled"
    return new if _STATUS_RANK.get(new, 0) >= _STATUS_RANK.get(old, 0) else old
