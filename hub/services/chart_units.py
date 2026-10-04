"""How chart numbers are written (rupees, kWh, notes...) and the words a chart uses."""

from __future__ import annotations

from dataclasses import dataclass


def group_indian(n: int) -> str:
    """1234567 -> 12,34,567."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


@dataclass(frozen=True)
class Unit:
    """A unit of measure. Chart values are integers in *minor units* (paise for rupees, Wh for kWh...).

    `Unit.inr()` is rupees in paise; `Unit.plain("kWh", minor=1000)` is kWh stored as Wh;
    `Unit.plain("notes")` is whole counts.
    """

    symbol: str = ""
    prefix: bool = True  # "₹189" rather than "189 kWh"
    minor: int = 1  # how many minor units make one whole unit
    indian: bool = False  # lakh/crore grouping (12,34,567) and the L / Cr short forms
    decimals: int = 0  # decimal places shown by format()

    @classmethod
    def inr(cls) -> Unit:
        return cls("₹", True, 100, True, 0)

    @classmethod
    def plain(cls, symbol: str = "", minor: int = 1, decimals: int = 0, prefix: bool = False) -> Unit:
        return cls(symbol, prefix, minor, False, decimals)

    def _wrap(self, body: str, sign: str, symbol: bool = True) -> str:
        if not self.symbol or not symbol:
            return f"{sign}{body}"
        return f"{sign}{self.symbol}{body}" if self.prefix else f"{sign}{body} {self.symbol}"

    def format(self, value: float, decimals: int | None = None) -> str:
        """Full precision with grouping: ₹12,34,567 or 1,284.5 kWh. Rounds half up."""
        decimals = self.decimals if decimals is None else decimals
        sign = "-" if value < 0 else ""
        scale = 10**decimals
        scaled = (int(abs(value)) * scale + self.minor // 2) // self.minor
        major, frac = divmod(scaled, scale)
        body = group_indian(major) if self.indian else f"{major:,}"
        if decimals:
            body += f".{frac:0{decimals}d}"
        return self._wrap(body, sign)

    def short(self, value: float, symbol: bool = True) -> str:
        """Compact for axes and labels: ₹189, ₹17.5k, ₹1.7L, 12.5k kWh, 3.2M. `symbol=False` drops the unit."""
        x = value / self.minor
        sign = "-" if x < 0 else ""
        a = abs(x)

        def trim(v: float) -> str:
            return f"{v:.1f}".rstrip("0").rstrip(".")

        if self.indian:
            if a >= 1e7:
                body = trim(a / 1e7) + "Cr"
            elif a >= 1e5 or round(a / 1e3, 1) >= 100:
                body = trim(a / 1e5) + "L"
            elif a >= 1e3:
                body = trim(a / 1e3) + "k"
            else:
                body = trim(a) if self.decimals else f"{round(a):d}"
        else:
            if a >= 1e9:
                body = trim(a / 1e9) + "B"
            elif a >= 1e6 or round(a / 1e3, 1) >= 1000:
                body = trim(a / 1e6) + "M"
            elif a >= 1e3:
                body = trim(a / 1e3) + "k"
            else:
                body = trim(a) if self.decimals else f"{round(a):d}"
        return self._wrap(body, sign, symbol)


@dataclass(frozen=True)
class ChartText:
    """The words a progress chart uses. The defaults are generic; pass your own for your domain."""

    title: str = "Progress this month"
    noun: str = "limit"  # what the line is called: "budget", "allowance", "goal", "plan"
    total_name: str = "Total so far"  # the climbing line
    left_name: str = "Left"  # the falling line in the burn-down view
    big_name: str = "Big item"  # a single large item counted once in "oneoffs" mode
    activity: str = "activity"  # "No {activity} recorded yet this month."
