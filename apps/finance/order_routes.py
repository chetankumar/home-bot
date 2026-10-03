"""Amazon orders: the list, matching, and manual link/unlink."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hub.plugin import AppContext

from . import matching, orders


def build_order_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    def back(**params: str) -> RedirectResponse:
        return RedirectResponse(ctx.url("/orders") + ("?" + urlencode(params) if params else ""), status_code=303)

    def sync_context() -> dict:
        return {
            "run": ctx.scheduler.last_run(orders.JOB_ID),
            "running": ctx.scheduler.is_running(orders.JOB_ID),
            "last": ctx.kv.get("last_orders_sync"),
            "connected": ctx.gmail.connected,
        }

    @router.post("/orders/sync", response_class=HTMLResponse)
    def sync_now(request: Request):
        if ctx.gmail.connected:
            ctx.scheduler.run_now(orders.JOB_ID)  # is_running() is true as soon as this returns
        return orders_sync_status(request, watch=True)

    @router.get("/orders/sync/status", response_class=HTMLResponse)
    def orders_sync_status(request: Request, watch: bool = False):
        context = sync_context()
        headers = {"HX-Refresh": "true"} if watch and not context["running"] else None  # done: reload the list
        return ctx.render(request, "_orders_sync_status.html", headers=headers, watch=watch, **context)

    @router.get("/orders", response_class=HTMLResponse)
    def orders_page(request: Request, show: str = "all", msg: str | None = None):
        with ctx.db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY ordered_at DESC, id DESC LIMIT 300")]
            items: dict[int, list[dict]] = {}
            for i in conn.execute("SELECT * FROM order_items ORDER BY id"):
                items.setdefault(i["order_id"], []).append(dict(i))
            paid: dict[int, list[dict]] = {}
            for t in conn.execute(
                "SELECT id, order_id, occurred_at, amount_paise, order_match, order_match_note, counterparty_raw"
                " FROM transactions WHERE order_id IS NOT NULL ORDER BY occurred_at"
            ):
                paid.setdefault(t["order_id"], []).append(dict(t))
            # Bank history starts at the oldest alert email synced, not at the first transaction.
            history_start = conn.execute("SELECT MIN(received_at) FROM emails").fetchone()[0]
            spare = [dict(t) for t in matching.candidate_transactions(conn)]
            unparsed = [dict(r) for r in conn.execute(
                "SELECT * FROM order_emails WHERE status = 'unparsed' ORDER BY received_at DESC LIMIT 50")]
            email_counts = dict(conn.execute("SELECT status, COUNT(*) FROM order_emails GROUP BY status").fetchall())
            for o in rows:  # let the user see what the parser saw when the total isn't from the email
                o["email_body"] = None
                if o["total_source"] != "email":
                    body = conn.execute(
                        "SELECT body FROM order_emails WHERE body LIKE ? ORDER BY received_at LIMIT 1",
                        (f"%{o['order_number']}%",),
                    ).fetchone()
                    o["email_body"] = body["body"] if body else None
        for o in rows:
            o["lines"] = items.get(o["id"], [])
            o["paid"] = paid.get(o["id"], [])
            o["before_history"] = bool(history_start) and o["ordered_at"][:10] < history_start[:10]

        matched = sum(1 for o in rows if o["paid"])
        # an order that can still be matched: has a total, isn't cancelled, nothing linked
        open_ = [o for o in rows if not o["paid"] and o["total_paise"] and o["status"] != "cancelled"]
        shown = [o for o in rows if not o["paid"]] if show == "unmatched" else rows
        return ctx.render(
            request,
            "orders.html",
            orders=shown,
            show=show,
            msg=msg,
            n_orders=len(rows),
            n_matched=matched,
            n_open=len(open_),
            spare=spare,
            unparsed=unparsed,
            email_counts=email_counts,
            sync=sync_context(),
            senders=orders.senders(ctx),
        )

    @router.post("/orders/match")
    def match_now():
        with ctx.db() as conn:
            r = matching.match_orders(conn, **matching.window_settings(ctx.config))
        text = (
            f"Matched {r.total} order(s): {r.exact} exact, {r.ambiguous} ambiguous, {r.split} split."
            if r.total else "No new matches."
        )
        return back(msg=text)

    @router.post("/orders/reparse")
    def reparse(use_ai: str = Form("")):
        r = orders.reparse(ctx, use_ai=bool(use_ai))
        return back(msg=f"Re-parsed: {r['parsed']} parsed, {r['ignored']} ignored, {r['unparsed']} still unparsed; {r['matched']} new match(es).")

    @router.post("/orders/{order_id}/unlink")
    def unlink(order_id: int):
        with ctx.db() as conn:
            matching.unlink(conn, order_id)
        return back(msg="Unlinked.")

    @router.post("/orders/{order_id}/link")
    def link(order_id: int, txn_id: str = Form("")):
        try:
            tid = int(txn_id)
        except ValueError:
            return back(msg="Pick a transaction to link.")
        with ctx.db() as conn:
            if conn.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone() is None:
                raise HTTPException(404)
            matching.link_manually(conn, order_id, tid)
        return back(msg="Linked.")

    return router
