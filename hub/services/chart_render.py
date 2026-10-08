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

W = 720
ML, MR, MT, MB = 58, 30, 24, 36
PW = W - ML - MR
PH = 280  # the line pane when it is alone
PH_LINE = 230  # the line pane when the day-by-day columns sit under it
GAP = 40  # between the panes: room for the column pane's caption
PH_COL = 120  # the column pane
COL_MAX = 24  # a column is never thicker than this
COL_GAP = 2  # surface gap between the blue and red segments of a column
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


def _col_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """A column: square at the baseline (its bottom), rounded at the data end (its top)."""
    r = max(0.0, min(r, h / 2, w / 2))
    if r < 0.5:
        return f"M{_f(x)} {_f(y)} h{_f(w)} v{_f(h)} h{_f(-w)} Z"
    return (f"M{_f(x)} {_f(y + h)} V{_f(y + r)} Q{_f(x)} {_f(y)} {_f(x + r)} {_f(y)} H{_f(x + w - r)} "
            f"Q{_f(x + w)} {_f(y)} {_f(x + w)} {_f(y + r)} V{_f(y + h)} Z")


def build_progress(s, view: str, unit: Unit, text: ChartText, columns: bool = True) -> dict:
    """Everything the template needs. `view` is 'climb' (the total rises) or 'burn' (what's left falls).

    With `columns`, a second pane sits under the line on the same day axis: one column per day of
    everyday spend against the per-day target line. (Two panes, never a dual axis: the two measures
    are on very different scales.)"""
    view = view if view in VIEWS else "climb"
    view_fallback = view == "burn" and s.limit is None
    if view_fallback:
        view = "climb"
    ph = PH_LINE if columns else PH  # the line pane's height
    pane2_top = MT + ph + GAP
    plot_bottom = MT + ph
    hit_bottom = pane2_top + PH_COL if columns else plot_bottom
    H = hit_bottom + MB
    base = {"view": view, "view_fallback": view_fallback, "W": W, "H": H, "ML": ML, "MR": MR, "MT": MT,
            "PW": PW, "PH": ph, "mode": s.mode, "state": s.state, "slot": SLOT}
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
        return MT + ph * (hi_v - v) / (hi_v - lo_v)

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
    x_label_y = hit_bottom + 18  # under the last pane

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
    label_y = max(ty - 12, MT + 10) if view == "burn" else min(ty + 20, MT + ph - 6)
    labels.append({"x": _f(tx + 10 if near_left else tx - 10), "y": _f(label_y),
                   "anchor": "start" if near_left else "end",
                   "text": f"{text.total_name.split(' so far')[0] if view == 'climb' else text.left_name} "
                           f"{unit.short(val(S))}"})
    dots = [{"x": _f(tx), "y": _f(ty), "key": "actual"}]
    for ln in lines:
        if ln["end"] and ln["key"] != "needed":
            dots.append({"x": ln["end"][0], "y": ln["end"][1], "key": ln["key"]})
    big_marks = [{"x": _f(X(p["day"])), "y": _f(Y(val(s.cumulative[p["day"]])))} for p in s.big if p["day"] <= d]

    # ---- day-by-day columns: a second pane on the same day axis ---------------------------------------------
    T, target_kind = (s.day_target, s.day_target_kind) if columns else (None, None)
    ev = s.everyday
    col_pane = None
    target_label = text.target_name if target_kind == "needed" else "Even split"
    if columns and d >= 1:
        peak = max([ev[i] for i in range(d)] + [T or 0, 0])
        top2 = max(peak * 1.15, unit.minor)  # never a degenerate axis
        ticks2 = nice_ticks(0, top2 / unit.minor, 3)
        hi2 = ticks2[-1] * unit.minor

        def Y2(v: float) -> float:
            return pane2_top + PH_COL * (hi2 - v) / hi2

        slot_w = PW / n
        cw = min(COL_MAX, slot_w * 0.7)
        base_y = Y2(0)
        cols = []
        for i in range(1, d + 1):
            e = ev[i - 1]
            if e <= 0:
                continue
            x0 = X(i) - cw / 2
            top_y = min(Y2(e), base_y - 2)  # a tiny day is still a visible sliver
            within = min(e, T) if T is not None else e
            over = T is not None and e > T
            blue = red = None
            blue_h = red_h = 0.0
            if within > 0:
                blue_top = min(Y2(within), base_y - 2)
                blue_h = base_y - blue_top
                if over:  # the blue part stops at the target line; the excess is a red cap above it
                    blue = _col_path(x0, blue_top, cw, blue_h, 0)
                    red_bottom = blue_top - COL_GAP
                    red_top = min(top_y, red_bottom - 2)
                    red_h = red_bottom - red_top
                    red = _col_path(x0, red_top, cw, red_h, 4)
                else:
                    blue = _col_path(x0, blue_top, cw, blue_h, 4)
            elif over:  # the target is zero: all of it is excess
                red_h = base_y - top_y
                red = _col_path(x0, top_y, cw, red_h, 4)
            cols.append({"i": i, "blue": blue, "red": red, "x": _f(X(i)), "y": _f(top_y),
                         "blue_h": round(blue_h, 1), "red_h": round(red_h, 1), "w": round(cw, 1),
                         "label": f"{day_label(s.year, s.month, i)}: {fmt(e)}"})
        diamonds = sorted({p["day"] for p in s.big_items if p["day"] <= d})
        col_pane = {
            "top": _f(pane2_top), "bottom": _f(base_y), "caption_y": _f(pane2_top - 12), "cols": cols,
            "y_ticks": [{"y": _f(Y2(t * unit.minor)), "label": unit.short(t * unit.minor, unit.prefix),
                         "zero": abs(t) < 1e-9, "v": t * unit.minor} for t in ticks2],
            "target_y": _f(Y2(T)) if T is not None else None,
            "target_label": f"{target_label} {unit.short(T)}" if T is not None else None,
            "diamonds": [{"x": _f(X(day)), "y": _f(pane2_top + 9), "day": day_label(s.year, s.month, day)}
                         for day in diamonds],
            "caption": text.day_name + (" · big payments left out" if s.big_items else ""),
            "width": _f(cw),
        }
        legend.append({"key": "col", "name": text.day_name})
        if T is not None:
            legend.append({"key": "over", "name": f"Over {target_label.lower() if target_kind == 'even' else 'target'}"})
            legend.append({"key": "target", "name": target_label})

    # ---- per-day tooltip + table ----------------------------------------------------------------------
    slope = (s.projected - S) / left if left > 0 else 0
    big_by_day: dict[int, list[dict]] = {}
    for p in (s.big_items if columns else s.big):  # the columns leave every big item out, so name them all
        big_by_day.setdefault(p["day"], []).append(p)

    def py(v: float) -> float:
        return round(min(max(Y(v), MT), MT + ph), 1)

    days, table = [], []
    for i in range(1, n + 1):
        rows = []
        cells = {"date": day_label(s.year, s.month, i), "day": "", "actual": "", "month": "", "recent": "",
                 "needed": "", "left": "", "everyday": "", "bigpay": "", "vs": ""}
        if i <= d:
            c = s.cumulative[i]
            extra = f"+{fmt(s.daily[i - 1])} that day" if s.daily[i - 1] else "nothing that day"
            rows.append({"key": "actual", "name": text.total_name if view == "climb" else text.left_name,
                         "value": fmt(val(c)), "extra": extra, "y": py(val(c))})
            if columns:
                e = ev[i - 1]
                if T is not None:
                    diff = e - T
                    vs = f"{fmt(abs(diff))} {'over' if diff > 0 else 'under'} target" if diff else "on target"
                    cells["vs"] = f"+{fmt(diff)}" if diff > 0 else (f"-{fmt(-diff)}" if diff < 0 else "on target")
                else:
                    vs = ""
                rows.append({"key": "col", "name": text.day_name, "value": fmt(e), "extra": vs})
                cells["everyday"] = fmt(e) if e else "—"
            for p in big_by_day.get(i, []):
                rows.append({"key": "big", "name": text.big_name, "value": fmt(p["amount"]),
                             "extra": p["name"] + (" (left out of the columns)" if columns else "")})
            if columns:
                total_big = sum(int(p["amount"]) for p in big_by_day.get(i, []))
                cells["bigpay"] = fmt(total_big) if total_big else "—"
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
        days.append({"i": i, "x": round(X(i), 1), "cw": round(PW / n, 1), "label": cells["date"], "rows": rows})
        table.append(cells)

    data = {"W": W, "H": H, "ML": ML, "PW": PW, "n": n, "d": d, "columns": bool(col_pane), "days": days}
    sm = summary(s, unit, text)
    if col_pane:
        sm["daily"] = daily_sentence(s, unit, text, T, target_kind)
    return {
        **base, "empty": None, "y_ticks": y_ticks, "x_ticks": x_ticks, "actual_d": actual_d, "area_d": area_d,
        "lines": lines, "clips": clips, "labels": labels, "dots": dots, "big_marks": big_marks,
        "today_x": today_x, "ref_y": _f(ref_y) if ref_y is not None else None, "plot_bottom": plot_bottom,
        "hit_bottom": hit_bottom, "hit_h": hit_bottom - MT, "x_label_y": x_label_y, "col_pane": col_pane,
        "has_columns": bool(col_pane), "target_t": T,
        "legend": legend, "summary": sm, "table": table,
        "has_forecast": forecasting, "has_needed": needed_ok, "has_limit": L is not None, "y_unit": y_unit,
        "data_json": json.dumps(data, separators=(",", ":")),
    }


def daily_sentence(s, unit: Unit, text: ChartText, target: int | None, kind: str | None) -> str:
    """The day-by-day ups and downs in a sentence: how many days over, the average, the highest day."""
    d, fmt = s.days_elapsed, unit.format
    if d < 1 or s.spent == 0:
        return ""
    ev = s.everyday[:d]
    avg = round(sum(ev) / d)
    top = max(range(d), key=lambda i: ev[i])
    highest = f" Highest: {day_label(s.year, s.month, top + 1)}, {fmt(ev[top])}." if ev[top] > 0 else ""
    left_out = ""
    if s.big_items:
        named = ", ".join(f"{fmt(p['amount'])} {p['name']}" for p in s.big_items[:2])
        more = f" and {len(s.big_items) - 2} more" if len(s.big_items) > 2 else ""
        left_out = f" Big payments ({named}{more}) are left out of the columns."
    if target is None:
        return f"{text.day_name} averages {fmt(avg)}/day.{highest}{left_out}"
    if target <= 0:
        return f"{text.day_name} averages {fmt(avg)}/day; the {text.noun} is already used up.{highest}{left_out}"
    over = sum(1 for e in ev if e > target)
    diff = avg - target
    against = f"the {fmt(target)}/day target" if kind == "needed" else f"an even split of {fmt(target)}/day"
    vs = f"{fmt(abs(diff))} a day {'over' if diff > 0 else 'under'}" if diff else "right on it"
    return f"Over target on {over} of {d} days; averaging {fmt(avg)}/day against {against} ({vs}).{highest}{left_out}"


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
