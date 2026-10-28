import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def new_account(client, name="Asha"):
    r = client.post("/accounts", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def tx(client, acc, type_, amount, ref, **extra):
    return client.post(
        f"/accounts/{acc}/transactions",
        json={"type": type_, "amount": amount, "reference_id": ref, **extra},
    )


def balance(client, acc):
    return client.get(f"/accounts/{acc}/balance").json()["balance"]


# --- 1. accounts -------------------------------------------------------------

def test_create_account_starts_at_zero(client):
    r = client.post("/accounts", json={"name": "Asha"})
    body = r.json()
    assert r.status_code == 201
    assert body["balance"] == 0 and body["name"] == "Asha"
    assert body["id"] and body["created_at"]


def test_account_ids_unique(client):
    assert len({new_account(client) for _ in range(20)}) == 20


@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": "   "}, {"name": None}])
def test_name_required(client, payload):
    r = client.post("/accounts", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["balance", "id", "created_at"])
def test_client_cannot_set_system_fields(client, field):
    r = client.post("/accounts", json={"name": "Asha", field: 1000})
    assert r.status_code == 422


# --- 2. transactions -----------------------------------------------------------

def test_credit_and_debit(client):
    acc = new_account(client)
    assert tx(client, acc, "credit", 100, "r1").status_code == 201
    assert tx(client, acc, "credit", 50, "r2").status_code == 201
    assert balance(client, acc) == 150
    r = tx(client, acc, "debit", 30, "r3")
    assert r.status_code == 201 and r.json()["type"] == "debit"
    assert balance(client, acc) == 120


def test_insufficient_balance_rejected(client):
    acc = new_account(client)
    tx(client, acc, "credit", 100, "r1")
    r = tx(client, acc, "debit", 150, "r2")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert balance(client, acc) == 100
    assert client.get(f"/accounts/{acc}/transactions").json()["pagination"]["total_records"] == 1


def test_exact_balance_debit_allowed(client):
    acc = new_account(client)
    tx(client, acc, "credit", 100, "r1")
    assert tx(client, acc, "debit", 100, "r2").status_code == 201
    assert balance(client, acc) == 0


def test_duplicate_reference_rejected(client):
    acc = new_account(client)
    first = tx(client, acc, "credit", 100, "dup").json()
    r = tx(client, acc, "credit", 999, "dup")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_TRANSACTION"
    assert err["details"]["existing_transaction"]["id"] == first["id"]
    assert balance(client, acc) == 100
    assert client.get(f"/accounts/{acc}/transactions").json()["pagination"]["total_records"] == 1


def test_duplicate_reported_before_insufficient_balance(client):
    acc = new_account(client)
    tx(client, acc, "credit", 100, "c1")
    tx(client, acc, "debit", 100, "d1")
    assert tx(client, acc, "debit", 100, "d1").json()["error"]["code"] == "DUPLICATE_TRANSACTION"


def test_same_reference_allowed_on_different_accounts(client):
    a, b = new_account(client, "A"), new_account(client, "B")
    assert tx(client, a, "credit", 10, "same").status_code == 201
    assert tx(client, b, "credit", 10, "same").status_code == 201


def test_transaction_on_missing_account(client):
    r = tx(client, "nope", "credit", 10, "r1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"type": "refund", "amount": 10, "reference_id": "r"}, "INVALID_TRANSACTION_TYPE"),
        ({"type": "credit", "amount": 0, "reference_id": "r"}, "INVALID_AMOUNT"),
        ({"type": "credit", "amount": -5, "reference_id": "r"}, "INVALID_AMOUNT"),
        ({"type": "credit", "amount": 1.234, "reference_id": "r"}, "INVALID_AMOUNT"),
        ({"type": "credit", "amount": "abc", "reference_id": "r"}, "INVALID_AMOUNT"),
        ({"type": "credit", "amount": 10}, "VALIDATION_ERROR"),
        ({"type": "credit", "amount": 10, "reference_id": "  "}, "VALIDATION_ERROR"),
    ],
)
def test_transaction_validation(client, payload, code):
    acc = new_account(client)
    r = client.post(f"/accounts/{acc}/transactions", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code
    assert balance(client, acc) == 0


def test_decimal_amounts_are_exact(client):
    acc = new_account(client)
    for i in range(10):
        tx(client, acc, "credit", 0.1, f"r{i}")
    assert balance(client, acc) == 1.0  # float math would give 0.9999999999999999


def test_malformed_json(client):
    acc = new_account(client)
    r = client.post(
        f"/accounts/{acc}/transactions", content="{bad", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST_BODY"


def test_non_object_body(client):
    r = client.post("/accounts", json=[1, 2])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST_BODY"


# --- 3. balance ----------------------------------------------------------------

def test_balance_shape_and_missing(client):
    acc = new_account(client)
    assert client.get(f"/accounts/{acc}/balance").json() == {"account_id": acc, "balance": 0}
    r = client.get("/accounts/missing/balance")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


# --- 4. history ----------------------------------------------------------------

def test_history_latest_first_filter_and_pagination(client):
    acc = new_account(client)
    for i in range(7):
        tx(client, acc, "credit", 10, f"c{i}")
    for i in range(3):
        tx(client, acc, "debit", 1, f"d{i}")

    r = client.get(f"/accounts/{acc}/transactions", params={"limit": 4}).json()
    assert r["pagination"] == {
        "page": 1, "limit": 4, "total_records": 10, "total_pages": 3, "has_next": True, "has_prev": False,
    }
    assert [t["reference_id"] for t in r["data"]] == ["d2", "d1", "d0", "c6"]

    last = client.get(f"/accounts/{acc}/transactions", params={"limit": 4, "page": 3}).json()
    assert [t["reference_id"] for t in last["data"]] == ["c1", "c0"]
    assert last["pagination"]["has_next"] is False

    debits = client.get(f"/accounts/{acc}/transactions", params={"type": "debit"}).json()
    assert debits["pagination"]["total_records"] == 3
    assert {t["type"] for t in debits["data"]} == {"debit"}

    credits = client.get(f"/accounts/{acc}/transactions", params={"type": "credit"}).json()
    assert credits["pagination"]["total_records"] == 7

    stamps = [t["created_at"] for t in client.get(f"/accounts/{acc}/transactions").json()["data"]]
    assert stamps == sorted(stamps, reverse=True)


def test_history_page_past_end_is_empty(client):
    acc = new_account(client)
    tx(client, acc, "credit", 10, "c")
    r = client.get(f"/accounts/{acc}/transactions", params={"page": 5}).json()
    assert r["data"] == [] and r["pagination"]["total_pages"] == 1


def test_history_empty_account(client):
    acc = new_account(client)
    r = client.get(f"/accounts/{acc}/transactions").json()
    assert r["data"] == [] and r["pagination"]["total_pages"] == 0


@pytest.mark.parametrize(
    "params,code",
    [
        ({"page": 0}, "INVALID_PAGINATION"),
        ({"limit": 0}, "INVALID_PAGINATION"),
        ({"limit": 101}, "INVALID_PAGINATION"),
        ({"page": "x"}, "INVALID_PAGINATION"),
        ({"type": "refund"}, "INVALID_TRANSACTION_TYPE"),
    ],
)
def test_history_bad_params(client, params, code):
    acc = new_account(client)
    r = client.get(f"/accounts/{acc}/transactions", params=params)
    assert r.status_code == 422 and r.json()["error"]["code"] == code


def test_history_missing_account(client):
    assert client.get("/accounts/missing/transactions").status_code == 404


def test_history_uses_index_without_sort(client, tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    for sql in (
        "SELECT * FROM transactions WHERE account_id='a' ORDER BY created_at DESC, rowid DESC LIMIT 5",
        "SELECT * FROM transactions WHERE account_id='a' AND type='debit' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 5",
    ):
        plan = " ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "USING INDEX" in plan and "TEMP B-TREE" not in plan, plan


# --- 5. integrity --------------------------------------------------------------

def test_balance_consistency_example(client):
    acc = new_account(client)
    for i, (t, a) in enumerate([("credit", 100), ("credit", 50), ("debit", 30), ("debit", 20)]):
        tx(client, acc, t, a, f"r{i}")
    assert balance(client, acc) == 100


def test_transactions_immutable_via_api(client):
    acc = new_account(client)
    tid = tx(client, acc, "credit", 10, "r").json()["id"]
    for method in ("put", "patch", "delete"):
        r = getattr(client, method)(f"/accounts/{acc}/transactions")
        assert r.status_code == 405
    assert client.delete(f"/accounts/{acc}/transactions/{tid}").status_code == 404


def test_transactions_immutable_in_database(client, tmp_path):
    acc = new_account(client)
    tx(client, acc, "credit", 10, "r")
    conn = sqlite3.connect(tmp_path / "test.db")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE transactions SET amount = 1")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM transactions")


def test_db_constraints_block_negative_balance(client, tmp_path):
    acc = new_account(client)
    conn = sqlite3.connect(tmp_path / "test.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE accounts SET balance = -1 WHERE id = ?", (acc,))


def test_atomicity_failed_balance_update_rolls_back_insert(client, tmp_path):
    """Force the balance UPDATE to fail after the INSERT: nothing may persist."""
    acc = new_account(client)
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        "CREATE TRIGGER boom BEFORE UPDATE ON accounts BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        tx(client, acc, "credit", 10, "r")
    conn.execute("DROP TRIGGER boom")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert balance(client, acc) == 0


def test_concurrent_debits_never_overdraw(client):
    acc = new_account(client)
    tx(client, acc, "credit", 100, "seed")
    results = []

    def spend(i):
        results.append(tx(client, acc, "debit", 30, f"d{i}").status_code)

    threads = [threading.Thread(target=spend, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(201) == 3
    assert balance(client, acc) == 10


def test_concurrent_duplicates_create_one(client):
    acc = new_account(client)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(tx(client, acc, "credit", 10, "same").status_code))
        for _ in range(10)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [201] + [409] * 9
    assert balance(client, acc) == 10


# --- 9. transfers --------------------------------------------------------------

def transfer(client, src, dst, amount, ref="t1"):
    return client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": amount, "reference_id": ref},
    )


def test_transfer_moves_money(client):
    a, b = new_account(client, "A"), new_account(client, "B")
    tx(client, a, "credit", 100, "s")
    tx(client, b, "credit", 50, "s")
    r = transfer(client, a, b, 30)
    assert r.status_code == 201
    assert balance(client, a) == 70 and balance(client, b) == 80
    body = r.json()
    a_hist = client.get(f"/accounts/{a}/transactions", params={"type": "debit"}).json()["data"]
    b_hist = client.get(f"/accounts/{b}/transactions", params={"type": "credit"}).json()["data"]
    assert a_hist[0]["id"] == body["debit_transaction_id"] and a_hist[0]["transfer_id"] == body["id"]
    assert b_hist[0]["id"] == body["credit_transaction_id"]


def test_transfer_insufficient_is_all_or_nothing(client):
    a, b = new_account(client, "A"), new_account(client, "B")
    tx(client, a, "credit", 10, "s")
    r = transfer(client, a, b, 30)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert balance(client, a) == 10 and balance(client, b) == 0
    assert client.get(f"/accounts/{b}/transactions").json()["pagination"]["total_records"] == 0


def test_transfer_missing_account(client):
    a = new_account(client)
    tx(client, a, "credit", 10, "s")
    r = transfer(client, a, "ghost", 5)
    assert r.status_code == 404
    assert balance(client, a) == 10


def test_transfer_same_account_rejected(client):
    a = new_account(client)
    assert transfer(client, a, a, 5).status_code == 422


def test_transfer_idempotent(client):
    a, b = new_account(client, "A"), new_account(client, "B")
    tx(client, a, "credit", 100, "s")
    first = transfer(client, a, b, 30).json()
    r = transfer(client, a, b, 30)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_TRANSFER"
    assert r.json()["error"]["details"]["existing_transfer"]["id"] == first["id"]
    assert balance(client, a) == 70 and balance(client, b) == 30


def test_transfer_credit_leg_failure_rolls_back_debit(client, tmp_path):
    a, b = new_account(client, "A"), new_account(client, "B")
    tx(client, a, "credit", 100, "s")
    conn = sqlite3.connect(tmp_path / "test.db")
    # Let the debit leg through, then fail on the credit leg.
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON transactions WHEN NEW.type = 'credit' "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        transfer(client, a, b, 30)
    conn.execute("DROP TRIGGER boom")
    conn.commit()
    assert balance(client, a) == 100 and balance(client, b) == 0
    assert conn.execute("SELECT COUNT(*) FROM transfers").fetchone()[0] == 0


# --- 10. edge cases / hardening -----------------------------------------------

@pytest.mark.parametrize("page", [10**6, 2**62, 10**30])
def test_huge_page_is_empty_not_500(client, page):
    acc = new_account(client)
    tx(client, acc, "credit", 10, "c")
    r = client.get(f"/accounts/{acc}/transactions", params={"page": page})
    assert r.status_code == 200
    assert r.json()["data"] == [] and r.json()["pagination"]["total_records"] == 1


def test_credit_cannot_overflow_balance(client, tmp_path):
    acc = new_account(client)
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (2**63 - 100, acc))
    conn.commit()
    r = tx(client, acc, "credit", "9999999999999.99", "big")
    assert r.status_code == 422 and r.json()["error"]["code"] == "BALANCE_LIMIT_EXCEEDED"
    row = conn.execute("SELECT balance, typeof(balance) FROM accounts WHERE id = ?", (acc,)).fetchone()
    assert row == (2**63 - 100, "integer")


def test_db_rejects_non_integer_balance(client, tmp_path):
    acc = new_account(client)
    conn = sqlite3.connect(tmp_path / "test.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE accounts SET balance = 1.5 WHERE id = ?", (acc,))


def test_mixed_validation_errors_are_generic(client):
    acc = new_account(client)
    r = client.post(f"/accounts/{acc}/transactions", json={"type": "credit", "amount": 0})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert {d["field"] for d in r.json()["error"]["details"]} == {"amount", "reference_id"}


def test_405_includes_allow_header(client):
    acc = new_account(client)
    r = client.delete(f"/accounts/{acc}/transactions")
    assert r.status_code == 405 and set(r.headers["allow"].replace(" ", "").split(",")) == {"GET", "POST"}


def test_transfer_reference_prefix_reserved(client):
    acc = new_account(client)
    r = tx(client, acc, "credit", 10, "transfer:anything")
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_locked_database_returns_503(client, tmp_path, monkeypatch):
    acc = new_account(client)
    monkeypatch.setattr("app.db.BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(tmp_path / "test.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    try:
        r = tx(client, acc, "credit", 10, "r")
    finally:
        holder.execute("ROLLBACK")
    assert r.status_code == 503 and r.json()["error"]["code"] == "SERVICE_BUSY"
    assert r.headers["retry-after"] == "1"
    assert tx(client, acc, "credit", 10, "r").status_code == 201  # the retry succeeds


def test_history_reads_one_snapshot(client, tmp_path, monkeypatch):
    """A write committed between reading the counter and the rows must not leak in."""
    acc = new_account(client)
    tx(client, acc, "credit", 10, "c0")
    import app.service as service

    real_fetch = service._fetch_account

    def fetch_then_concurrent_write(conn, account_id):
        row = real_fetch(conn, account_id)
        other = sqlite3.connect(tmp_path / "test.db", isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO transactions (id, account_id, type, amount, reference_id, created_at) "
            "VALUES ('x', ?, 'credit', 1, 'sneaky', '9999')", (account_id,),
        )
        other.execute("UPDATE accounts SET balance = balance + 1, credit_count = credit_count + 1 WHERE id = ?",
                      (account_id,))
        other.execute("COMMIT")
        return row

    monkeypatch.setattr(service, "_fetch_account", fetch_then_concurrent_write)
    r = client.get(f"/accounts/{acc}/transactions").json()
    assert r["pagination"]["total_records"] == len(r["data"]) == 1


def test_transfer_conserves_money_under_concurrency(client):
    accs = [new_account(client, f"A{i}") for i in range(4)]
    for a in accs:
        tx(client, a, "credit", 100, "seed")
    results = []

    def move(i):
        src, dst = accs[i % 4], accs[(i + 1 + i % 3) % 4]  # offset 1..3: never the same account
        results.append(transfer(client, src, dst, 35, f"t{i}").status_code)

    threads = [threading.Thread(target=move, args=(i,)) for i in range(24)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert set(results) <= {201, 422}
    balances = [balance(client, a) for a in accs]
    assert sum(balances) == 400 and min(balances) >= 0
    for a in accs:
        hist = client.get(f"/accounts/{a}/transactions", params={"limit": 100}).json()["data"]
        net = sum(t["amount"] if t["type"] == "credit" else -t["amount"] for t in hist)
        assert net == balance(client, a)


def test_duplicate_transfer_takes_priority_over_insufficient(client):
    a, b = new_account(client, "A"), new_account(client, "B")
    tx(client, a, "credit", 30, "s")
    assert transfer(client, a, b, 30).status_code == 201
    r = transfer(client, a, b, 30)  # balance is now 0, but it's a retry
    assert r.json()["error"]["code"] == "DUPLICATE_TRANSFER"


@pytest.mark.parametrize("amount,expected", [("50.25", 50.25), (" 7 ", 7), (1e2, 100), ("0.01", 0.01)])
def test_amount_formats_accepted(client, amount, expected):
    acc = new_account(client)
    r = tx(client, acc, "credit", amount, "r")
    assert r.status_code == 201 and r.json()["amount"] == expected


@pytest.mark.parametrize("amount", [True, "NaN", "Infinity", None, {"v": 1}, 10**16, "1_000", "1_0.50"])
def test_amount_rejects_junk(client, amount):
    acc = new_account(client)
    r = tx(client, acc, "credit", amount, "r")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_AMOUNT"


def test_transfer_rejects_digit_separators(client):
    a, b = new_account(client, "A"), new_account(client, "B")
    tx(client, a, "credit", 5000, "s")
    r = transfer(client, a, b, "1_000")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_AMOUNT"
    assert balance(client, a) == 5000 and balance(client, b) == 0


def test_length_limits(client):
    acc = new_account(client)
    assert client.post("/accounts", json={"name": "x" * 201}).status_code == 422
    assert tx(client, acc, "credit", 1, "r" * 101).status_code == 422
    assert tx(client, acc, "credit", 1, "r", description="d" * 501).status_code == 422
    assert tx(client, acc, "credit", 1, "r" * 100, description="d" * 500).status_code == 201


def test_whitespace_trimmed_on_reference(client):
    acc = new_account(client)
    assert tx(client, acc, "credit", 1, " ref ").json()["reference_id"] == "ref"
    assert tx(client, acc, "credit", 1, "ref").status_code == 409


def _init_worker(path, barrier, results):
    import os

    os.environ["LEDGER_DB_PATH"] = path
    from app.db import init_db

    barrier.wait()  # every worker hits the fresh file at the same instant
    try:
        init_db()
        results.put("ok")
    except Exception as exc:  # noqa: BLE001 - report, don't crash the child
        results.put(repr(exc))


def test_parallel_workers_initialise_same_db(tmp_path):
    """`uvicorn --workers N` on a fresh file: no worker may fail startup."""
    import multiprocessing as mp

    for i in range(5):
        barrier, results = mp.Barrier(6), mp.Queue()
        procs = [
            mp.Process(target=_init_worker, args=(str(tmp_path / f"w{i}.db"), barrier, results))
            for _ in range(6)
        ]
        [p.start() for p in procs]
        [p.join() for p in procs]
        assert [results.get() for _ in procs] == ["ok"] * 6
