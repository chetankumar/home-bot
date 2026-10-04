"""Hello: the reference app. Copy this folder to start a new one.

Shows each part of the contract: a manifest, setup(ctx) returning a router,
its own SQLite migrations, AI via an alias, a scheduled job, and kv settings.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hub.plugin import AppContext, Manifest
from hub.services.ai import AIError
from hub.services.chart_units import ChartText, Unit

MONTHLY_GOAL = 20  # notes

manifest = Manifest(
    id="hello",
    name="Hello",
    icon="👋",
    description="Reference app: notes, an AI summary and a heartbeat job.",
)


def setup(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    def notes() -> list[dict]:
        with ctx.db() as conn:
            rows = conn.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def heartbeat() -> None:
        ctx.kv.set("last_heartbeat", datetime.now(ctx.tz).isoformat(timespec="seconds"))

    ctx.scheduler.interval("heartbeat", heartbeat, minutes=30)

    def notes_per_day() -> dict[int, int]:
        """This month's notes by day (in the hub timezone), for the chart."""
        now = datetime.now(ctx.tz)
        per_day: dict[int, int] = {}
        for n in notes():
            made = datetime.fromisoformat(n["created_at"]).replace(tzinfo=UTC).astimezone(ctx.tz)
            if (made.year, made.month) == (now.year, now.month):
                per_day[made.day] = per_day.get(made.day, 0) + 1
        return per_day

    def notes_chart():
        # ctx.charts does the maths and the drawing; the app only supplies the per-day counts.
        return ctx.charts.progress(
            notes_per_day(), limit=MONTHLY_GOAL, unit=Unit.plain("notes"),
            text=ChartText(title="Notes this month", noun="goal", total_name="Notes so far",
                           left_name="Left to goal", activity="notes"),
        )

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return ctx.render(
            request,
            "hello.html",
            notes=notes(),
            chart=notes_chart(),
            last_heartbeat=ctx.kv.get("last_heartbeat"),
            ai_ready=ctx.ai.available(),
        )

    @router.post("/notes", response_class=HTMLResponse)
    def add_note(request: Request, body: str = Form(...)):
        if body.strip():
            with ctx.db() as conn:
                conn.execute("INSERT INTO notes(body) VALUES (?)", (body.strip(),))
        if request.headers.get("hx-request"):
            return ctx.render(request, "_notes.html", notes=notes())
        return RedirectResponse(ctx.url("/"), status_code=303)

    @router.post("/notes/{note_id}/delete", response_class=HTMLResponse)
    def delete_note(request: Request, note_id: int):
        with ctx.db() as conn:
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        if request.headers.get("hx-request"):
            return ctx.render(request, "_notes.html", notes=notes())
        return RedirectResponse(ctx.url("/"), status_code=303)

    @router.post("/summarise", response_class=HTMLResponse)
    def summarise(request: Request):
        items = notes()
        if not items:
            return HTMLResponse('<p class="muted">Add some notes first.</p>')
        text = "\n".join(f"- {n['body']}" for n in items)
        try:
            result = ctx.ai.complete(
                f"Summarise these notes in two or three sentences:\n{text}",
                model="fast",
                max_tokens=300,
            )
            summary, error = result.text, None
        except AIError as e:
            summary, error = None, str(e)
        return ctx.render(request, "_summary.html", summary=summary, error=error)

    return router
