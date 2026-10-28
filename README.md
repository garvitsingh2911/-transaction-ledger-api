# Transaction Ledger API

A backend for a simple transaction ledger: create accounts, post credit/debit
transactions, read balances and paginated history, and transfer money between
accounts atomically.

**Stack:** Python 3.9+ · FastAPI · SQLite (built into Python, nothing to install or run).

- [API documentation](API.md). Interactive Swagger UI at `/docs` once running.
- [Assumptions](#assumptions)
- [Design and complexity](#design-and-complexity)

---

## Run locally

```bash
cd assesment
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

- API: <http://localhost:8000>
- Swagger UI: <http://localhost:8000/docs> · ReDoc: <http://localhost:8000/redoc>

The SQLite file `ledger.db` and its schema are created on first start. To put it
somewhere else, set `LEDGER_DB_PATH=/path/to/file.db`.

### Run the tests

```bash
pip install -r requirements-dev.txt
pytest -v                                   # 78 automated tests
```

### Verify every feature against a running server

```bash
uvicorn app.main:app --port 8000            # terminal 1
python scripts/verify_live.py               # terminal 2 -> "88/88 checks passed"
```

`verify_live.py` uses only the standard library and sends real HTTP requests,
including 20 parallel debits and 20 parallel duplicates. It prints PASS/FAIL for
each of the brief's requirements.

There are 78 tests. They cover every rule in the brief: validation, duplicates,
insufficient balance, ordering, filtering, pagination, immutability, forced
mid-transaction failures (atomicity), concurrent debits, duplicates and transfers,
plus edge cases: huge page numbers, int64 balance overflow, a locked database (503),
consistent history snapshots under concurrent writes, and several server workers
initialising the same fresh database file at once.

### Quick tour with curl

```bash
# create two accounts
A=$(curl -s -X POST localhost:8000/accounts -H 'content-type: application/json' \
      -d '{"name":"Asha"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
B=$(curl -s -X POST localhost:8000/accounts -H 'content-type: application/json' \
      -d '{"name":"Ravi"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -s -X POST localhost:8000/accounts/$A/transactions -H 'content-type: application/json' \
     -d '{"type":"credit","amount":100,"reference_id":"INV-1","description":"Opening deposit"}'

curl -s -X POST localhost:8000/transfers -H 'content-type: application/json' \
     -d "{\"from_account_id\":\"$A\",\"to_account_id\":\"$B\",\"amount\":30,\"reference_id\":\"TRF-1\"}"

curl -s localhost:8000/accounts/$A/balance
curl -s "localhost:8000/accounts/$A/transactions?type=debit&page=1&limit=10"
```

---

## Project structure

```
assesment/
├── app/
│   ├── main.py            # app wiring: routers, error handlers, DB init on startup
│   ├── db.py              # connections, schema bootstrap, atomic() transaction unit
│   ├── schema.sql         # tables, constraints, indexes, immutability triggers
│   ├── models.py          # Pydantic request/response schemas (validation lives here)
│   ├── service.py         # business rules: balances, duplicates, history, transfers
│   ├── errors.py          # one consistent error envelope for every failure
│   └── routes/
│       ├── accounts.py    # /accounts, /accounts/{id}/balance, /accounts/{id}/transactions
│       └── transfers.py   # /transfers
├── tests/test_api.py
├── scripts/verify_live.py # end-to-end PASS/FAIL check against a live server
├── API.md
├── requirements.txt / requirements-dev.txt
└── README.md
```

Each layer has one job: routes handle HTTP, models validate input, service
applies the rules, and the database enforces the invariants as a backstop.

---

## Design and complexity

| Operation | Complexity | How |
| --- | --- | --- |
| Create account | **O(log n)** | one B-tree insert |
| Add transaction | **O(log n)** | PK lookup + `(account_id, reference_id)` unique-index lookup + one insert + one PK update |
| Get balance | **O(log n)** | the balance is stored on the account, never recomputed by summing |
| Transaction history | **O(log n + offset + limit)** | reverse scan of a composite index, so there is no sort step |
| Pagination totals | **O(1)** | per-account `credit_count` / `debit_count` counters, no `COUNT(*)` |
| Transfer | **O(log n)** | two of the above inside one DB transaction |

**Running balance instead of re-summing.** Every transaction updates
`accounts.balance` in the same DB transaction as the insert. A balance read is a
single row lookup and never scans history.

**Indexes match the queries exactly.** `(account_id, created_at)` and
`(account_id, type, created_at)` cover the unfiltered and type-filtered history.
SQLite walks them backwards, which gives `created_at DESC` order. The implicit
trailing `rowid` breaks ties when two rows share a timestamp, so pages never
overlap or skip rows. A test runs `EXPLAIN QUERY PLAN` to prove there is no
temp-B-tree sort.

**O(1) totals.** Paginated APIs often run `COUNT(*)` on every request, which is
O(n). This one keeps per-type counters on the account row, updated atomically
with each insert.

**Exact money.** Amounts are accepted with at most 2 decimal places and stored as
integer minor units (paise/cents). Ten credits of `0.1` give exactly `1.00`.

### Integrity guarantees

| Requirement | Enforced by |
| --- | --- |
| Atomicity | `BEGIN IMMEDIATE … COMMIT`. Any exception rolls back the insert and the balance update together. Tests force a failure *between* the two steps and check nothing persisted. |
| No overdraft, even under concurrency | `BEGIN IMMEDIATE` takes the write lock before the balance is read, so check-then-write cannot interleave. `CHECK (balance >= 0)` is a DB backstop. Tested with 20 parallel debits across 2 server processes: exactly the affordable number succeed. |
| Duplicate `reference_id` per account | `UNIQUE (account_id, reference_id)`. The duplicate check also runs *before* the balance check, so a retried debit gets `409 DUPLICATE_TRANSACTION` and not a misleading `INSUFFICIENT_BALANCE`. |
| Immutability | No update/delete routes (`405`), plus DB triggers that `ABORT` any `UPDATE`/`DELETE` on `transactions` and `transfers`. Direct SQL can't change them either. |
| Valid data | `CHECK` constraints on `type`, `amount > 0`, non-empty `name`/`reference_id`, and `typeof(balance) = 'integer'`. Foreign keys are on. |
| No balance overflow | A credit that would push a balance past SQLite's int64 limit is rejected with `422 BALANCE_LIMIT_EXCEEDED`. Without this, SQLite would silently store a lossy REAL. |
| Busy database | If the write lock can't be taken within 5 s, the request returns `503 SERVICE_BUSY` with `Retry-After: 1`. Nothing was written, and `reference_id` makes the retry safe. |
| Client can't set the balance | Request models reject unknown fields (`extra="forbid"`). `{"name":"x","balance":1000}` returns `422`. |
| Transfers can't be partial | Transfer record, debit leg, credit leg and both balance updates share one DB transaction. A test fails the credit leg and checks the debit was rolled back. |

---

## Assumptions

1. **IDs are server-generated UUIDv4s.** The brief says only that `id` must be unique. Clients never choose IDs.
2. **Amounts** must be `> 0`, have at most 2 decimal places, and be below 10¹³. Numbers and numeric strings (`"50.25"`) are both accepted. Digit separators such as `"1_000"` are rejected with `INVALID_AMOUNT`. Responses return amounts as JSON numbers in major units (`150.0`).
3. **A duplicate `reference_id` returns `409 Conflict`** with the *existing* transaction in `error.details`, so a retrying client can recover the original result. The duplicate is checked even when the new payload differs (amount or type): the reference identifies the operation.
4. **Insufficient balance returns `422 Unprocessable Entity`.** The request is well-formed but breaks a business rule. Rejected transactions are not stored.
5. **Validation errors return `422`. Malformed JSON or a non-object body returns `400`.** Field-specific codes (`INVALID_AMOUNT`, `INVALID_TRANSACTION_TYPE`, `INVALID_PAGINATION`) are used when the failure comes from that field alone.
6. **Pagination:** `page` defaults to 1 and `limit` to 20, with a max `limit` of 100. A page past the end returns an empty `data` list with correct metadata, not an error, and runs no row query (so even `page=10^30` is safe). The counter and the rows are read in one snapshot, so `total_records` always agrees with `data`. An account with no transactions has `total_pages: 0`.
7. **Sorting** is by `created_at` descending, with insertion order as the tie-break.
8. **Timestamps** are ISO-8601 UTC with microsecond precision, generated by the server.
9. **`description` is optional** (up to 500 chars). `name` is trimmed and limited to 200 chars. `reference_id` is trimmed and limited to 100 chars, and may not start with `transfer:` (reserved for transfer legs).
10. **Transfers** require a client `reference_id`, which is unique per *source* account, so a retried transfer is idempotent (`409 DUPLICATE_TRANSFER`). The transfer appears in each account's history as a normal debit/credit entry with `transfer_id` set. Each leg's `reference_id` is `transfer:<transfer_id>`, so it can't collide with a client reference on either account. A transfer to the same account is rejected.
11. **No authentication or authorization.** The brief doesn't ask for it.
12. **SQLite** was chosen so the project runs with zero setup and still has real constraints, triggers and ACID transactions. The SQL is portable. On PostgreSQL the same design would use `SELECT … FOR UPDATE` row locks in place of SQLite's database-level write lock, which would let writes to different accounts run in parallel.
13. **Offset pagination** is used because the brief asks for `page`/`limit`. For very deep pages, keyset (cursor) pagination on `(created_at, rowid)` would make every page O(log n + limit). The indexes already support it.
