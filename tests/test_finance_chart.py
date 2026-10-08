"""The Finance dashboard's use of ctx.charts: view modes, forecast modes, and agreement with the cards."""

from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from apps.finance import stats
from tests.conftest import login
from tests.test_finance import count, fin, seed  # noqa: F401  (fin is a fixture)

BASE = "/apps/finance/"
DAY10 = date(2026, 10, 10)  # "today" in these tests: 10 of 31 days gone


@pytest.fixture
def dash(fin, monkeypatch):  # noqa: F811
    import hub_apps.finance.routes as routes_mod

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 12, tzinfo=tz)

    monkeypatch.setattr(routes_mod, "datetime", Frozen)
    seed(fin.ctx, [("2026-10-01T09:00:00", 15000, "debit", None),   # rent on the 1st: a big payment
                   ("2026-10-02T10:00:00", 450, "debit", None),
                   ("2026-10-05T10:00:00", 1299, "debit", None),
                   ("2026-10-09T10:00:00", 640, "debit", None),
                   ("2026-10-07T10:00:00", 25000, "debit", "Transfers"),  # not spending: never charted
                   ("2026-10-08T10:00:00", 50000, "credit", None)])         # credits are not spending
    fin.ctx.kv.set("budget_paise", 40000_00)
    fin.client = login(TestClient(fin.app))
    return fin


def text(html: str) -> str:
    """The page as a reader sees it: HTML entities decoded (Jinja escapes the apostrophe in "month's")."""
    return html.replace("&#39;", "'").replace("&amp;", "&")


def query(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# -- the chart on the page -----------------------------------------------------------------------------------
def test_dashboard_has_the_chart_with_legend_summary_and_table(dash):
    html = text(dash.client.get(BASE).text)
    assert 'class="card viz-root progress-chart"' in html and "Spending through the month" in html
    for want in ("Spent so far", "At this month's pace", "At last 7 days' pace", "Needed to finish within budget",
                 "Table view", "Run-rate:"):
        assert want in html
    assert html.index("Spent so far") > html.index("Projected month-end")  # below the stat cards
    assert 'src="/static/charts.js"' in html  # loaded by the shell, not per page


def test_only_real_spending_is_charted(dash):
    with dash.ctx.db() as conn:
        s = stats.month_stats(conn, 2026, 10, DAY10, 40000_00)
    assert s.spent == (15000 + 450 + 1299 + 640) * 100  # no Transfers, no credit
    assert s.daily[0] == 15000_00 and s.daily[6] == 0 and s.daily[8] == 640_00 and s.cumulative[-1] == s.spent


def test_view_toggle_keeps_the_month_and_forecast(dash):
    html = dash.client.get(BASE + "?chart=climb&forecast=oneoffs&month=2026-10").text
    links = [a for a in html.split('<a href="')[1:] if "chart=burn" in a.split('"')[0]]
    assert links and query(links[0].split('"')[0].replace("&amp;", "&")) == {
        "month": "2026-10", "chart": "burn", "forecast": "oneoffs"}
    burn = dash.client.get(BASE + "?chart=burn").text
    assert "Budget left" in burn and "Budget used up" in burn


def test_burn_down_needs_a_budget(dash):
    dash.ctx.kv.delete("budget_paise")
    html = dash.client.get(BASE + "?chart=burn").text
    assert "Burn-down needs a budget" in html and 'class="disabled"' in html
    assert "Spent so far" in html  # it still shows the climb-up


def test_past_future_and_empty_months(dash):
    seed(dash.ctx, [("2026-09-15T10:00:00", 30000, "debit", None)])
    past = dash.client.get(BASE + "?month=2026-09").text
    assert "Finished the month at ₹30,000, ₹10,000 under budget." in past and "Needed to finish" not in past
    assert "Nothing to chart for this month yet." in dash.client.get(BASE + "?month=2026-12").text
    assert "No spending was recorded this month." in dash.client.get(BASE + "?month=2026-08").text


# -- forecast modes ------------------------------------------------------------------------------------------------
def numbers(fin, mode):  # noqa: F811
    with fin.ctx.db() as conn:
        return stats.burn(conn, 2026, 10, DAY10, 40000_00, mode)


def test_run_rate_is_the_default_and_matches_the_original_maths(dash):
    b = numbers(dash, "runrate")
    assert (b.mode, b.big_total) == ("runrate", 0)
    assert b.daily_rate == round(b.spent / 10) and b.projected == b.daily_rate * 31
    html = dash.client.get(BASE).text
    assert "Average daily spend so far × days in the month." in html
    assert stats.inr(b.projected, False) in html  # the Projected card


def test_big_payments_once_counts_the_rent_once(dash):
    run, once = numbers(dash, "runrate"), numbers(dash, "oneoffs")
    assert (once.big_total, once.big_count) == (15000_00, 1)
    assert once.daily_rate == round((once.spent - 15000_00) / 10)  # everyday spend only
    assert once.projected == 15000_00 + once.daily_rate * 31
    assert once.projected < run.projected  # a rent on the 1st is no longer multiplied by 31
    assert once.days_left == run.days_left and once.target_daily == run.target_daily  # needed pace is unchanged


def test_chart_and_cards_use_the_same_numbers_in_both_modes(dash):
    for mode in ("runrate", "oneoffs"):
        b = numbers(dash, mode)
        html = dash.client.get(BASE + f"?forecast={mode}").text
        assert stats.inr(b.projected, False) in html  # card
        assert f"end at {stats.inr(b.projected, False)}" in html or f"({stats.inr(b.projected, False)})" in html  # chart summary
        with dash.ctx.db() as conn:
            series = stats.month_stats(conn, 2026, 10, DAY10, 40000_00, mode)
        assert series.projected == b.projected and series.rate == b.daily_rate


def test_forecast_choice_is_remembered_and_junk_is_ignored(dash):
    assert dash.ctx.kv.get("forecast_mode") is None
    page = dash.client.get(BASE + "?forecast=oneoffs").text
    assert dash.ctx.kv.get("forecast_mode") == "oneoffs"
    assert "Big payments counted once: ₹15,000" in page and "counted once, not multiplied" in page
    assert "counted once" in dash.client.get(BASE).text  # next visit: still the chosen mode, no query needed
    dash.client.get(BASE + "?forecast=bogus")
    assert dash.ctx.kv.get("forecast_mode") == "oneoffs"  # junk changes nothing
    assert "Run-rate:" in dash.client.get(BASE + "?forecast=runrate").text
    assert dash.ctx.kv.get("forecast_mode") == "runrate"


def test_daily_burn_card_explains_big_payments(dash):
    html = dash.client.get(BASE + "?forecast=oneoffs").text
    assert "everyday spend, ₹15,000 in big payments counted once" in html


def test_the_big_payment_threshold_is_configurable(dash):
    dash.ctx.config["oneoff_threshold"] = 500  # rupees: now the ₹1,299 and ₹640 count as big too
    html = dash.client.get(BASE + "?forecast=oneoffs").text
    assert "everyday spend, ₹16,939 in big payments counted once" in html
    dash.ctx.config["oneoff_threshold"] = 50000
    assert "Big payments counted once: none this month." in dash.client.get(BASE + "?forecast=oneoffs").text


def test_the_forecast_control_names_the_threshold(dash):
    html = dash.client.get(BASE).text
    assert 'aria-label="Forecast method"' in html and "Big payments once" in html
    assert "a single payment of ₹5,000 or more counts once" in html


def test_a_big_payment_is_marked_on_the_chart_and_in_the_tooltip_data(dash):
    html = dash.client.get(BASE + "?forecast=oneoffs").text
    assert 'class="viz-big s1"' in html  # a diamond on the Actual line
    assert "Big payment" in html and "₹15,000" in html


# -- the day-by-day columns inside the same chart ---------------------------------------------------------------
def column_paths(html):
    import re

    return re.findall(r'class="viz-col-(blue|red)" d="([^"]+)"', html)


def test_rent_is_a_diamond_not_a_column(dash):
    html = dash.client.get(BASE).text
    with dash.ctx.db() as conn:
        s = stats.month_stats(conn, 2026, 10, DAY10, 40000_00)
    assert s.everyday[0] == 0 and s.daily[0] == 15000_00  # day 1 was only the rent
    assert [i + 1 for i, e in enumerate(s.everyday) if e] == [2, 5, 9]
    assert len([p for kind, p in column_paths(html) if kind == "blue"]) == 3  # one column for each everyday day
    assert 'class="viz-big s-ref"' in html and "Big payment, Thu 1 Oct" in html
    assert "Everyday spend · big payments left out" in html
    assert "Big payments (₹15,000 cp0) are left out of the columns." in text(html)  # says what was left out


def test_a_heavy_day_gets_a_red_cap(dash):
    seed(dash.ctx, [("2026-10-10T10:00:00", 3000, "debit", None)])
    html = dash.client.get(BASE).text
    assert 'class="viz-col-red"' in html
    assert "Over target on 2 of 10 days" in text(html)  # the fixture has one other heavy day; quiet days count as under, so out of days elapsed
    assert "over target" in html  # tooltip data and table


def test_the_target_line_is_the_card_figure(dash):
    with dash.ctx.db() as conn:
        s = stats.month_stats(conn, 2026, 10, DAY10, 40000_00)
        b = stats.burn(conn, 2026, 10, DAY10, 40000_00)
    assert s.day_target == b.target_daily and s.day_target_kind == "needed"
    html = text(dash.client.get(BASE).text)
    assert "Target daily burn" in html  # the card, and the line's label, share the name
    assert f"the {stats.inr(b.target_daily, False)}/day target" in html  # the sentence names the same figure


def test_the_columns_ignore_the_forecast_mode(dash):
    run = column_paths(dash.client.get(BASE + "?forecast=runrate").text)
    once = column_paths(dash.client.get(BASE + "?forecast=oneoffs").text)
    assert run and run == once  # only the line pane follows the Forecast control


def test_a_finished_month_is_judged_against_an_even_split(dash):
    seed(dash.ctx, [("2026-09-15T10:00:00", 3000, "debit", None), ("2026-09-20T10:00:00", 500, "debit", None)])
    html = text(dash.client.get(BASE + "?month=2026-09").text)
    assert "Even split" in html and "against an even split of ₹1,333/day" in html  # 40,000 / 30 days
    assert 'class="viz-col-red"' in html  # the 3,000 day is over it


def test_without_a_budget_the_columns_remain_with_no_target(dash):
    dash.ctx.kv.delete("budget_paise")
    html = dash.client.get(BASE).text
    assert column_paths(html) and "viz-target" not in html.replace(".viz-target", "")  # no target line drawn
    assert "Everyday spend averages" in html


def test_hello_has_a_daily_pane_too(dash):
    dash.client.post("/apps/hello/notes", data={"body": "one"})
    html = dash.client.get("/apps/hello/").text
    assert "Notes per day" in html and "viz-target" not in html.replace(".viz-target", "") or "Daily pace for the goal" in html
