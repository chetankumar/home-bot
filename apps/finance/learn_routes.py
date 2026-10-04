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
            rules = [dict(r) for r in conn.execute(
                "SELECT * FROM learned_parsers WHERE status != 'rejected' ORDER BY id DESC")]
        for r in rules:
            r["preview"] = json.loads(r["preview"]) if r["preview"] else None
        return ctx.render(
            request, "parsers.html", kinds=KINDS, misses=misses, recent=recent, rules=rules, msg=msg,
            ai_ready=ctx.ai.available(),
        )

    @router.post("/parsers/propose")
    def propose(kind: str = Form(...)):
        if kind not in KINDS:
            raise HTTPException(404)
        return back(learn.propose(ctx, kind))

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

    @router.post("/parsers/{rule_id}/disable")
    def disable(rule_id: int):
        decide(rule_id, "disabled")
        return back("Disabled. Transactions it already created are kept.")

    return router
