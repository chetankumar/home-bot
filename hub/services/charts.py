"""Charts and period statistics, as a service any app can use through `ctx.charts`.

The service never touches a database: your app supplies the per-day amounts and gets back the
numbers (pace, forecast, the day a limit is reached...) and, if you want it, a ready-to-place
chart. It is for "how much so far this month, against an optional limit" questions: spending
against a budget, electricity against an allowance, data against a plan, calories against a goal.

    stats = ctx.charts.stats({1: 12000, 2: 4500}, limit=300000)          # numbers only
    chart = ctx.charts.progress(daily, limit=300000, unit=Unit.inr())     # numbers + HTML
    ... {{ chart.html }} ...

Amounts are integers in the unit's minor units (paise for rupees). See docs/plugin-guide.md.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment
from markupsafe import Markup

from hub.services.chart_render import VIEWS, build_progress
from hub.services.chart_units import ChartText, Unit

MODE_RUNRATE, MODE_ONEOFFS = "runrate", "oneoffs"
MODES = (MODE_RUNRATE, MODE_ONEOFFS)
RECENT_DAYS = 7


def parse_mode(value: str | None, default: str = MODE_RUNRATE) -> str:
    """A mode name from untrusted input (a query string): the value if valid, else `default`."""
    return value if value in MODES else default


@dataclass
class ProgressStats:
    """Where a month stands, and where it is heading. All amounts are minor units."""

    year: int
    month: int
    days_in_period: int
    days_elapsed: int  # days counted so far: today's date (current), all (past), 0 (future)
    state: str  # current | past | future
    mode: str
    limit: int | None
    spent: int  # total so far
    daily: list[int]  # per day, index 0 = day 1 (zero after today)
    cumulative: list[int]  # total through day i; index 0 = 0; length days_elapsed + 1
    big: list[dict]  # big items counted once ("oneoffs" mode): day, amount, name
    big_total: int
    rate: int  # per-day pace (everyday spend only, in "oneoffs" mode)
    projected: int  # forecast for the end of the month at that pace
    remaining: int | None  # limit - spent
    projected_over: int | None  # projected - limit (negative = under)
    days_left: int
    target_daily: int | None  # per day you can add from now and still finish exactly on the limit
    cut_pct: int | None  # % the pace must fall to hit target_daily (None = on track)
    recent_pace: int  # per-day pace over the last 7 days
    recent_end: int  # forecast at that pace
    needed_end: int | None  # where the "needed" line ends (about the limit)
    extra: dict = field(default_factory=dict)

    def crossing(self, which: str = "month") -> date | None:
        """The day the total reaches the limit: already (the first day it did), or on a forecast
        line (`which` is "month" for this month's pace, "recent" for the last 7 days). None if never."""
        if self.limit is None or self.days_elapsed == 0:
            return None
        if self.spent >= self.limit:
            day = next(i for i, c in enumerate(self.cumulative) if c >= self.limit)
            return date(self.year, self.month, max(day, 1))
        left = self.days_left
        end = self.projected if which == "month" else self.recent_end
        pace = (end - self.spent) / left if left > 0 else 0
        if pace <= 0:
            return None
        x = self.days_elapsed + (self.limit - self.spent) / pace
        if x > self.days_in_period:
            return None
        return date(self.year, self.month, min(self.days_in_period, max(self.days_elapsed + 1, ceil(x))))


def progress_stats(
    daily: Mapping[int, int] | Sequence[int],
    *,
    year: int,
    month: int,
    today: date,
    limit: int | None = None,
    mode: str = MODE_RUNRATE,
    big: Iterable[Mapping[str, Any]] = (),
) -> ProgressStats:
    """Pure maths behind `ctx.charts.stats()`; see that method for the arguments."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive (or None)")
    n = calendar.monthrange(year, month)[1]
    if (year, month) == (today.year, today.month):
        d, state = today.day, "current"
    elif (year, month) < (today.year, today.month):
        d, state = n, "past"
    else:
        d, state = 0, "future"

    items = daily.items() if isinstance(daily, Mapping) else enumerate(daily, start=1)
    per_day = {int(k): int(v) for k, v in items if 1 <= int(k) <= n and v}
    spent = sum(per_day.values())
    series = [0] * n
    for day, v in per_day.items():
        # Anything dated after today (a timezone slip, a future-dated entry) counts today.
        series[(min(day, d) if d else day) - 1] += v
    series = [v if i < d else 0 for i, v in enumerate(series)]
    cumulative = [0]
    for i in range(d):
        cumulative.append(cumulative[-1] + series[i])

    used = []
    if mode == MODE_ONEOFFS:
        for b in big:
            day = int(b["day"])
            if 1 <= day <= n:
                used.append({**b, "day": min(day, d) if d else day})
    big_total = sum(int(b["amount"]) for b in used)
    big_by_day: dict[int, int] = {}
    for b in used:
        big_by_day[b["day"]] = big_by_day.get(b["day"], 0) + int(b["amount"])

    rate = round((spent - big_total) / d) if d else 0
    projected = spent if d == n else big_total + rate * n
    remaining = (limit - spent) if limit is not None else None
    days_left = n - d
    target = cut = None
    if limit is not None and days_left > 0:
        target = max(remaining, 0) // days_left  # floored: rounding never plans an overshoot
        if rate > target:
            cut = round(100 * (1 - target / rate))
    recent_pace = 0
    if d:
        lo = max(1, d - RECENT_DAYS + 1)
        recent_pace = round(sum(series[i - 1] - big_by_day.get(i, 0) for i in range(lo, d + 1)) / (d - lo + 1))
    return ProgressStats(
        year=year, month=month, days_in_period=n, days_elapsed=d, state=state, mode=mode, limit=limit,
        spent=spent, daily=series, cumulative=cumulative, big=used, big_total=big_total, rate=rate,
        projected=projected, remaining=remaining,
        projected_over=(projected - limit) if limit is not None else None,
        days_left=days_left, target_daily=target, cut_pct=cut,
        recent_pace=recent_pace, recent_end=spent + recent_pace * days_left,
        needed_end=(spent + target * days_left) if target is not None else None,
    )


@dataclass
class ProgressChart:
    html: Markup  # the chart card: put it in a template as {{ chart.html }}
    stats: ProgressStats
    view: str  # "climb" or "burn" as actually drawn (burn needs a limit)
    empty: bool  # nothing to draw (a future month, or no activity in a past one)


class Charts:
    """The host-wide service. Apps use their own scoped view, `ctx.charts` (AppCharts)."""

    def __init__(self, env: Environment):
        self._env = env

    def for_app(self, tz: ZoneInfo) -> AppCharts:
        return AppCharts(self, tz)

    def render_progress(self, geometry: dict, text: ChartText, links: Mapping[str, str]) -> Markup:
        tpl = self._env.get_template("charts/progress.html")
        return Markup(tpl.render(chart=geometry, text=text, links=dict(links)))


class AppCharts:
    """`ctx.charts`: period statistics and charts. The month defaults to the current one, in the hub timezone."""

    def __init__(self, charts: Charts, tz: ZoneInfo):
        self._charts = charts
        self._tz = tz

    def _period(self, year: int | None, month: int | None, today: date | None) -> tuple[int, int, date]:
        today = today or datetime.now(self._tz).date()
        return (year or today.year), (month or today.month), today

    def stats(
        self,
        daily: Mapping[int, int] | Sequence[int],
        *,
        year: int | None = None,
        month: int | None = None,
        today: date | None = None,
        limit: int | None = None,
        mode: str = MODE_RUNRATE,
        big: Iterable[Mapping[str, Any]] = (),
    ) -> ProgressStats:
        """Numbers only: pace, forecasts, the day the limit is reached, the daily target to stay within it.

        daily   per-day amounts in minor units, as {day_of_month: amount} or a list (index 0 = day 1)
        limit   the budget / allowance / goal, or None
        mode    "runrate" (average so far x days in the month) or "oneoffs" (items in `big` count once
                and only the rest is projected forward)
        big     [{"day": 1, "amount": 1500000, "name": "Rent"}, ...] (used only in "oneoffs" mode)
        """
        y, m, t = self._period(year, month, today)
        return progress_stats(daily, year=y, month=m, today=t, limit=limit, mode=mode, big=big)

    def progress(
        self,
        daily: Mapping[int, int] | Sequence[int],
        *,
        year: int | None = None,
        month: int | None = None,
        today: date | None = None,
        limit: int | None = None,
        mode: str = MODE_RUNRATE,
        big: Iterable[Mapping[str, Any]] = (),
        unit: Unit | None = None,
        view: str = "climb",
        links: Mapping[str, str] | None = None,
        text: ChartText | None = None,
    ) -> ProgressChart:
        """The month as a chart that grows as you go: a climb-up (the total rises) or burn-down (what is
        left falls) with forecast lines and the limit. Arguments as `stats()`, plus:

        unit    how amounts are written: Unit.inr() (default), Unit.plain("kWh", minor=1000, decimals=1)...
        view    "climb" (default) or "burn" (needs a limit; falls back to climb without one)
        links   {"climb": url, "burn": url} to show the view toggle; omit for a chart with no toggle
        text    ChartText(...) to name things in your domain (title, "budget", "Spent so far", ...)
        """
        stats = self.stats(daily, year=year, month=month, today=today, limit=limit, mode=mode, big=big)
        unit, text = unit or Unit.inr(), text or ChartText()
        geometry = build_progress(stats, view if view in VIEWS else "climb", unit, text)
        geometry["title"] = text.title
        html = self._charts.render_progress(geometry, text, links or {})
        return ProgressChart(html=html, stats=stats, view=geometry["view"], empty=bool(geometry.get("empty")))
