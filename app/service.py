"""Ledger business logic. Every write runs inside one `atomic()` unit.

Complexity (n = rows in the touched table, B-tree indexes throughout):
  create_account     O(log n)
  add_transaction    O(log n)   account PK lookup + (account_id, reference_id) unique lookup
                                + one insert + one PK update
  get_balance        O(log n)
  list_transactions  O(log n + offset + limit); total from O(1) per-account counters
  transfer           O(log n)
"""
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .db import atomic, snapshot
from .errors import APIError, not_found
from .models import TRANSFER_REF_PREFIX, TransactionCreate, TransferCreate, TxType

# SQLite INTEGER is signed 64-bit; past this, arithmetic silently becomes a lossy REAL.
MAX_BALANCE = 2**63 - 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _cents(amount: Decimal) -> int:
    return int(amount * 100)  # exact: validated to at most 2 decimal places


def _major(cents: int) -> float:
    return cents / 100


def _account_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "balance": _major(row["balance"]),
        "created_at": row["created_at"],
    }


def _tx_out(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["amount"] = _major(out["amount"])
    return out


_TX_COLS = "id, account_id, type, amount, description, reference_id, transfer_id, created_at"


def _fetch_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise not_found(account_id)
    return row


def _apply(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    tx_type: TxType,
    cents: int,
    reference_id: str,
    description: Optional[str],
    created_at: str,
    transfer_id: Optional[str] = None,
) -> dict:
    """Insert one ledger entry and move the balance. Caller must hold `atomic()`."""
    if tx_type is TxType.debit and account["balance"] < cents:
        raise APIError(
            422,
            "INSUFFICIENT_BALANCE",
            "Insufficient balance for this debit.",
            {
                "account_id": account["id"],
                "balance": _major(account["balance"]),
                "requested": _major(cents),
            },
        )
    if tx_type is TxType.credit and account["balance"] + cents > MAX_BALANCE:
        raise APIError(
            422,
            "BALANCE_LIMIT_EXCEEDED",
            "This credit would exceed the maximum balance an account can hold.",
            {"account_id": account["id"], "balance": _major(account["balance"]), "requested": _major(cents)},
        )
    tx_id = str(uuid.uuid4())
    conn.execute(
        f"INSERT INTO transactions ({_TX_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tx_id, account["id"], tx_type.value, cents, description, reference_id, transfer_id, created_at),
    )
    delta = cents if tx_type is TxType.credit else -cents
    counter = "credit_count" if tx_type is TxType.credit else "debit_count"
    # Guarded update: the CHECK (balance >= 0) constraint is the last line of defence.
    conn.execute(
        f"UPDATE accounts SET balance = balance + ?, {counter} = {counter} + 1 WHERE id = ?",
        (delta, account["id"]),
    )
    return {
        "id": tx_id,
        "account_id": account["id"],
        "type": tx_type.value,
        "amount": _major(cents),
        "description": description,
        "reference_id": reference_id,
        "transfer_id": transfer_id,
        "created_at": created_at,
    }


def create_account(conn: sqlite3.Connection, name: str) -> dict:
    row = {"id": str(uuid.uuid4()), "name": name, "balance": 0, "created_at": _now()}
    with atomic(conn):
        conn.execute(
            "INSERT INTO accounts (id, name, balance, created_at) VALUES (?, ?, 0, ?)",
            (row["id"], name, row["created_at"]),
        )
    row["balance"] = 0.0
    return row


def get_account(conn: sqlite3.Connection, account_id: str) -> dict:
    return _account_out(_fetch_account(conn, account_id))


def get_balance(conn: sqlite3.Connection, account_id: str) -> dict:
    row = conn.execute("SELECT id, balance FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise not_found(account_id)
    return {"account_id": row["id"], "balance": _major(row["balance"])}


def add_transaction(conn: sqlite3.Connection, account_id: str, body: TransactionCreate) -> dict:
    with atomic(conn):
        account = _fetch_account(conn, account_id)
        # Duplicate check precedes the balance check: a retried debit that already
        # succeeded must report "already exists", not "insufficient balance".
        existing = conn.execute(
            f"SELECT {_TX_COLS} FROM transactions WHERE account_id = ? AND reference_id = ?",
            (account_id, body.reference_id),
        ).fetchone()
        if existing is not None:
            raise APIError(
                409,
                "DUPLICATE_TRANSACTION",
                f"A transaction with reference_id '{body.reference_id}' already exists for this account.",
                {"existing_transaction": _tx_out(existing)},
            )
        return _apply(
            conn, account, body.type, _cents(body.amount), body.reference_id, body.description, _now()
        )


def list_transactions(
    conn: sqlite3.Connection, account_id: str, tx_type: Optional[TxType], page: int, limit: int
) -> dict:
    offset = (page - 1) * limit
    # One snapshot, so total_records always agrees with the rows returned even
    # while other requests are writing.
    with snapshot(conn):
        account = _fetch_account(conn, account_id)
        if tx_type is None:
            total = account["credit_count"] + account["debit_count"]
            where, params = "account_id = ?", [account_id]
        else:
            total = account[f"{tx_type.value}_count"]
            where, params = "account_id = ? AND type = ?", [account_id, tx_type.value]

        # A page past the end needs no query. This also keeps an arbitrarily large
        # `page` from overflowing SQLite's 64-bit OFFSET.
        rows = conn.execute(
            f"SELECT {_TX_COLS} FROM transactions WHERE {where} "
            "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall() if offset < total else []

    total_pages = math.ceil(total / limit) if total else 0
    return {
        "data": [_tx_out(r) for r in rows],
        "pagination": {
            "page": page,
            "limit": limit,
            "total_records": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
        },
    }


def transfer(conn: sqlite3.Connection, body: TransferCreate) -> dict:
    with atomic(conn):
        source = _fetch_account(conn, body.from_account_id)
        dest = _fetch_account(conn, body.to_account_id)

        existing = conn.execute(
            "SELECT * FROM transfers WHERE from_account_id = ? AND reference_id = ?",
            (source["id"], body.reference_id),
        ).fetchone()
        if existing is not None:
            raise APIError(
                409,
                "DUPLICATE_TRANSFER",
                f"A transfer with reference_id '{body.reference_id}' already exists for this source account.",
                {"existing_transfer": _transfer_out(conn, existing)},
            )

        cents, now, transfer_id = _cents(body.amount), _now(), str(uuid.uuid4())
        conn.execute(
            "INSERT INTO transfers (id, from_account_id, to_account_id, amount, reference_id, "
            "description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (transfer_id, source["id"], dest["id"], cents, body.reference_id, body.description, now),
        )
        # Both legs share a reference derived from the (unique) transfer id, so they
        # can never collide with a client-chosen reference on either account.
        leg_ref = f"{TRANSFER_REF_PREFIX}{transfer_id}"
        debit = _apply(conn, source, TxType.debit, cents, leg_ref, body.description, now, transfer_id)
        credit = _apply(conn, dest, TxType.credit, cents, leg_ref, body.description, now, transfer_id)

    return {
        "id": transfer_id,
        "from_account_id": source["id"],
        "to_account_id": dest["id"],
        "amount": _major(cents),
        "reference_id": body.reference_id,
        "description": body.description,
        "created_at": now,
        "debit_transaction_id": debit["id"],
        "credit_transaction_id": credit["id"],
    }


def _transfer_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    legs = dict(
        conn.execute("SELECT type, id FROM transactions WHERE transfer_id = ?", (row["id"],)).fetchall()
    )
    return {
        "id": row["id"],
        "from_account_id": row["from_account_id"],
        "to_account_id": row["to_account_id"],
        "amount": _major(row["amount"]),
        "reference_id": row["reference_id"],
        "description": row["description"],
        "created_at": row["created_at"],
        "debit_transaction_id": legs.get("debit"),
        "credit_transaction_id": legs.get("credit"),
    }
