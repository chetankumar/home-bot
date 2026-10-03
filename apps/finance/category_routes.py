"""Dynamic categories: narrate a spend to categorise it, and manage the category list.

The model only ever *picks or proposes*. A new category is created by an explicit
confirm request, never as a side effect of narrating.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hub.plugin import AppContext

from . import categories as cats_db
from . import categorise, tagging
from .queries import recipient_names, txn_rows

MAX_DESCRIPTION = 200


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def build_category_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    def back(path: str, **params: str) -> RedirectResponse:
        return RedirectResponse(ctx.url(path) + ("?" + urlencode(params) if params else ""), status_code=303)

    # -- narrating a transaction ---------------------------------------------------------
    def row_response(request: Request, txn_id: int, **extra):
        with ctx.db() as conn:
            rows = txn_rows(conn, "t.id = ?", (txn_id,))
            if not rows:
                raise HTTPException(404)
            cats, recips = cats_db.listing(conn), recipient_names(conn)
        return ctx.render(
            request, "_txn_row.html", t=rows[0], categories=cats, recipients=recips, **extra
        )

    def must_exist(conn, txn_id: int) -> None:
        if conn.execute("SELECT 1 FROM transactions WHERE id = ?", (txn_id,)).fetchone() is None:
            raise HTTPException(404)

    def filed_note(name: str, created: bool, taught: str | None) -> str:
        note = f"Created category {name} and filed this under it" if created else f"Filed under {name}"
        return note + (f". {taught} now defaults to {name}." if taught else ".")

    @router.get("/transactions/{txn_id}/row", response_class=HTMLResponse)
    def transaction_row(request: Request, txn_id: int):
        return row_response(request, txn_id)

    @router.post("/transactions/{txn_id}/narrate", response_class=HTMLResponse)
    def narrate(request: Request, txn_id: int, narration: str = Form("")):
        narration = categorise.clean_narration(narration)
        note = None
        with ctx.db() as conn:
            must_exist(conn, txn_id)
            decision = categorise.decide(
                ctx, conn, narration, categorise.transaction_context(conn, txn_id)
            )
            if isinstance(decision, categorise.Matched):
                cats_db.assign_to_transaction(conn, txn_id, decision.category_id, narration)
                taught = cats_db.teach_recipient(conn, txn_id, decision.category_id)
                note = filed_note(decision.name, False, taught)
        if isinstance(decision, categorise.Proposal):
            return row_response(request, txn_id, proposal=decision, narration_text=narration)
        if isinstance(decision, categorise.Failed):
            return row_response(
                request, txn_id, error=decision.reason, narration_text=narration, open_manual=True
            )
        return row_response(request, txn_id, note=note)

    @router.post("/transactions/{txn_id}/narrate/confirm", response_class=HTMLResponse)
    def narrate_confirm(
        request: Request,
        txn_id: int,
        narration: str = Form(""),
        name: str = Form(""),
        counts_as_spend: str = Form("1"),
        category_id: str = Form(""),
    ):
        narration = categorise.clean_narration(narration)
        with ctx.db() as conn:
            must_exist(conn, txn_id)
            try:
                cid, created = _int(category_id), False
                if cid is not None:  # "use an existing category instead"
                    if cats_db.get(conn, cid) is None:
                        raise cats_db.CategoryError("no such category")
                else:
                    cid, created = cats_db.create(conn, name, counts_as_spend == "1", None, "ai")
                cats_db.assign_to_transaction(conn, txn_id, cid, narration)
                taught = cats_db.teach_recipient(conn, txn_id, cid)
                note = filed_note(cats_db.get(conn, cid)["name"], created, taught)
            except cats_db.CategoryError as e:
                conn.rollback()
                return row_response(request, txn_id, error=str(e), narration_text=narration, open_manual=True)
        return row_response(request, txn_id, note=note)

    # -- narrating a recipient (the "who is this?" queue) --------------------------------
    def queue_response(request: Request, q: dict, **extra):
        with ctx.db() as conn:
            cats = cats_db.listing(conn)
        return ctx.render(
            request, "_queue_form.html", q=q, categories=cats, ai_ready=ctx.ai.available(), **extra
        )

    def finish_queue(conn, name: str, key: str, kind: str, category_id: int | None) -> Response:
        rid = tagging.find_or_create_recipient(conn, name, category_id)
        n = tagging.assign_key(conn, key, kind, rid) if key else 0
        msg = f"{name} ({n} transaction{'s' if n != 1 else ''} tagged)"
        url = ctx.url("/recipients") + "?" + urlencode({"saved": msg})
        return Response(status_code=200, headers={"HX-Redirect": url})

    @router.get("/recipients/queue/form", response_class=HTMLResponse)
    def queue_form(request: Request, key: str, kind: str = "merchant", raw: str = ""):
        return queue_response(request, {"key": key, "kind": kind, "raw": raw})

    @router.post("/recipients/queue/save", response_class=HTMLResponse)
    def queue_save(
        request: Request,
        key: str = Form(...),
        kind: str = Form("merchant"),
        raw: str = Form(""),
        name: str = Form(""),
        narration: str = Form(""),
        category_id: str = Form(""),
    ):
        q = {"key": key, "kind": kind, "raw": raw}
        name, narration = name.strip(), categorise.clean_narration(narration)
        if not name:
            return queue_response(request, q, error="Give them a name first.", narration_text=narration)
        with ctx.db() as conn:
            cid = _int(category_id)
            existing = conn.execute(
                "SELECT category_id FROM recipients WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            # Adding another UPI id / merchant name to someone who already has a category
            # needs no model call.
            if cid is None and narration and not (existing and existing["category_id"]):
                decision = categorise.decide(
                    ctx, conn, narration, categorise.recipient_context(conn, key, name)
                )
                if isinstance(decision, categorise.Proposal):
                    return queue_response(
                        request, q, proposal=decision, person=name, narration_text=narration
                    )
                if isinstance(decision, categorise.Failed):
                    return queue_response(
                        request, q, error=decision.reason, person=name, narration_text=narration,
                        open_manual=True,
                    )
                cid = decision.category_id
            return finish_queue(conn, name, key, kind, cid)

    @router.post("/recipients/queue/confirm", response_class=HTMLResponse)
    def queue_confirm(
        request: Request,
        key: str = Form(...),
        kind: str = Form("merchant"),
        raw: str = Form(""),
        name: str = Form(""),
        narration: str = Form(""),
        category_name: str = Form(""),
        counts_as_spend: str = Form("1"),
        category_id: str = Form(""),
    ):
        q = {"key": key, "kind": kind, "raw": raw}
        with ctx.db() as conn:
            try:
                cid = _int(category_id)
                if cid is None:
                    cid, _ = cats_db.create(conn, category_name, counts_as_spend == "1", None, "ai")
                elif cats_db.get(conn, cid) is None:
                    raise cats_db.CategoryError("no such category")
            except cats_db.CategoryError as e:
                conn.rollback()
                return queue_response(
                    request, q, error=str(e), person=name, narration_text=narration, open_manual=True
                )
            return finish_queue(conn, name.strip(), key, kind, cid)

    # -- the Categories page ---------------------------------------------------------------
    @router.get("/categories", response_class=HTMLResponse)
    def categories_page(request: Request, msg: str | None = None, error: str | None = None):
        with ctx.db() as conn:
            rows = [dict(c) for c in cats_db.listing(conn)]
            usage = cats_db.stats(conn)
        for c in rows:
            c["n"] = usage.get(c["id"], {}).get("n", 0)
            c["total"] = usage.get(c["id"], {}).get("total", 0)
        return ctx.render(request, "categories.html", rows=rows, msg=msg, error=error)

    def clean_description(text: str) -> str:
        return " ".join(text.split())[:MAX_DESCRIPTION]

    @router.post("/categories")
    def add_category(
        name: str = Form(""), description: str = Form(""), counts_as_spend: str = Form("")
    ):
        try:
            with ctx.db() as conn:
                _, created = cats_db.create(
                    conn, name, bool(counts_as_spend), clean_description(description), "user"
                )
        except cats_db.CategoryError as e:
            return back("/categories", error=str(e))
        return back("/categories", msg="Added." if created else "That category already exists.")

    @router.post("/categories/{category_id}")
    def edit_category(
        category_id: int,
        name: str = Form(""),
        description: str = Form(""),
        counts_as_spend: str = Form(""),
    ):
        try:
            with ctx.db() as conn:
                if cats_db.get(conn, category_id) is None:
                    raise HTTPException(404)
                cats_db.update(
                    conn, category_id, name, clean_description(description), bool(counts_as_spend)
                )
        except cats_db.CategoryError as e:
            return back("/categories", error=str(e))
        return back("/categories", msg="Saved.")

    @router.post("/categories/{category_id}/merge")
    def merge_category(category_id: int, target_id: str = Form("")):
        try:
            with ctx.db() as conn:
                tid = _int(target_id)
                if tid is None:
                    raise cats_db.CategoryError("pick a category to merge into")
                cats_db.merge(conn, category_id, tid)
        except cats_db.CategoryError as e:
            return back("/categories", error=str(e))
        return back("/categories", msg="Merged.")

    @router.post("/categories/{category_id}/delete")
    def delete_category(category_id: int):
        with ctx.db() as conn:
            cats_db.delete(conn, category_id)
        return back("/categories", msg="Deleted. Its transactions are now uncategorised.")

    return router
