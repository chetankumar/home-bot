"""Finance pages: dashboard, transactions, recipients, review, settings."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hub.plugin import AppContext
from hub.services.ai import AIError

from . import categories as cats_db
from . import stats, tagging
from .category_routes import build_category_router
from .order_routes import build_order_router
from .queries import recipient_names, txn_rows
from .parsers import Parsed, parse_date, to_paise
from . import orders as orders_mod
from .sync import JOB_ID, insert_transaction, reparse, senders

INSTRUMENTS = ["upi", "credit_card", "debit_card", "netbanking", "atm"]


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    router.include_router(build_category_router(ctx))
    router.include_router(build_order_router(ctx))

    def today() -> date:
        return datetime.now(ctx.tz).date()

    def categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return cats_db.listing(conn)

    def budget() -> int | None:
        return ctx.kv.get("budget_paise")

    def local_model() -> str:
        try:
            return ":".join(ctx.ai.resolve())
        except AIError as e:  # misconfigured alias: show it rather than fail the page
            return f"not configured ({e})"

    def back(path: str) -> RedirectResponse:
        return RedirectResponse(ctx.url(path), status_code=303)

    def sync_context() -> dict:
        return {
            "run": ctx.scheduler.last_run(JOB_ID),
            "running": ctx.scheduler.is_running(JOB_ID),
            "last_sync": ctx.kv.get("last_sync"),
            "connected": ctx.gmail.connected,
        }

    # -- dashboard -------------------------------------------------------------------
    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, month: str | None = None):
        t = today()
        y, m = stats.parse_ym(month, t)
        with ctx.db() as conn:
            b = stats.burn(conn, y, m, t, budget())
            cats = stats.by_category(conn, y, m)
            top = stats.top_recipients(conn, y, m)
            data = {
                "burn": b,
                "categories": [(n, v, stats.inr(v, False)) for n, v in cats],
                "top": top,
                "untagged": stats.untagged_count(conn),
                "unparsed": conn.execute("SELECT COUNT(*) FROM emails WHERE status = 'unparsed'").fetchone()[0],
                "credits": stats.credits(conn, y, m),
            }
        return ctx.render(
            request,
            "dashboard.html",
            ym=f"{y:04d}-{m:02d}",
            month_label=date(y, m, 1).strftime("%B %Y"),
            prev=stats.shift_month(y, m, -1),
            next=stats.shift_month(y, m, 1),
            is_current=(y, m) == (t.year, t.month),
            **data,
            **sync_context(),
        )

    @router.post("/sync", response_class=HTMLResponse)
    def sync_now(request: Request):
        if ctx.gmail.connected:
            ctx.scheduler.run_now(JOB_ID)
        return sync_status(request, watch=True)

    @router.get("/sync/status", response_class=HTMLResponse)
    def sync_status(request: Request, watch: bool = False):
        context = sync_context()
        headers = None
        if watch and not context["running"]:
            headers = {"HX-Refresh": "true"}  # finished: reload so the numbers update
        return ctx.render(request, "_sync_status.html", headers=headers, watch=watch, **context)

    # -- transactions ------------------------------------------------------------------
    @router.get("/transactions", response_class=HTMLResponse)
    def transactions(request: Request, month: str | None = None):
        t = today()
        y, m = stats.parse_ym(month, t)
        start, end = stats.month_bounds(y, m)
        with ctx.db() as conn:
            rows = txn_rows(conn, "t.occurred_at >= ? AND t.occurred_at < ?", (start, end))
            cats, recips = categories(conn), recipient_names(conn)
        return ctx.render(
            request,
            "transactions.html",
            rows=rows,
            categories=cats,
            recipients=recips,
            ym=f"{y:04d}-{m:02d}",
            month_label=date(y, m, 1).strftime("%B %Y"),
            prev=stats.shift_month(y, m, -1),
            next=stats.shift_month(y, m, 1),
        )

    @router.post("/transactions/{txn_id}", response_class=HTMLResponse)
    def edit_transaction(
        request: Request, txn_id: int, category_id: str = Form(""), recipient_id: str = Form("")
    ):
        cid, rid = _int(category_id), _int(recipient_id)
        with ctx.db() as conn:
            if conn.execute("SELECT 1 FROM transactions WHERE id = ?", (txn_id,)).fetchone() is None:
                raise HTTPException(404)
            conn.execute("UPDATE transactions SET recipient_id = ? WHERE id = ?", (rid, txn_id))
            if cid is not None:
                conn.execute(
                    "UPDATE transactions SET category_id = ?, category_manual = 1 WHERE id = ?",
                    (cid, txn_id),
                )
            else:  # cleared: fall back to the recipient's category
                conn.execute(
                    "UPDATE transactions SET category_manual = 0, category_id ="
                    " (SELECT category_id FROM recipients WHERE id = transactions.recipient_id)"
                    " WHERE id = ?",
                    (txn_id,),
                )
            row = txn_rows(conn, "t.id = ?", (txn_id,))[0]
            cats, recips = categories(conn), recipient_names(conn)
        return ctx.render(request, "_txn_row.html", t=row, categories=cats, recipients=recips)

    # -- recipients ----------------------------------------------------------------------
    @router.get("/recipients", response_class=HTMLResponse)
    def recipients_page(request: Request, saved: str | None = None):
        with ctx.db() as conn:
            queue = stats.untagged(conn, limit=100)
            known = [dict(r) for r in conn.execute(
                "SELECT r.*, c.name AS category,"
                " (SELECT COUNT(*) FROM transactions t WHERE t.recipient_id = r.id) AS n,"
                " (SELECT COALESCE(SUM(amount_paise), 0) FROM transactions t"
                "   WHERE t.recipient_id = r.id AND t.direction = 'debit') AS total"
                " FROM recipients r LEFT JOIN categories c ON c.id = r.category_id ORDER BY r.name"
            ).fetchall()]
            keys: dict[int, list[dict]] = {}
            for k in conn.execute("SELECT * FROM recipient_keys ORDER BY key"):
                keys.setdefault(k["recipient_id"], []).append(dict(k))
            cats = categories(conn)
        return ctx.render(
            request,
            "recipients.html",
            queue=queue,
            known=known,
            keys=keys,
            categories=cats,
            saved=saved,
            ai_ready=ctx.ai.available(),
        )

    @router.post("/recipients")
    def create_recipient(
        name: str = Form(...),
        category_id: str = Form(""),
        key: str = Form(""),
        kind: str = Form(""),
    ):
        name = name.strip()
        if not name:
            return back("/recipients")
        # Keys typed by hand (add form) are normalised the same way sync does it.
        norm_key, norm_kind = (key.strip(), kind) if kind in ("upi", "merchant") else tagging.counterparty_key(key)
        if norm_kind == "upi":
            norm_key = norm_key.lower()
        with ctx.db() as conn:
            rid = tagging.find_or_create_recipient(conn, name, _int(category_id))
            n = tagging.assign_key(conn, norm_key, norm_kind, rid) if norm_key else 0
        return back(f"/recipients?saved={name} ({n} transaction{'s' if n != 1 else ''} tagged)")

    @router.post("/recipients/{rid}")
    def update_recipient(rid: int, name: str = Form(...), category_id: str = Form("")):
        with ctx.db() as conn:
            try:
                conn.execute(
                    "UPDATE recipients SET name = ?, category_id = ? WHERE id = ?",
                    (name.strip(), _int(category_id), rid),
                )
            except sqlite3.IntegrityError:
                return back(f"/recipients?saved=Another recipient is already called {name}")
            tagging.retag_recipient(conn, rid)
        return back("/recipients")

    @router.post("/recipients/{rid}/keys")
    def add_key(rid: int, key: str = Form(...)):
        k, kind = tagging.counterparty_key(key)
        if k:
            with ctx.db() as conn:
                tagging.assign_key(conn, k, kind, rid)
        return back("/recipients")

    @router.post("/recipients/{rid}/delete")
    def delete_recipient(rid: int):
        with ctx.db() as conn:
            conn.execute(
                "UPDATE transactions SET category_id = NULL WHERE recipient_id = ? AND category_manual = 0",
                (rid,),
            )
            conn.execute("DELETE FROM recipients WHERE id = ?", (rid,))
        return back("/recipients")

    @router.post("/keys/{key_id}/delete")
    def delete_key(key_id: int):
        with ctx.db() as conn:
            tagging.remove_key(conn, key_id)
        return back("/recipients")

    # -- review ---------------------------------------------------------------------------
    @router.get("/review", response_class=HTMLResponse)
    def review(request: Request, msg: str | None = None):
        with ctx.db() as conn:
            emails = [dict(r) for r in conn.execute(
                "SELECT * FROM emails WHERE status = 'unparsed' ORDER BY received_at DESC LIMIT 200"
            ).fetchall()]
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM emails GROUP BY status").fetchall())
        return ctx.render(
            request, "review.html", emails=emails, counts=counts, msg=msg, instruments=INSTRUMENTS
        )

    @router.post("/review/reparse")
    def review_reparse(use_ai: str = Form("")):
        result = reparse(ctx, use_ai=bool(use_ai))
        return back(
            f"/review?msg=Re-parsed: {result['parsed']} parsed, {result['ignored']} ignored,"
            f" {result['unparsed']} still unparsed."
        )

    @router.post("/review/{gmail_id}/ignore")
    def review_ignore(gmail_id: str):
        with ctx.db() as conn:
            conn.execute(
                "UPDATE emails SET status = 'ignored', parser = 'manual' WHERE gmail_id = ?",
                (gmail_id,),
            )
        return back("/review")

    @router.post("/review/{gmail_id}/manual")
    def review_manual(
        gmail_id: str,
        amount: str = Form(...),
        direction: str = Form("debit"),
        instrument: str = Form("upi"),
        counterparty: str = Form(""),
        on: str = Form(""),
    ):
        try:
            paise = to_paise(amount)
        except ValueError:
            return back("/review?msg=Amount must be a number.")
        if paise <= 0 or direction not in ("debit", "credit") or instrument not in INSTRUMENTS:
            return back("/review?msg=Check the amount, direction and instrument.")
        with ctx.db() as conn:
            row = conn.execute("SELECT received_at FROM emails WHERE gmail_id = ?", (gmail_id,)).fetchone()
            if row is None:
                raise HTTPException(404)
            parsed = Parsed(
                amount_paise=paise,
                direction=direction,
                instrument=instrument,
                counterparty_raw=counterparty.strip() or None,
                occurred_on=parse_date(on),
                parser="manual",
            )
            insert_transaction(conn, gmail_id, row["received_at"], parsed, "manual")
            conn.execute(
                "UPDATE emails SET status = 'parsed', parser = 'manual', error = NULL WHERE gmail_id = ?",
                (gmail_id,),
            )
        return back("/review?msg=Saved.")

    # -- settings -------------------------------------------------------------------------
    @router.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: bool = False):
        with ctx.db() as conn:
            hint = stats.trailing_average(conn, today())
        return ctx.render(
            request,
            "settings.html",
            budget=budget(),
            senders=senders(ctx),
            amazon_senders=orders_mod.senders(ctx),
            hint=hint,
            saved=saved,
            connected=ctx.gmail.connected,
            ai_ready=ctx.ai.available(),
            local_model=local_model(),
            sync_cron=ctx.config.get("sync_cron", "15 7 * * *"),
        )

    @router.post("/settings")
    def save_settings(
        budget_rupees: str = Form(""), sender_list: str = Form(""), amazon_sender_list: str = Form("")
    ):
        if budget_rupees.strip():
            try:
                ctx.kv.set("budget_paise", to_paise(budget_rupees.strip()))
            except ValueError:
                pass
        else:
            ctx.kv.delete("budget_paise")
        addrs = [s.strip().lower() for s in sender_list.replace(",", "\n").splitlines() if s.strip()]
        if addrs:
            ctx.kv.set("senders", addrs)
        amazon = [s.strip().lower() for s in amazon_sender_list.replace(",", "\n").splitlines() if s.strip()]
        if amazon:
            ctx.kv.set("amazon_senders", amazon)
        return back("/settings?saved=1")

    return router
