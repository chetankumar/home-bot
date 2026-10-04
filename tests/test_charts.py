"""The host's chart service (ctx.charts): units, period statistics, geometry, rendering, safety."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from hub.services import chart_render
from hub.services.chart_units import ChartText, Unit
from hub.services.charts import MODES, parse_mode, progress_stats
from tests.conftest import ROOT, login

TODAY = date(2026, 10, 10)  # day 10 of 31: 21 days left


def stats(daily, **kw):
    kw.setdefault("year", 2026)
    kw.setdefault("month", 10)
    kw.setdefault("today", TODAY)
    return progress_stats(daily, **kw)


# -- units --------------------------------------------------------------------------------------
def test_inr_format_and_rounding():
    u = Unit.inr()
    assert u.format(18900) == "₹189"
    assert u.format(123456789) == "₹12,34,568"  # 12,34,567.89 rounds half up
    assert u.format(99950) == "₹1,000" and u.format(99949) == "₹999"
    assert u.format(-5000) == "-₹50" and u.format(5050, decimals=2) == "₹50.50"
    assert u.format(0) == "₹0"


def test_short_forms():
    u = Unit.inr()
    assert [u.short(v) for v in (18900, 1750000, 4000000, 17000000, 9995000, 2000000000)] == [
        "₹189", "₹17.5k", "₹40k", "₹1.7L", "₹1L", "₹2Cr"]
    k = Unit.plain("k units", minor=1)
    assert Unit.plain("GB").short(2_500_000) == "2.5M GB" and k.short(950) == "950 k units"
    assert Unit.plain("kWh", minor=1000, decimals=1).format(12345) == "12.3 kWh"
    assert Unit.plain("notes").format(7) == "7 notes" and Unit.plain("kWh", prefix=True).format(7) == "kWh7"


# -- statistics ---------------------------------------------------------------------------------------
def test_run_rate_numbers():
    s = stats({1: 100000, 2: 50000, 5: 50000}, limit=500000)
    assert (s.state, s.days_elapsed, s.days_in_period, s.days_left) == ("current", 10, 31, 21)
    assert (s.spent, s.rate, s.projected) == (200000, 20000, 620000)  # 20,000 a day x 31
    assert (s.remaining, s.projected_over) == (300000, 120000)
    assert s.target_daily == 300000 // 21 == 14285 and s.cut_pct == round(100 * (1 - 14285 / 20000))
    assert s.needed_end == 200000 + 14285 * 21
    assert s.cumulative[:6] == [0, 100000, 150000, 150000, 150000, 200000] and len(s.cumulative) == 11
    assert s.daily[0] == 100000 and s.daily[10] == 0 and len(s.daily) == 31
    assert s.recent_pace == round(50000 / 7) and s.recent_end == 200000 + s.recent_pace * 21  # days 4-10


def test_crossing_day():
    s = stats({1: 100000, 2: 50000, 5: 50000}, limit=500000)
    assert s.crossing("month") == date(2026, 10, 25)  # 10 + (500000-200000)/20000
    assert s.crossing("recent") is None  # at that pace the month ends under the limit
    over = stats({1: 300000, 4: 250000}, limit=500000)
    assert over.crossing() == date(2026, 10, 4)  # the day it already went over
    assert stats({1: 100}).crossing() is None  # no limit


def test_one_off_mode_counts_big_items_once():
    daily = {1: 100000, 2: 50000, 5: 50000}
    big = [{"day": 1, "amount": 100000, "name": "Rent"}]
    run = stats(daily, limit=500000, mode="runrate", big=big)
    once = stats(daily, limit=500000, mode="oneoffs", big=big)
    assert (run.big_total, run.projected) == (0, 620000)  # `big` is ignored in run-rate mode
    assert (once.big_total, once.rate) == (100000, 10000)  # (200000 - 100000) / 10
    assert once.projected == 100000 + 10000 * 31 == 410000
    assert once.recent_pace == run.recent_pace  # day 1 is outside the last 7 days
    in_window = stats({9: 500000, 10: 7000}, limit=900000, mode="oneoffs", big=[{"day": 9, "amount": 500000, "name": "x"}])
    assert in_window.recent_pace == round(7000 / 7)  # inside the window, it is left out of the pace


def test_modes_agree_without_big_items():
    daily = {1: 1000, 3: 2500}
    assert stats(daily, mode="oneoffs").projected == stats(daily, mode="runrate").projected


def test_past_and_future_months():
    past = stats({1: 100, 30: 50}, month=9, year=2026, today=date(2026, 10, 5), limit=1000)
    assert (past.state, past.days_elapsed, past.spent, past.projected, past.days_left) == ("past", 30, 150, 150, 0)
    assert past.target_daily is None and past.needed_end is None
    future = stats({}, month=12, today=TODAY)
    assert (future.state, future.days_elapsed, future.cumulative, future.spent) == ("future", 0, [0], 0)
    last_day = stats({1: 100}, today=date(2026, 10, 31), limit=1000)
    assert last_day.days_left == 0 and last_day.target_daily is None and last_day.projected == 100


def test_accepts_lists_and_clamps_entries_dated_after_today():
    s = stats([100, 0, 50], limit=1000)  # a list: index 0 is day 1
    assert (s.daily[0], s.daily[2], s.spent) == (100, 50, 150)
    late = stats({12: 700, 3: 100})  # dated after today: counted on today so nothing is lost
    assert late.spent == 800 and late.cumulative[-1] == 800 and late.daily[9] == 700
    assert stats({0: 5, 40: 9}).spent == 0  # not days of this month


def test_argument_validation():
    with pytest.raises(ValueError):
        stats({}, mode="sideways")
    with pytest.raises(ValueError):
        stats({}, limit=0)
    assert parse_mode("oneoffs") == "oneoffs" and parse_mode("junk") == "runrate" and parse_mode(None, "oneoffs") == "oneoffs"
    assert MODES == ("runrate", "oneoffs")


# -- geometry and wording -------------------------------------------------------------------------------------
def build(s, view="climb", **kw):
    return chart_render.build_progress(s, view, kw.get("unit", Unit.inr()), kw.get("text", ChartText()))


def test_nice_ticks():
    assert chart_render.nice_ticks(0, 38) == [0, 10, 20, 30, 40]
    assert chart_render.nice_ticks(0, 7.3) == [0, 2, 4, 6, 8]
    assert chart_render.nice_ticks(-31, 42)[0] <= -31 and chart_render.nice_ticks(-31, 42)[-1] >= 42


def test_climb_scale_covers_the_data_and_the_limit():
    c = build(stats({1: 100000, 5: 100000}, limit=500000))
    labels = [t["label"] for t in c["y_ticks"]]
    assert labels[0] == "₹0" and labels[-1] in ("₹6k", "₹7k", "₹8k", "₹10k")  # past the limit, with the pace
    assert [d["name"] for d in c["legend"]] == ["Total so far", "At this month's pace", "At last 7 days' pace",
                                                  "Needed to finish within limit", "Limit"]
    assert c["has_forecast"] and c["has_needed"] and len(c["table"]) == 31 and len(c["x_ticks"]) >= 6


def test_a_forecast_far_above_the_limit_is_cut_with_one_arrow_and_one_label():
    s = stats({1: 1500000, 2: 50000}, today=date(2026, 10, 4), limit=4000000)
    assert s.projected > 4000000 * 1.6  # far above the cap
    c = build(s)
    assert len(c["clips"]) == 1 and c["clips"][0]["up"] and c["clips"][0]["text"] == "₹1.2L"  # twin forecasts share
    assert [l["key"] for l in c["legend"] if l["key"] in ("month", "recent")] == ["month", "recent"]
    top = float(c["y_ticks"][-1]["y"])
    assert abs(c["clips"][0]["y"] - top) < 1  # the line leaves at the top edge of the plot
    assert float(c["y_ticks"][-1]["y"]) >= chart_render.MT - 1e-6


def test_two_different_cut_forecasts_each_get_a_label_row():
    s = stats({1: 1500000, 9: 2000000}, limit=500000)  # run-rate and last-7-days end in different places
    c = build(s)
    assert len(c["clips"]) == 2 and {k["dy"] for k in c["clips"]} == {0, 14}


def test_burn_down_is_limit_minus_total_and_can_go_below_zero():
    s = stats({1: 300000, 4: 250000}, limit=500000)
    c = build(s, "burn")
    assert c["view"] == "burn" and not c["view_fallback"]
    zero = next(t for t in c["y_ticks"] if t["zero"])
    assert float(zero["y"]) < chart_render.MT + chart_render.PH  # a zero line inside the plot: below it is "over"
    assert [r["actual"] for r in c["table"][:4]] == ["₹3,000", "₹3,000", "₹3,000", "₹5,500"]
    assert [r["left"] for r in c["table"][:4]] == ["₹2,000", "₹2,000", "₹2,000", "-₹500"]
    day4 = c["table"][3]
    assert "-₹500" in day4["left"] and "Limit used up" in {x["name"] for x in c["legend"]}
    data = c["data_json"]
    assert '"label":"Sun 4 Oct"' in data and '"name":"Left"' in data


def test_burn_view_without_a_limit_falls_back_to_climb():
    c = build(stats({1: 1000}), "burn")
    assert c["view"] == "climb" and c["view_fallback"] and not c["has_limit"]
    assert [l["key"] for l in c["legend"]] == ["actual", "month", "recent"]


def test_empty_states():
    assert "Nothing to chart" in build(stats({}, month=12))["empty"]
    assert "No activity was recorded" in build(stats({}, month=9, today=date(2026, 10, 5)))["empty"]
    assert "No spending was recorded" in build(stats({}, month=9, today=date(2026, 10, 5)),
                                               text=ChartText(activity="spending"))["empty"]
    fresh = build(stats({}, today=date(2026, 10, 2), limit=1000))  # this month, nothing yet: still drawn
    assert fresh["empty"] is None and "No activity recorded yet" in fresh["summary"]["text"]


def test_summary_wording():
    t = ChartText(noun="budget", activity="spending")
    sm = lambda s, **kw: build(s, **kw, text=t)["summary"]  # noqa: E731
    over = sm(stats({1: 100000, 2: 50000, 5: 50000}, limit=500000))
    assert (over["tone"], over["icon"]) == ("bad", "⚠")
    assert "reach your ₹5,000 budget on 25 Oct and end at ₹6,200" in over["text"]
    assert "Last 7 days: ₹71/day, ending at ₹3,500." in over["text"]  # no crossing repeated when it adds nothing
    ok = sm(stats({1: 100000}, limit=500000))
    assert (ok["tone"], ok["icon"]) == ("good", "✓") and "On pace to end ₹" in ok["text"] and "under budget" in ok["text"]
    already = sm(stats({1: 300000, 4: 250000}, limit=500000))
    assert "Already ₹500 over budget (since 4 Oct)." in already["text"]
    assert sm(stats({1: 9000}))["tone"] == "info"  # no limit: informational only, no verdict
    done = sm(stats({1: 600000}, month=9, today=date(2026, 10, 5), limit=500000))
    assert "Finished the month at ₹6,000, ₹1,000 over budget." in done["text"]
    assert "Run-rate" in over["notes"][0]
    big = [{"day": 1, "amount": 100000, "name": "Rent"}]
    once = sm(stats({1: 100000, 2: 50000}, limit=500000, mode="oneoffs", big=big))
    assert once["notes"] == ["Big items counted once: ₹1,000 Rent."]


def test_other_units_and_words_flow_through():
    s = stats({1: 12500, 2: 8000}, limit=300000)
    t = ChartText(title="Power this month", noun="allowance", total_name="Used so far", left_name="Allowance left",
                  big_name="Heavy load", activity="power use")
    c = build(s, unit=Unit.plain("kWh", minor=1000, decimals=1), text=t)
    assert c["legend"][0]["name"] == "Used so far" and "Allowance" in {x["name"] for x in c["legend"]}
    assert "20.5 kWh" in c["data_json"] or "20.5 kWh" in str(c["table"])
    assert "allowance" in c["summary"]["text"]


# -- rendering through ctx.charts -------------------------------------------------------------------------------------
@pytest.fixture
def charts(hub_app):
    app = hub_app()
    return app.state.hub.registry.apps["hello"].ctx.charts


def render(charts, daily=None, **kw):
    return charts.progress(daily if daily is not None else {1: 100000, 5: 50000}, today=TODAY, limit=500000, **kw)


def test_ctx_charts_returns_stats_and_safe_html(charts):
    chart = render(charts, links={"climb": "/x?chart=climb", "burn": "/x?chart=burn"})
    assert chart.stats.spent == 150000 and chart.view == "climb" and not chart.empty
    html = str(chart.html)
    assert html.lstrip().startswith("<section") and 'class="card viz-root progress-chart"' in html
    assert 'href="/x?chart=burn"' in html and "Table view" in html and "<svg" in html
    assert "data-chart=" in html and 'visibility="hidden"' in html
    assert hasattr(chart.html, "__html__")  # Markup: a template renders it without escaping
    assert charts.stats({1: 5}, limit=10, today=TODAY).spent == 5  # numbers only, no rendering


def test_defaults_come_from_the_hub_timezone_and_clock(charts):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    s = charts.stats({1: 100})
    assert (s.year, s.month, s.days_elapsed, s.state) == (now.year, now.month, now.day, "current")


def test_no_toggle_without_links_and_a_disabled_burn_without_a_limit(charts):
    assert 'role="group" aria-label="Chart view"' not in str(render(charts).html)
    html = str(charts.progress({1: 5}, today=TODAY, links={"climb": "/c", "burn": "/b"}).html)
    assert 'class="disabled"' in html and 'href="/b"' not in html
    assert "Burn-down needs a limit" in str(charts.progress({1: 5}, today=TODAY, view="burn").html)


def test_text_you_pass_in_is_escaped(charts):
    evil = '<script>alert("x")</script>'
    chart = render(charts, text=ChartText(title=evil, noun=evil, total_name=evil, big_name=evil), mode="oneoffs",
                   big=[{"day": 1, "amount": 100000, "name": evil + '"><img src=x onerror=alert(1)>'}])
    html = str(chart.html)
    assert "<script" not in html and "<img" not in html  # nothing you passed becomes markup
    assert "&lt;script&gt;" in html                       # it is shown as text instead
    assert 'onerror="' not in html and "&lt;img" in html  # including inside the tooltip data attribute


def test_invalid_arguments_raise_clearly(charts):
    with pytest.raises(ValueError):
        render(charts, mode="nope")
    with pytest.raises(ValueError):
        charts.progress({}, limit=-5)


# -- static files and the hello demo -------------------------------------------------------------------------------------
def test_assets_are_served_and_loaded_by_every_page(hub_app):
    client = login(TestClient(hub_app()))
    assert client.get("/static/charts.css").status_code == 200 and client.get("/static/charts.js").status_code == 200
    page = client.get("/").text
    assert 'href="/static/charts.css"' in page and 'src="/static/charts.js"' in page


def test_the_script_writes_text_with_textcontent_only():
    js = (ROOT / "hub" / "static" / "charts.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "document.write" not in js
    assert js.count("textContent") >= 2 and 'visibility", "hidden"' in js and "data-ready" in js


def test_chart_palette_is_the_validated_one():
    css = (ROOT / "hub" / "static" / "charts.css").read_text(encoding="utf-8")
    for hexcode in ("#2a78d6", "#eb6834", "#1baf7a", "#3987e5", "#d95926", "#199e70"):
        assert hexcode in css  # categorical slots 1-3, light and dark (see the dataviz validator)


def test_hello_charts_notes_against_a_goal(hub_app):
    client = login(TestClient(hub_app()))
    client.post("/apps/hello/notes", data={"body": "first"})
    client.post("/apps/hello/notes", data={"body": "second"})
    html = client.get("/apps/hello/").text
    assert "Notes this month" in html and "Notes so far" in html and "goal" in html and "<svg" in html


def test_label_goes_on_the_side_the_lines_do_not_leave_to():
    s = stats({1: 100000, 5: 100000}, limit=500000)
    climb, burn = build(s)["labels"][-1], build(s, "burn")["labels"][-1]
    ty_climb = next(float(d["y"]) for d in build(s)["dots"] if d["key"] == "actual")
    ty_burn = next(float(d["y"]) for d in build(s, "burn")["dots"] if d["key"] == "actual")
    assert float(climb["y"]) > ty_climb   # climb-up: the lines rise, so the label sits below the dot
    assert float(burn["y"]) < ty_burn     # burn-down: the lines fall, so it sits above


def test_every_series_class_the_script_can_emit_has_a_fill():
    css = (ROOT / "hub" / "static" / "charts.css").read_text(encoding="utf-8")
    for cls in (".viz-dot.s1", ".viz-dot.s2", ".viz-dot.s3", ".viz-dot.s-ref", ".viz-tri.s-ref"):
        assert cls in css  # the hover dots reuse these; a missing rule draws black


def test_suffix_units_are_named_once_above_the_axis_not_on_every_tick():
    s = stats({1: 3, 2: 4}, limit=20)
    notes = build(s, unit=Unit.plain("notes"))
    assert notes["y_unit"] == "notes" and all("notes" not in t["label"] for t in notes["y_ticks"])
    assert "7 notes" in notes["data_json"]  # tooltips and labels keep the full unit
    money = build(stats({1: 100000}, limit=500000))
    assert money["y_unit"] == "" and money["y_ticks"][1]["label"].startswith("₹")
    assert Unit.plain("kWh").short(1500, symbol=False) == "1.5k" and Unit.plain("kWh").short(1500) == "1.5k kWh"
