-- Money is stored as INTEGER minor units (paise/cents) so arithmetic is exact.
-- Timestamps are ISO-8601 UTC strings with microseconds, which sort lexicographically.

CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    name         TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    -- typeof() check: an int64 overflow would otherwise be stored as a lossy REAL.
    balance      INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0 AND typeof(balance) = 'integer'),
    -- Per-type counters kept in the same DB transaction as every insert, so
    -- pagination totals are an O(1) read instead of an O(n) COUNT(*).
    credit_count INTEGER NOT NULL DEFAULT 0 CHECK (credit_count >= 0),
    debit_count  INTEGER NOT NULL DEFAULT 0 CHECK (debit_count >= 0),
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    id              TEXT PRIMARY KEY,
    from_account_id TEXT    NOT NULL REFERENCES accounts (id),
    to_account_id   TEXT    NOT NULL REFERENCES accounts (id),
    amount          INTEGER NOT NULL CHECK (amount > 0),
    reference_id    TEXT    NOT NULL CHECK (length(trim(reference_id)) > 0),
    description     TEXT,
    created_at      TEXT    NOT NULL,
    CHECK (from_account_id <> to_account_id),
    UNIQUE (from_account_id, reference_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id           TEXT PRIMARY KEY,
    account_id   TEXT    NOT NULL REFERENCES accounts (id),
    type         TEXT    NOT NULL CHECK (type IN ('credit', 'debit')),
    amount       INTEGER NOT NULL CHECK (amount > 0),
    description  TEXT,
    reference_id TEXT    NOT NULL CHECK (length(trim(reference_id)) > 0),
    transfer_id  TEXT REFERENCES transfers (id),
    created_at   TEXT    NOT NULL,
    -- Idempotency: one reference per account; also the O(log n) duplicate lookup.
    UNIQUE (account_id, reference_id)
);

-- History reads walk these B-trees backwards: O(log n + offset + limit), no sort step.
-- rowid is implicitly the last key column, so a reverse scan yields
-- (created_at DESC, rowid DESC) -- a stable tie-break on equal timestamps.
CREATE INDEX IF NOT EXISTS ix_tx_account_created
    ON transactions (account_id, created_at);
CREATE INDEX IF NOT EXISTS ix_tx_account_type_created
    ON transactions (account_id, type, created_at);
-- O(log n) lookup of a transfer's two legs.
CREATE INDEX IF NOT EXISTS ix_tx_transfer
    ON transactions (transfer_id) WHERE transfer_id IS NOT NULL;

-- Immutability enforced by the database itself, not only by the absence of routes.
CREATE TRIGGER IF NOT EXISTS trg_transactions_no_update
BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_transactions_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_transfers_no_update
BEFORE UPDATE ON transfers
BEGIN
    SELECT RAISE(ABORT, 'transfers are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_transfers_no_delete
BEFORE DELETE ON transfers
BEGIN
    SELECT RAISE(ABORT, 'transfers are immutable');
END;
