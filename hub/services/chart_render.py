"""Geometry and wording for the progress chart. Pure functions: ProgressStats in, drawable data out.

Everything is computed here (scales, nice ticks, paths, clipping, summary text, tooltip data and the
table view) so the template only lays it out and the maths can be tested without rendering anything.
Values are integers in the unit's minor units; the SVG viewBox is W x H.
"""

from __future__ import annotations

import json
from datetime import date
from math import ceil, floor, log10

from hub.services.chart_units import ChartText, Unit

W, H = 720, 340
ML, MR, MT, MB = 58, 30, 24, 36
PW, PH = W - ML - MR, H - MT - MB
CAP_FACTOR = 1.6  # the y-axis stops at 1.6x the limit; a forecast beyond it leaves the top with an arrow
VIEWS = ("climb", "burn")
SLOT = {"actual": 1, "month": 2, "recent": 3}


def day_label(year: int, month: int, day: int) -> str:
    d = date(year, month, day)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round tick values covering [lo, hi]."""
    span = max(hi - lo, 1e-9)
    raw = span / target
    mag = 10 ** floor(log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = floor(lo / step + 1e-9) * step
    end = ceil(hi / step - 1e-9) * step
    out, v = [], start
    while v <= end + step / 2:
        out.append(round(v, 9))
        v += step
    return out


def _f(x: float) -> str:
    return f"{x:.1f}"


def build_progress(s, view: str, unit: Unit, text: ChartText) -> dict:
    """Everything the template needs. `view` is 'climb' (the total rises) or 'burn' (what's left falls)."""
    view = view if view in VIEWS else "climb"
    view_fallback = view == "burn" and s.limit is None
    if view_fallback:
        view = "climb"
    base = {"view": view, "view_fallback": view_fallback, "W": W, "H": H, "ML": ML, "MR": MR, "MT": MT,
            "PW": PW, "PH": PH, "mode": s.mode, "state": s.state, "slot": SLOT}
    if s.state == "future" or (s.spent == 0 and s.state == "past"):
        return {**base, "empty": "Nothing to chart for this month yet." if s.state == "future"
                else f"No {text.activity} was recorded this month."}

    n, d, left = s.days_in_period, s.days_elapsed, s.days_left
    L, S = s.limit, s.spent
    forecasting = s.state == "current" and left > 0 and d >= 1
    fmt = unit.format

    def val(c: float) -> float:
        return (L - c) if view == "burn" else c

    # ---- value range ---------------------------------------------------------------------------
    ends = [s.projected, s.recent_end] if forecasting else []
    unit_floor = 10 * unit.minor  # never a degenerate axis
    if view == "climb":
        cap = L * CAP_FACTOR if L else None
        top = max((L or 0), S) * 1.08
        if ends:
            far = max(ends)
            top = max(top, min(far, cap) if cap else far * 1.05)
        top = max(top, unit_floor)
        ticks = nice_ticks(0, top / unit.minor)
        lo_v, hi_v = 0.0, ticks[-1] * unit.minor
    else:
        lowest = min([0, L - S] + [L - e for e in ends])
        lo_raw = max(lowest, -0.6 * L)
        ticks = nice_ticks(lo_raw / unit.minor, L * 1.04 / unit.minor)
        lo_v, hi_v = ticks[0] * unit.minor, ticks[-1] * unit.minor

    def X(day: float) -> float:
        return ML + PW * day / n

    def Y(v: float) -> float:
        return MT + PH * (hi_v - v) / (hi_v - lo_v)

    def clip(x0: float, v0: float, x1: float, v1: float) -> tuple[float, float, bool]:
        """Shorten a line at the top/bottom of the plot; says whether it was cut."""
        if lo_v <= v1 <= hi_v:
            return x1, v1, False
        edge = hi_v if v1 > hi_v else lo_v
        t = (edge - v0) / (v1 - v0)
        return x0 + t * (x1 - x0), edge, True

    # ---- axes ------------------------------------------------------------------------------------
    # A prefix unit (₹) stays on every tick; a suffix unit ("notes", "kWh") is named once above the axis.
    tick_symbol = unit.prefix
    y_ticks = [{"y": _f(Y(t * unit.minor)), "label": unit.short(t * unit.minor, tick_symbol),
                "zero": abs(t) < 1e-9} for t in ticks]
    y_unit = "" if tick_symbol else unit.symbol
    if n <= 10:
        x_days = list(range(1, n + 1))
    else:
        x_days = sorted({1, *range(5, n + 1, 5), n})
        x_days = [x for x in x_days if x == n or n - x >= 3]  # don't crowd the last label
    x_ticks = [{"x": _f(X(i)), "label": str(i)} for i in x_days]

    # ---- lines -------------------------------------------------------------------------------------
    pts = [(i, val(c)) for i, c in enumerate(s.cumulative)]
    actual_d = "M" + " L".join(f"{_f(X(i))} {_f(Y(v))}" for i, v in pts)
    base_v = val(0) if view == "climb" else 0
    area_d = (f"{actual_d} L{_f(X(pts[-1][0]))} {_f(Y(base_v))} L{_f(X(0))} {_f(Y(base_v))} Z"
              if len(pts) > 1 else "")
    lines, clips = [], []
    legend = [{"key": "actual", "name": text.total_name if view == "climb" else text.left_name}]

    def forecast(key: str, name: str, end: int | None, dash: str) -> None:
        if end is None:
            return
        x1, v1, cut = clip(d, val(S), n, val(end))
        lines.append({"key": key, "name": name,
                      "d": f"M{_f(X(d))} {_f(Y(val(S)))} L{_f(X(x1))} {_f(Y(v1))}", "dash": dash,
                      "end": None if cut else (_f(X(n)), _f(Y(v1)))})
        if cut:
            clips.append({"key": key, "x": X(x1), "y": Y(v1), "up": v1 == hi_v, "value": end})
        legend.append({"key": key, "name": name})

    needed_ok = forecasting and L is not None and s.needed_end is not None and S < L
    if forecasting:
        forecast("month", "At this month's pace", s.projected, "6 4")
        forecast("recent", "At last 7 days' pace", s.recent_end, "2 4")
    if needed_ok:
        forecast("needed", f"Needed to finish within {text.noun}", s.needed_end, "10 4 2 4")
    if L is not None:
        legend.append({"key": "budget", "name": text.noun.capitalize() if view == "climb"
                       else f"{text.noun.capitalize()} used up"})
    # Two forecasts cut at the same place share one arrow and one label (their legend entries stay).
    merged: list[dict] = []
    for c in clips:
        twin = next((m for m in merged if abs(m["x"] - c["x"]) < 1 and abs(m["y"] - c["y"]) < 1), None)
        if twin:
            continue
        c["text"] = unit.short(c["value"])
        c["dy"] = len(merged) * 14  # a different arrow gets its own row of label
        merged.append(c)
    clips = merged

    ref_y = Y(L) if (view == "climb" and L is not None) else (Y(0) if view == "burn" else None)
    labels = []
    if L is not None:
        labels.append({"x": _f(ML + 6), "y": _f(ref_y - 6), "anchor": "start",
                       "text": f"{text.noun.capitalize()} {unit.short(L)}" if view == "climb"
                       else f"{text.noun.capitalize()} used up"})
    tx, ty = X(d), Y(val(S))
    today_x = _f(tx) if s.state == "current" else None
    near_left = tx - 110 < ML
    # Beside the dot, on the side the lines don't leave to: they climb in the climb-up view (label below)
    # and fall in the burn-down view (label above).
    label_y = max(ty - 12, MT + 10) if view == "burn" else min(ty + 20, MT + PH - 6)
    labels.append({"x": _f(tx + 10 if near_left else tx - 10), "y": _f(label_y),
                   "anchor": "start" if near_left else "end",
                   "text": f"{text.total_name.split(' so far')[0] if view == 'climb' else text.left_name} "
                           f"{unit.short(val(S))}"})
    dots = [{"x": _f(tx), "y": _f(ty), "key": "actual"}]
    for ln in lines:
        if ln["end"] and ln["key"] != "needed":
            dots.append({"x": ln["end"][0], "y": ln["end"][1], "key": ln["key"]})
    big_marks = [{"x": _f(X(p["day"])), "y": _f(Y(val(s.cumulative[p["day"]])))} for p in s.big if p["day"] <= d]

    # ---- per-day tooltip + table ----------------------------------------------------------------------
    slope = (s.projected - S) / left if left > 0 else 0
    big_by_day: dict[int, list[dict]] = {}
    for p in s.big:
        big_by_day.setdefault(p["day"], []).append(p)

    def py(v: float) -> float:
        return round(min(max(Y(v), MT), MT + PH), 1)

    days, table = [], []
    for i in range(1, n + 1):
        rows = []
        cells = {"date": day_label(s.year, s.month, i), "day": "", "actual": "", "month": "", "recent": "",
                 "needed": "", "left": ""}
        if i <= d:
            c = s.cumulative[i]
            extra = f"+{fmt(s.daily[i - 1])} that day" if s.daily[i - 1] else "nothing that day"
            rows.append({"key": "actual", "name": text.total_name if view == "climb" else text.left_name,
                         "value": fmt(val(c)), "extra": extra, "y": py(val(c))})
            for p in big_by_day.get(i, []):
                rows.append({"key": "big", "name": text.big_name, "value": fmt(p["amount"]), "extra": p["name"]})
            cells.update(day=fmt(s.daily[i - 1]) if s.daily[i - 1] else "—", actual=fmt(c),
                         left=fmt(L - c) if L is not None else "")
        if forecasting and i > d:
            k = i - d
            fm, fr = S + slope * k, S + s.recent_pace * k
            rows.append({"key": "month", "name": "At this month's pace", "value": fmt(val(fm)), "extra": "",
                         "y": py(val(fm))})
            rows.append({"key": "recent", "name": "At last 7 days' pace", "value": fmt(val(fr)), "extra": "",
                         "y": py(val(fr))})
            cells.update(month=fmt(fm), recent=fmt(fr))
            if needed_ok:
                fn = S + s.target_daily * k
                rows.append({"key": "needed", "name": f"Needed to finish within {text.noun}", "value": fmt(val(fn)),
                             "extra": "", "y": py(val(fn))})
                cells["needed"] = fmt(fn)
        days.append({"i": i, "x": round(X(i), 1), "label": cells["date"], "rows": rows})
        table.append(cells)

    data = {"W": W, "H": H, "ML": ML, "PW": PW, "n": n, "d": d, "days": days}
    return {
        **base, "empty": None, "y_ticks": y_ticks, "x_ticks": x_ticks, "actual_d": actual_d, "area_d": area_d,
        "lines": lines, "clips": clips, "labels": labels, "dots": dots, "big_marks": big_marks,
        "today_x": today_x, "ref_y": _f(ref_y) if ref_y is not None else None, "plot_bottom": MT + PH,
        "legend": legend, "summary": summary(s, unit, text), "table": table,
        "has_forecast": forecasting, "has_needed": needed_ok, "has_limit": L is not None, "y_unit": y_unit,
        "data_json": json.dumps(data, separators=(",", ":")),
    }


def summary(s, unit: Unit, text: ChartText) -> dict:
    """A sentence with an icon, so the verdict never rests on color alone."""
    L, S, fmt = s.limit, s.spent, unit.format
    noun = text.noun
    notes = []
    if s.mode == "oneoffs":
        if s.big:
            what = ", ".join(f"{fmt(p['amount'])} {p['name']}" for p in s.big[:3])
            more = f" and {len(s.big) - 3} more" if len(s.big) > 3 else ""
            notes.append(f"{text.big_name}s counted once: {what}{more}.")
        else:
            notes.append(f"{text.big_name}s counted once: none this month.")
    else:
        notes.append("Run-rate: the average per day so far × days in the month.")

    def when(day: date) -> str:
        return f"{day.day} {day.strftime('%b')}"

    if s.state == "past":
        if L is None:
            return {"tone": "info", "icon": "ℹ", "text": f"Finished the month at {fmt(S)}.", "notes": notes}
        diff = S - L
        return {"tone": "bad" if diff > 0 else "good", "icon": "⚠" if diff > 0 else "✓",
                "text": f"Finished the month at {fmt(S)}, {fmt(abs(diff))} {'over' if diff > 0 else 'under'} {noun}.",
                "notes": notes}
    if S == 0:
        return {"tone": "info", "icon": "ℹ", "text": f"No {text.activity} recorded yet this month.", "notes": notes}
    left = s.days_left
    cross_m, cross_r = s.crossing("month"), s.crossing("recent")
    recent = ""
    if left > 0:
        recent = f" Last 7 days: {fmt(s.recent_pace)}/day, ending at {fmt(s.recent_end)}"
        if L is not None and S < L and cross_r and cross_r != cross_m:  # only when it adds something
            recent += f", reaching the {noun} on {when(cross_r)}"
        recent += "."
    if L is None:
        return {"tone": "info", "icon": "ℹ",
                "text": f"At this month's pace you'll end at {fmt(s.projected)}.{recent}", "notes": notes}
    if S >= L:
        return {"tone": "bad", "icon": "⚠",
                "text": f"Already {fmt(S - L)} over {noun} (since {when(s.crossing())}).{recent}", "notes": notes}
    if left > 0 and s.projected > L and cross_m:
        return {"tone": "bad", "icon": "⚠",
                "text": f"At this month's pace you'll reach your {fmt(L)} {noun} on {when(cross_m)} and end at "
                        f"{fmt(s.projected)}.{recent}", "notes": notes}
    return {"tone": "good", "icon": "✓",
            "text": f"On pace to end {fmt(abs(L - s.projected))} under {noun} ({fmt(s.projected)}).{recent}",
            "notes": notes}
