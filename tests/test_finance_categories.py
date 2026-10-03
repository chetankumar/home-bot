"""Dynamic categories and narration-driven categorisation."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from apps.finance import categories as cats
from apps.finance import stats
from tests.conftest import login
from tests.test_finance import at, count, fin, seed  # noqa: F401  (fin is a fixture)

BASE = "/apps/finance"


def choice(category, is_new=False, spend=True):
    return {"category": category, "is_new": is_new, "counts_as_spend": spend}


@pytest.fixture
def client(fin):  # noqa: F811
    return login(TestClient(fin.app))


def cat_id(fin, name):  # noqa: F811
    return count(fin.ctx, "SELECT id FROM categories WHERE name = ?", name)


def first_txn(fin):  # noqa: F811
    return count(fin.ctx, "SELECT id FROM transactions ORDER BY id LIMIT 1")


def row(fin, sql, *args):  # noqa: F811
    with fin.ctx.db() as conn:
        return dict(conn.execute(sql, args).fetchone())


# -- names and matching -------------------------------------------------------------------
def test_normalise_name():
    assert cats.normalise_name("  pet   supplies. ") == "Pet Supplies"
    assert cats.normalise_name("Eating Out") == "Eating Out"
    for bad in ["", "   ", "Uncategorised", "x" * 41]:
        with pytest.raises(cats.CategoryError):
            cats.normalise_name(bad)


def test_find_matches_case_and_plurals_but_not_lookalikes(fin):  # noqa: F811
    with fin.ctx.db() as conn:
        assert cats.find(conn, "groceries")["name"] == "Groceries"
        assert cats.find(conn, "Grocery")["name"] == "Groceries"
        assert cats.find(conn, "bill")["name"] == "Bills"
        assert cats.find(conn, "Shipping") is None  # not 'Shopping'
        assert cats.find(conn, "Pets") is None


def test_seeded_categories_have_descriptions(fin):  # noqa: F811
    with fin.ctx.db() as conn:
        rows = cats.listing(conn)
    assert len(rows) == 9 and all(r["description"] and r["created_by"] == "seed" for r in rows)


# -- narrating a transaction -----------------------------------------------------------------
def test_narration_assigns_existing_category(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    fin.ollama.outputs = [choice("groceries")]
    r = client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate", data={"narration": "weekly vegetables"})
    assert r.status_code == 200 and "Filed under Groceries" in r.text
    t = row(fin, "SELECT * FROM transactions")
    assert (t["category_id"], t["category_manual"], t["narration"]) == (
        cat_id(fin, "Groceries"), 1, "weekly vegetables")
    prompt = fin.ollama.calls[0][2][-1].content
    assert "weekly vegetables" in prompt and "Groceries: Supermarkets" in prompt  # descriptions reach the model


def test_model_saying_new_for_an_existing_name_is_a_match_not_a_duplicate(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    fin.ollama.outputs = [choice("Grocery", is_new=True)]
    client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate", data={"narration": "veg"})
    assert count(fin.ctx, "SELECT COUNT(*) FROM categories") == 9
    assert row(fin, "SELECT category_id FROM transactions")["category_id"] == cat_id(fin, "Groceries")


def test_new_category_is_proposed_then_created_on_confirm(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 1800, "debit", None)])
    tid = first_txn(fin)
    fin.ollama.outputs = [choice("pets", is_new=True)]
    r = client.post(f"{BASE}/transactions/{tid}/narrate", data={"narration": "dog food and vet"})
    assert "Create <strong>Pets</strong>" in r.text
    assert count(fin.ctx, "SELECT COUNT(*) FROM categories") == 9  # nothing created yet
    assert row(fin, "SELECT category_id, narration FROM transactions") == {"category_id": None, "narration": None}

    data = {"narration": "dog food and vet", "name": "Pets", "counts_as_spend": "1"}
    r = client.post(f"{BASE}/transactions/{tid}/narrate/confirm", data=data)
    assert "Created category Pets" in r.text
    client.post(f"{BASE}/transactions/{tid}/narrate/confirm", data=data)  # double click
    assert count(fin.ctx, "SELECT COUNT(*) FROM categories WHERE name = 'Pets'") == 1
    pets = row(fin, "SELECT * FROM categories WHERE name = 'Pets'")
    assert (pets["created_by"], pets["counts_as_spend"]) == ("ai", 1)
    assert row(fin, "SELECT category_id FROM transactions")["category_id"] == pets["id"]


def test_proposal_can_be_a_non_spending_category(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 5000, "debit", None)])
    tid = first_txn(fin)
    client.post(f"{BASE}/transactions/{tid}/narrate/confirm",
                data={"narration": "sent to my savings", "name": "Savings", "counts_as_spend": "0"})
    assert row(fin, "SELECT counts_as_spend FROM categories WHERE name = 'Savings'")["counts_as_spend"] == 0
    with fin.ctx.db() as conn:
        assert stats.spent(conn, 2026, 10) == 0  # new categories flow straight into the maths


def test_confirm_with_existing_category_instead(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate/confirm",
                data={"narration": "x", "category_id": str(cat_id(fin, "Food"))})
    assert count(fin.ctx, "SELECT COUNT(*) FROM categories") == 9
    assert row(fin, "SELECT category_id FROM transactions")["category_id"] == cat_id(fin, "Food")


def test_unusable_proposed_name_is_rejected_server_side(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    r = client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate/confirm",
                    data={"narration": "x", "name": "Uncategorised"})
    assert "not a usable category name" in r.text
    assert count(fin.ctx, "SELECT COUNT(*) FROM categories") == 9


# -- teaching the recipient --------------------------------------------------------------------
def with_recipient(fin, category=None):  # noqa: F811
    with fin.ctx.db() as conn:
        cid = cat_id(fin, category) if category else None
        rid = conn.execute("INSERT INTO recipients(name, category_id) VALUES ('Swiggy', ?)", (cid,)).lastrowid
        conn.execute("UPDATE transactions SET recipient_id = ?", (rid,))
    return rid


def test_narration_teaches_a_recipient_with_no_category(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None), ("2026-10-03T10:00:00", 300, "debit", None)])
    rid = with_recipient(fin)
    fin.ollama.outputs = [choice("Food")]
    r = client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate", data={"narration": "dinner"})
    assert "Swiggy now defaults to Food" in r.text
    assert row(fin, "SELECT category_id FROM recipients WHERE id = ?", rid)["category_id"] == cat_id(fin, "Food")
    with fin.ctx.db() as conn:
        cats_by_txn = [r["category_id"] for r in conn.execute("SELECT category_id FROM transactions ORDER BY id")]
    assert cats_by_txn == [cat_id(fin, "Food")] * 2  # the other Swiggy spend followed the recipient


def test_narration_never_overrides_an_existing_recipient_category(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None), ("2026-10-03T10:00:00", 300, "debit", None)])
    rid = with_recipient(fin, "Food")
    fin.ollama.outputs = [choice("Groceries")]
    r = client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate", data={"narration": "supplies"})
    assert "defaults to" not in r.text
    assert row(fin, "SELECT category_id FROM recipients WHERE id = ?", rid)["category_id"] == cat_id(fin, "Food")
    assert row(fin, "SELECT category_id FROM transactions ORDER BY id")["category_id"] == cat_id(fin, "Groceries")


# -- failures ----------------------------------------------------------------------------------
def test_model_down_writes_nothing_and_offers_manual(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])

    def boom(*a, **k):
        raise ConnectionError("connection refused")

    fin.ollama.extract = boom
    r = client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate", data={"narration": "lunch"})
    assert "Pick a category manually" in r.text and "<details open>" in r.text and "lunch" in r.text
    assert row(fin, "SELECT category_id, narration FROM transactions") == {"category_id": None, "narration": None}


def test_invalid_model_output_twice_is_a_clean_failure(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    fin.ollama.outputs = ["not json", '{"category": ""}']
    r = client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate", data={"narration": "lunch"})
    assert "Pick a category manually" in r.text
    assert row(fin, "SELECT category_id FROM transactions")["category_id"] is None


def test_blank_narration_is_a_no_op_without_calling_the_model(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    r = client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate", data={"narration": "   "})
    assert "Write a short description" in r.text and fin.ollama.calls == []


def test_narration_only_ever_uses_the_local_provider(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    fin.ollama.outputs = [choice("Food")]
    client.post(f"{BASE}/transactions/{first_txn(fin)}/narrate", data={"narration": "lunch"})
    assert fin.anthropic.calls == []
    assert {u["provider"] for u in fin.app.state.hub.ai.usage_summary()} == {"ollama"}


def test_manual_fallback_still_works(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    client.post(f"{BASE}/transactions/{first_txn(fin)}", data={"category_id": str(cat_id(fin, "Health"))})
    assert row(fin, "SELECT category_id, category_manual FROM transactions") == {
        "category_id": cat_id(fin, "Health"), "category_manual": 1}


# -- recipients queue ----------------------------------------------------------------------------
def queue_data(**over):
    return {"key": "cp0@upi", "kind": "upi", "raw": "cp0", "name": "Ravi Vegetables",
            "narration": "weekly vegetables", **over}


def test_queue_narration_matches_existing_category(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    fin.ollama.outputs = [choice("Groceries")]
    r = client.post(f"{BASE}/recipients/queue/save", data=queue_data())
    assert r.headers["HX-Redirect"].startswith(f"{BASE}/recipients?saved=")
    rec = row(fin, "SELECT * FROM recipients")
    assert (rec["name"], rec["category_id"]) == ("Ravi Vegetables", cat_id(fin, "Groceries"))
    assert row(fin, "SELECT category_id, recipient_id FROM transactions")["category_id"] == cat_id(fin, "Groceries")


def test_queue_new_category_proposal_then_confirm(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    fin.ollama.outputs = [choice("Pets", is_new=True)]
    r = client.post(f"{BASE}/recipients/queue/save", data=queue_data(narration="dog food"))
    assert "Create <strong>Pets</strong>" in r.text
    assert count(fin.ctx, "SELECT COUNT(*) FROM recipients") == 0  # nothing saved yet
    r = client.post(f"{BASE}/recipients/queue/confirm",
                    data={**queue_data(narration="dog food"), "category_name": "Pets", "counts_as_spend": "1"})
    assert "HX-Redirect" in r.headers
    assert row(fin, "SELECT category_id FROM recipients")["category_id"] == cat_id(fin, "Pets")


def test_queue_failure_keeps_the_form_and_offers_manual(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    fin.ollama.extract = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
    r = client.post(f"{BASE}/recipients/queue/save", data=queue_data())
    assert "Pick a category manually" in r.text and 'value="Ravi Vegetables"' in r.text
    assert count(fin.ctx, "SELECT COUNT(*) FROM recipients") == 0
    # manual pick saves without the model
    r = client.post(f"{BASE}/recipients/queue/save",
                    data=queue_data(narration="", category_id=str(cat_id(fin, "Groceries"))))
    assert "HX-Redirect" in r.headers


def test_queue_existing_recipient_with_category_skips_the_model(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    with fin.ctx.db() as conn:
        conn.execute("INSERT INTO recipients(name, category_id) VALUES ('Ravi Vegetables', ?)", (cat_id(fin, "Groceries"),))
    client.post(f"{BASE}/recipients/queue/save", data=queue_data(narration="whatever"))
    assert fin.ollama.calls == []
    assert count(fin.ctx, "SELECT COUNT(*) FROM recipient_keys") == 1


# -- managing categories ---------------------------------------------------------------------------
def test_add_edit_and_duplicate_rejected(fin, client):  # noqa: F811
    client.post(f"{BASE}/categories", data={"name": "pets", "description": "Vet, food", "counts_as_spend": "1"})
    pets = row(fin, "SELECT * FROM categories WHERE name = 'Pets'")
    assert (pets["created_by"], pets["description"]) == ("user", "Vet, food")
    r = client.post(f"{BASE}/categories/{pets['id']}", data={"name": "food", "description": "x", "counts_as_spend": "1"},
                    follow_redirects=False)
    assert "already+exists" in r.headers["location"]
    assert row(fin, "SELECT name FROM categories WHERE id = ?", pets["id"])["name"] == "Pets"
    client.post(f"{BASE}/categories/{pets['id']}", data={"name": "Pets & Vets", "description": "", "counts_as_spend": ""})
    assert row(fin, "SELECT name, counts_as_spend FROM categories WHERE id = ?", pets["id"]) == {
        "name": "Pets & Vets", "counts_as_spend": 0}


def test_merge_moves_everything(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 100, "debit", "Food"), ("2026-10-03T10:00:00", 200, "debit", "Shopping")])
    with fin.ctx.db() as conn:
        conn.execute("INSERT INTO recipients(name, category_id) VALUES ('X', ?)", (cat_id(fin, "Shopping"),))
    client.post(f"{BASE}/categories/{cat_id(fin, 'Shopping')}/merge", data={"target_id": str(cat_id(fin, "Food"))})
    assert count(fin.ctx, "SELECT COUNT(*) FROM categories WHERE name = 'Shopping'") == 0
    assert count(fin.ctx, "SELECT COUNT(*) FROM transactions WHERE category_id = ?", cat_id(fin, "Food")) == 2
    assert row(fin, "SELECT category_id FROM recipients")["category_id"] == cat_id(fin, "Food")


def test_merge_into_itself_is_refused(fin):  # noqa: F811
    with fin.ctx.db() as conn, pytest.raises(cats.CategoryError):
        cats.merge(conn, cat_id(fin, "Food"), cat_id(fin, "Food"))


def test_delete_uncategorises_and_releases_manual_rows(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 100, "debit", "Food")])
    tid = first_txn(fin)
    client.post(f"{BASE}/transactions/{tid}", data={"category_id": str(cat_id(fin, "Food"))})
    assert row(fin, "SELECT category_manual FROM transactions")["category_manual"] == 1
    client.post(f"{BASE}/categories/{cat_id(fin, 'Food')}/delete")
    assert row(fin, "SELECT category_id, category_manual FROM transactions") == {"category_id": None, "category_manual": 0}


def test_toggling_counts_as_spend_changes_totals(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 100, "debit", "Food")])
    with fin.ctx.db() as conn:
        assert stats.spent(conn, 2026, 10) == 100_00
    client.post(f"{BASE}/categories/{cat_id(fin, 'Food')}", data={"name": "Food", "description": "", "counts_as_spend": ""})
    with fin.ctx.db() as conn:
        assert stats.spent(conn, 2026, 10) == 0


def test_pages_render(fin, client):  # noqa: F811
    seed(fin.ctx, [("2026-10-02T10:00:00", 450, "debit", None)])
    for path in ["/categories", "/transactions?month=2026-10", "/recipients"]:
        r = client.get(BASE + path)
        assert r.status_code == 200, path
    assert "Add a category" in client.get(BASE + "/categories").text
    assert "Categorise" in client.get(BASE + "/transactions?month=2026-10").text


# -- migration -------------------------------------------------------------------------------------
def test_migration_002_upgrades_an_existing_database(tmp_path):
    from hub.services.db import Database
    from tests.conftest import ROOT

    mig = tmp_path / "migrations"
    mig.mkdir()
    src = ROOT / "apps" / "finance" / "migrations"
    (mig / "001_init.sql").write_text((src / "001_init.sql").read_text(encoding="utf-8"), encoding="utf-8")
    db = Database(tmp_path / "f.db", mig)
    with db() as conn:
        conn.execute("INSERT INTO transactions(occurred_at, amount_paise, direction, instrument, category_id)"
                     " VALUES ('2026-10-01T10:00:00', 5000, 'debit', 'upi', 2)")
        conn.execute("INSERT INTO categories(name) VALUES ('Mine')")
    (mig / "002_dynamic_categories.sql").write_text((src / "002_dynamic_categories.sql").read_text(encoding="utf-8"), encoding="utf-8")
    assert Database(tmp_path / "f.db", mig).applied == ["002_dynamic_categories.sql"]
    with db() as conn:
        t = conn.execute("SELECT amount_paise, category_id, narration FROM transactions").fetchone()
        mine = conn.execute("SELECT description, created_by FROM categories WHERE name = 'Mine'").fetchone()
        seeded = conn.execute("SELECT description FROM categories WHERE name = 'Food'").fetchone()
    assert tuple(t) == (5000, 2, None) and tuple(mine) == (None, "seed") and seeded[0]
    assert isinstance(conn, sqlite3.Connection)
