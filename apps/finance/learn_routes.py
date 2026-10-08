"""The Parsers page: the miss log, and the regexes the local model proposed for approval."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hub.plugin import AppContext

from . import learn
from . import orders as orders_mod
from .sync import reparse

KINDS = {"bank": "HDFC alerts", "amazon": "Amazon orders"}


def build_learn_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    def back(msg: str | None = None) -> RedirectResponse:
        return RedirectResponse(ctx.url("/parsers") + ("?" + urlencode({"msg": msg}) if msg else ""), status_code=303)

    @router.get("/parsers", response_class=HTMLResponse)
    def parsers_page(request: Request, msg: str | None = None):
        with ctx.db() as conn:
            misses = {
                k: dict(conn.execute(
                    "SELECT outcome, COUNT(*) FROM parse_misses WHERE kind = ? GROUP BY outcome", (k,)
                ).fetchall())
                for k in KINDS
            }
            recent = [dict(r) for r in conn.execute(
                "SELECT m.kind, m.outcome, m.noted_at, COALESCE(e.subject, o.subject) AS subject,"
                " COALESCE(e.received_at, o.received_at) AS received_at"
                " FROM parse_misses m LEFT JOIN emails e ON m.kind = 'bank' AND e.gmail_id = m.gmail_id"
                " LEFT JOIN order_emails o ON m.kind = 'amazon' AND o.gmail_id = m.gmail_id"
                " ORDER BY m.noted_at DESC LIMIT 50"
            )]
            for k in KINDS:
                learn.ensure_seeded(conn, k)  # so a fresh install shows the built-ins too
            transcripts = {r["kind"]: json.loads(r["transcript"] or "[]") for r in conn.execute(
                "SELECT kind, transcript FROM regex_negotiations WHERE id IN"
                " (SELECT MAX(id) FROM regex_negotiations GROUP BY kind)")}
            rules = [dict(r) for r in conn.execute(
                "SELECT * FROM learned_parsers WHERE status != 'rejected' ORDER BY matches DESC, id")]
        for r in rules:
            r["preview"] = json.loads(r["preview"]) if r["preview"] else None
        proposed = [r for r in rules if r["status"] == "proposed"]
        scorecard = {k: [r for r in rules if r["kind"] == k and r["status"] != "proposed"] for k in KINDS}
        return ctx.render(
            request, "parsers.html", kinds=KINDS, misses=misses, recent=recent, proposed=proposed,
            scorecard=scorecard, msg=msg, transcripts=transcripts,
            status={k: status_context(k) for k in KINDS},
            ai_ready=ctx.ai.available(),
        )

    def latest(kind: str) -> dict | None:
        with ctx.db() as conn:
            row = conn.execute(
                "SELECT id, kind, status, replies, samples, last_error, started_at, finished_at"
                " FROM regex_negotiations WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return dict(row) if row else None

    def status_context(kind: str) -> dict:
        n = latest(kind)
        running = ctx.scheduler.is_running(f"learn_{kind}")
        if n and n["status"] == "running" and not running:  # the process died mid-run
            n["status"], n["last_error"] = "failed", n["last_error"] or "Interrupted."
        return {"kind": kind, "label": KINDS[kind], "n": n, "running": running, "max_replies": learn.MAX_REPLIES}

    @router.post("/parsers/propose", response_class=HTMLResponse)
    def propose(request: Request, kind: str = Form(...)):
        if kind not in KINDS:
            raise HTTPException(404)
        with ctx.db() as conn:
            pending = conn.execute(
                "SELECT 1 FROM learned_parsers WHERE kind = ? AND status = 'proposed'", (kind,)).fetchone()
        if pending:
            return back("Approve or reject the pending proposals first.")
        ctx.scheduler.run_now(f"learn_{kind}")  # runs in the background; the page shows its progress
        return back()

    @router.get("/parsers/status/{kind}", response_class=HTMLResponse)
    def status(request: Request, kind: str):
        if kind not in KINDS:
            raise HTTPException(404)
        context = status_context(kind)
        headers = None
        if not context["running"] and request.headers.get("HX-Request") and request.query_params.get("watch"):
            headers = {"HX-Refresh": "true"}  # finished: reload to show the proposals
        return ctx.render(request, "_learn_status.html", headers=headers, **context)

    @router.post("/parsers/negotiations/{nid}/dismiss")
    def dismiss(nid: int, to: str = Form("/")):
        with ctx.db() as conn:
            conn.execute("UPDATE regex_negotiations SET dismissed = 1 WHERE id = ?", (nid,))
        return RedirectResponse(ctx.url(to if to.startswith("/") and not to.startswith("//") else "/"), status_code=303)

    def decide(rule_id: int, status: str) -> str:
        with ctx.db() as conn:
            row = conn.execute("SELECT kind FROM learned_parsers WHERE id = ?", (rule_id,)).fetchone()
            if row is None:
                raise HTTPException(404)
            conn.execute(
                "UPDATE learned_parsers SET status = ?, decided_at = datetime('now') WHERE id = ?", (status, rule_id)
            )
        return row["kind"]

    @router.post("/parsers/{rule_id}/approve")
    def approve(rule_id: int):
        kind = decide(rule_id, "active")
        # Re-read the emails that were missed; the model is not used, only the regexes.
        if kind == "bank":
            r = reparse(ctx, use_ai=False)
        else:
            r = orders_mod.reparse(ctx, use_ai=False)
        with ctx.db() as conn:
            learn.clear_covered(conn, kind)
        return back(f"Approved. Re-read missed emails: {r['parsed']} parsed, {r['unparsed']} still unparsed.")

    @router.post("/parsers/{rule_id}/reject")
    def reject(rule_id: int):
        decide(rule_id, "rejected")
        return back("Rejected.")

    @router.post("/parsers/{rule_id}/enable")
    def enable(rule_id: int):
        decide(rule_id, "active")
        return back("Enabled.")

    @router.post("/parsers/{rule_id}/disable")
    def disable(rule_id: int):
        decide(rule_id, "disabled")
        return back("Disabled. Transactions it already created are kept.")

    return router
